"""Ticket 07 确定性崩溃 / 恢复 / resolution 子进程（仅测试用）。

核心场景：NON_IDEMPOTENT 通知工具把外部效果写入**独立于 RunStore 的
journal 文件**（副作用证据，重复执行即可观测），进程在 Tool Step
checkpoint 提交前硬崩溃（``os._exit``）；第二进程恢复时运行时**不得
再次通知**，而是进入 WAITING 等待上层应用显式 resolution。

用法：``python tests/fixtures/notification_worker.py <db>
<notification-journal> <model-journal> <mode> <run_id> [action [result]]``

- ``mode=notify-and-crash``：创建 Run 并启动；模型请求 notify 工具，
  notify 写入 journal 后成功返回，进程在 ``BEFORE_TOOL_CHECKPOINT``
  硬崩溃（退出码 17）。stdout 打印 ``RUN_ID=<run_id>``。
- ``mode=resume``：重开数据库（时钟越过崩溃遗留租约），resume_run；
  Run 应进入 WAITING 且**不追加 journal**。stdout 打印
  ``RUN_ID=`` / ``STATUS=`` / ``WAITING_REASON=`` / ``STEP_ID=`` /
  ``ALLOWED=`` / ``EVIDENCE=``（机器可读的公开 inspection 快照）。
- ``mode=resolve <action> [result]``：重开数据库后先 resume（幂等
  返回 WAITING），再提交指定 resolution：
  ``RETRY_STEP`` / ``CONFIRM_STEP <result>`` / ``FAIL_RUN`` /
  ``CANCEL_RUN``。stdout 打印 ``RUN_ID=`` / ``STATUS=`` / ``EVIDENCE=``
  （等待态与终态的公开 inspection 快照）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta

_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "src")
)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from m_agent import (  # noqa: E402
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    DeterministicModelAdapter,
    DeterministicTool,
    FakeClock,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    PlaintextPayloadCodec,
    ResolutionAction,
    RunResolution,
    Runner,
    SQLiteRunStore,
    ToolCall,
    ToolEffect,
    ToolOutcome,
    allowed_resolutions,
)
from m_agent.runtime import ModelRequirements, ToolCallingMode

_CRASH_EXIT_CODE = 17
_TOOL_CALLING = ModelCapabilities(tool_calling=ToolCallingMode.NATIVE)


def journal_count(path: str) -> int:
    """读取独立外部 journal 的效果/调用次数。"""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def append_journal(path: str, entry: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry + "\n")


class JournalNotifier(DeterministicTool):
    """NON_IDEMPOTENT fake 通知：把效果追加到外部 journal 文件。

    副作用证据存在 RunStore 之外的 journal（每次调用追加一行），
    因此重复执行可被确定性观测——这正是 Ticket 07 的核心证据。
    """

    deterministic: bool = True

    def __init__(self, journal_path: str) -> None:
        super().__init__(
            name="notify",
            effect=ToolEffect.NON_IDEMPOTENT,
        )
        self._journal_path = journal_path

    async def invoke(self, request: ToolRequest) -> ToolOutcome:
        n = journal_count(self._journal_path) + 1
        append_journal(self._journal_path, f"notify:{n}")
        return ToolOutcome.success(
            request.call_id, self.name, result=f"notification-{n}"
        )


class NotifyThenAnswerModel(DeterministicModelAdapter):
    """确定性模型：第一次请求 notify 工具，收到 outcome 后给出最终响应。"""

    deterministic: bool = True

    def __init__(self, model_journal_path: str) -> None:
        super().__init__(capabilities=_TOOL_CALLING)
        self._model_journal_path = model_journal_path

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        append_journal(
            self._model_journal_path, f"model:{journal_count(self._model_journal_path) + 1}"
        )
        if not request.tool_outcomes:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        call_id="call-notify",
                        tool_name="notify",
                        arguments="{}",
                    ),
                )
            )
        return ModelResponse(
            content="delivered: " + request.tool_outcomes[0].result
        )


def build_registry(
    journal_path: str, model_journal_path: str
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="support_agent",
            version="1.0",
            instructions="Notify the customer deterministically.",
            model_requirements=ModelRequirements(capabilities=_TOOL_CALLING),
            model_adapter=NotifyThenAnswerModel(model_journal_path),
            tools=(JournalNotifier(journal_path),),
        )
    )
    return registry


async def inspection_evidence(runner: Runner, run_id: str) -> dict[str, object]:
    """仅经公开 Runner inspection API 构建机器可读的跨进程证据。"""
    inspection = await runner.inspect_run(run_id)
    run = inspection.run
    return {
        "status": run.status.value,
        "waiting_reason": run.waiting_reason,
        "waiting_step_id": run.waiting_step_id,
        "allowed_actions": [
            action.value for action in allowed_resolutions(run)
        ],
        "steps": [
            {
                "step_id": step.step_id,
                "step_type": step.step_type.value,
                "status": step.status.value,
            }
            for step in inspection.steps
        ],
        "attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "step_id": attempt.step_id,
                "status": attempt.status.value,
                "classification": (
                    attempt.classification.value
                    if attempt.classification is not None
                    else None
                ),
                "error_code": attempt.error_code,
            }
            for attempt in inspection.attempts
        ],
        "checkpoints": [
            {
                "step_id": checkpoint.step_id,
                "attempt_id": checkpoint.attempt_id,
                "step_type": checkpoint.step_type.value,
            }
            for checkpoint in inspection.checkpoints
        ],
    }


def print_evidence(
    waiting: dict[str, object],
    terminal: dict[str, object] | None,
    *,
    waiting_model_call_count: int,
    waiting_notification_count: int,
    model_journal_path: str,
    notification_journal_path: str,
) -> None:
    """将独立 journal 与公开 inspection 证据写成单个可解析记录。"""
    print(
        "EVIDENCE="
        + json.dumps(
            {
                "waiting": waiting,
                "terminal": terminal,
                "waiting_model_call_count": waiting_model_call_count,
                "waiting_notification_count": waiting_notification_count,
                "model_call_count": journal_count(model_journal_path),
                "notification_count": journal_count(notification_journal_path),
            },
            sort_keys=True,
        ),
        flush=True,
    )


async def open_after_crash(
    db_path: str, run_id: str
) -> tuple[SQLiteRunStore, FakeClock]:
    """重开崩溃进程遗留的数据库，并把时钟推进到崩溃租约过期之后。

    崩溃进程（``os._exit``）的租约仍持久化在数据库中；第二进程必须
    等租约过期才能接管（ADR 0013）。通过公开 ``get_run`` 读取持久化
    的 ``lease_expires_at``，用 :class:`FakeClock` 确定性推进，避免
    真实 sleep。
    """
    probe = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
    try:
        crashed = await probe.get_run(run_id)
        assert crashed is not None, f"run {run_id} not found"
        assert crashed.lease_expires_at is not None, (
            "crashed run must carry a persisted lease"
        )
        expires = crashed.lease_expires_at
    finally:
        probe.close()
    clock = FakeClock(start=expires + timedelta(seconds=1))
    store = SQLiteRunStore(
        db_path, payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    return store, clock


def main() -> None:
    db_path = sys.argv[1]
    journal_path = sys.argv[2]
    model_journal_path = sys.argv[3]
    mode = sys.argv[4]
    run_id = sys.argv[5]
    action = sys.argv[6] if len(sys.argv) > 6 else None
    result = sys.argv[7] if len(sys.argv) > 7 else None

    async def run() -> None:
        if mode == "notify-and-crash":
            def crash_hook(point: CrashPoint, run_id: str) -> None:
                if point is CrashPoint.BEFORE_TOOL_CHECKPOINT:
                    print(f"RUN_ID={run_id}", flush=True)
                    os._exit(_CRASH_EXIT_CODE)  # noqa: PLR1722 - 硬崩溃模拟

            registry = build_registry(journal_path, model_journal_path)
            store = SQLiteRunStore(
                db_path, payload_codec=PlaintextPayloadCodec()
            )
            runner = Runner(
                registry=registry, store=store, crash_hook=crash_hook
            )
            created = await runner.create_run(
                "support_agent", "1.0", input="hi"
            )
            await runner.start_run(created.run_id)
            return

        # resume / resolve：真实第二进程，时钟越过崩溃租约后接管。
        store, _ = await open_after_crash(db_path, run_id)
        registry = build_registry(journal_path, model_journal_path)
        runner = Runner(registry=registry, store=store)
        waiting = await runner.resume_run(run_id)
        waiting_snapshot = await inspection_evidence(runner, run_id)
        waiting_model_call_count = journal_count(model_journal_path)
        waiting_notification_count = journal_count(journal_path)
        if mode == "resume":
            print(f"RUN_ID={run_id}", flush=True)
            print(f"STATUS={waiting.status.value}", flush=True)
            print(
                f"WAITING_REASON={waiting.waiting_reason}", flush=True
            )
            print(f"STEP_ID={waiting.waiting_step_id}", flush=True)
            print(
                "ALLOWED="
                + ",".join(a.value for a in allowed_resolutions(waiting)),
                flush=True,
            )
            print_evidence(
                waiting_snapshot,
                None,
                waiting_model_call_count=waiting_model_call_count,
                waiting_notification_count=waiting_notification_count,
                model_journal_path=model_journal_path,
                notification_journal_path=journal_path,
            )
            return
        if mode == "resolve":
            assert action is not None, "resolve mode requires an action"
            resolution = RunResolution(
                action=ResolutionAction(action),
                result=result,
                waiting_step_id=waiting.waiting_step_id,
            )
            terminal = await runner.resolve_run(
                run_id,
                resolution,
                expected_version=waiting.version,
            )
            print(f"RUN_ID={run_id}", flush=True)
            print(f"STATUS={terminal.status.value}", flush=True)
            print_evidence(
                waiting_snapshot,
                await inspection_evidence(runner, run_id),
                waiting_model_call_count=waiting_model_call_count,
                waiting_notification_count=waiting_notification_count,
                model_journal_path=model_journal_path,
                notification_journal_path=journal_path,
            )
            return
        raise SystemExit(f"unknown mode {mode!r}")

    asyncio.run(run())


if __name__ == "__main__":
    main()
