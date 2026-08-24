"""确定性崩溃子进程（仅测试用）。

在指定 CrashPoint 通过 ``os._exit`` 硬终止进程，模拟真实进程崩溃：
不运行 finally / atexit、不回滚已提交的 SQLite 事务、不留存任何
Python 对象供后续进程复用。已提交的 checkpoint 落盘，Run 终态尚未
写入——这正是 Ticket 02 / 04 的确定性崩溃点。

用法：``python tests/fixtures/crash_worker.py <db_path> <model_log_path>
<crash_point> [<provider_log_path>]``

- ``crash_point`` 取值：``before_context_checkpoint`` /
  ``after_context_checkpoint`` / ``before_model_checkpoint`` /
  ``after_model_checkpoint`` / ``none``（对应 :class:`CrashPoint`）。
- 每次模型调用向 ``model_log_path`` 追加一行（跨进程调用计数证据）；
  传入 ``provider_log_path`` 时注册 Context Provider，每次 provider
  调用也追加一行（Ticket 04 跨进程复用证据）。
- 崩溃前向 stdout 输出 ``RUN_ID=<run_id>``；正常完成时输出
  ``RUN_ID=`` / ``ATTEMPT_ID=`` / ``STATUS=`` 三行。
"""

from __future__ import annotations

import asyncio
import os
import sys

_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src")
)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from m_agent.runtime import (
    AgentDefinition,
    ContextItem,
    ContextRequest,
    CrashPoint,
    DefinitionRegistry,
    ModelRequest,
    ModelResponse,
    Runner,
)
from m_agent.adapters import (
    DeterministicContextProvider,
    DeterministicModelAdapter,
    PlaintextPayloadCodec,
    SQLiteRunStore,
)
from m_agent import (
    AgentDefinition,
    DefinitionRegistry,
    Runner,
)
from m_agent.runtime import ModelExecutionBudget, RetryPolicy

_CRASH_EXIT_CODE = 17


class LoggingModelAdapter(DeterministicModelAdapter):
    """确定性模型：每次 generate 向 log 文件追加一行（跨进程计数）。"""

    deterministic: bool = True

    def __init__(self, log_path: str, responses: tuple[str, ...]) -> None:
        super().__init__(responses=responses)
        self._log_path = log_path

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{request.input}\n")
        index = min(self.call_count - 1, len(self._responses) - 1)
        return ModelResponse(content=self._responses[index])


class LoggingContextProvider(DeterministicContextProvider):
    """确定性外部数据源：每次 provide 写一行日志并返回变化的内容。

    第一次调用返回 ``original external context``；若恢复错误地再次
    调用，则返回 ``CHANGED external context (must not be used)``——
    跨进程日志行数即 provider 总调用次数，内容变化证明复用而不是
    重新查询。
    """

    def __init__(self, log_path: str) -> None:
        super().__init__()
        self._log_path = log_path

    async def provide(self, request: ContextRequest) -> list[ContextItem]:
        self.call_count += 1
        # 跨进程调用序号：以日志文件已有行数 + 1 为准（实例 call_count
        # 在恢复进程会重置，不能作为跨进程证据）。
        n = 0
        if os.path.exists(self._log_path):
            with open(self._log_path, encoding="utf-8") as fh:
                n = sum(1 for _ in fh)
        n += 1
        if n == 1:
            content = "original external context"
        else:
            content = "CHANGED external context (must not be used)"
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{content}\n")
        return [
            ContextItem(
                item_id="ctx-1",
                content=content,
                source="fake-external-source",
                metadata={"attempt": str(n)},
            )
        ]


def main() -> None:
    db_path, log_path, crash_point = sys.argv[1], sys.argv[2], sys.argv[3]
    provider_log = sys.argv[4] if len(sys.argv) > 4 else None

    context_provider = (
        LoggingContextProvider(log_path=provider_log)
        if provider_log is not None
        else None
    )
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="assistant",
            version="1.0",
            instructions="Answer deterministically.",
            model_execution_budget=ModelExecutionBudget(
                run_max_attempts=int(os.environ.get("M_AGENT_TEST_MODEL_BUDGET", "8")),
                primary_max_attempts=int(
                    os.environ.get("M_AGENT_TEST_MODEL_BUDGET", "8")
                ),
                context_compression_max_attempts=0,
                output_repair_max_attempts=0,
            ),
            model_adapter=LoggingModelAdapter(
                log_path=log_path,
                responses=("crash-safe answer",),
            ),
            context_provider=context_provider,
            retry_policy=RetryPolicy(max_attempts=2),
        )
    )

    def crash_hook(point: CrashPoint, run_id: str) -> None:
        if point.value == crash_point:
            print(f"RUN_ID={run_id}", flush=True)
            os._exit(_CRASH_EXIT_CODE)  # noqa: PLR1722 - 硬崩溃模拟

    async def run() -> None:
        store = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
        runner = Runner(registry=registry, store=store, crash_hook=crash_hook)
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.start_run(created.run_id)
        inspection = await runner.inspect_run(created.run_id)
        checkpoint = inspection.checkpoints[0]
        print(f"RUN_ID={created.run_id}", flush=True)
        print(f"ATTEMPT_ID={checkpoint.attempt_id}", flush=True)
        print(f"STATUS={terminal.status.value}", flush=True)
        store.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
