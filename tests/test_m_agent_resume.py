"""Ticket 02/04 跨进程崩溃恢复测试。

Ticket 02 验收要求：

- 崩溃测试启动真实子进程（``os._exit`` 硬退出），不以同进程异常冒充
  进程状态丢失；
- crash injection 确定且可测试：子进程通过 :class:`CrashPoint` 在
  checkpoint 落盘后 / 落盘前退出，无随机时机、无长 sleep；
- 恢复只用持久化状态 + 精确 Definition 版本解析，不保留第一进程的
  Python 对象（子进程对象随 ``os._exit`` 全部销毁）；
- 模型实际调用次数以跨进程日志文件为硬证据；
- 精确 Definition 缺失 -> WAITING / DEFINITION_UNAVAILABLE，绝不使用
  最新版本；
- 恢复语义记录并验证为 at-least-once（checkpoint 前崩溃会重新执行）。

Ticket 04 追加验收（:class:`ContextCrossProcessResumeTests`）：

- Context Step checkpoint 后崩溃：第二进程复用原始 Context Items，
  provider 不再被调用（跨进程日志为硬证据）、外部数据变化不重写；
- Context checkpoint 落盘前崩溃：provider 重新执行（at-least-once）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

# 保证直接运行（unittest.main）与 pytest 下都能导入 fixtures 包。
_TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from m_agent import (
    DEFAULT_LEASE_TTL,
    AgentDefinition,
    CrashPoint,
    DefinitionRegistry,
    DeterministicModelAdapter,
    FakeClock,
    IllegalRunTransitionError,
    InMemoryRunStore,
    PlaintextPayloadCodec,
    REASON_DEFINITION_UNAVAILABLE,
    RunNotFoundError,
    Runner,
    RunStatus,
    SQLiteRunStore,
    StepType,
    deserialize_model_response,
)

from fixtures.crash_worker import LoggingContextProvider, LoggingModelAdapter

_WORKER = Path(__file__).parent / "fixtures" / "crash_worker.py"
_CRASH_EXIT_CODE = 17


def build_registry(
    log_path: str,
    version: str = "1.0",
    provider_log: str | None = None,
) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition(
            definition_id="assistant",
            version=version,
            instructions=f"Answer deterministically ({version}).",
            model_adapter=LoggingModelAdapter(
                log_path=log_path,
                responses=("crash-safe answer",),
            ),
            context_provider=(
                LoggingContextProvider(log_path=provider_log)
                if provider_log is not None
                else None
            ),
        )
    )
    return registry


async def open_resume_store(
    db_path: str, run_id: str
) -> tuple[SQLiteRunStore, FakeClock]:
    """重开数据库并返回恢复 store + 已越过崩溃遗留租约过期点的时钟。

    Ticket 03 语义（ADR 0013）：崩溃进程的租约在 TTL 内仍有效，恢复
    Runner 必须等租约过期才能接管。本 helper 读取崩溃后持久化的
    ``lease_expires_at``，把 :class:`FakeClock` 确定性推进到过期之后，
    避免用真实 sleep 等待。
    """
    probe = SQLiteRunStore(db_path, payload_codec=PlaintextPayloadCodec())
    try:
        crashed = await probe.get_run(run_id)
        assert crashed is not None
        expires = crashed.lease_expires_at
    finally:
        probe.close()
    assert expires is not None, "crashed run must carry a persisted lease"
    clock = FakeClock(start=expires + timedelta(seconds=1))
    store = SQLiteRunStore(
        db_path, payload_codec=PlaintextPayloadCodec(), clock=clock
    )
    return store, clock


def run_worker(
    db_path: str,
    log_path: str,
    crash_point: str,
    provider_log: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """启动真实子进程执行 Run；返回 completed process（可能崩溃退出）。"""
    cmd = [
        sys.executable,
        str(_WORKER),
        db_path,
        log_path,
        crash_point,
    ]
    if provider_log is not None:
        cmd.append(provider_log)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def parse_run_id(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RUN_ID="):
            return line.split("=", 1)[1]
    raise AssertionError(f"worker did not print RUN_ID; stdout={stdout!r}")


def count_model_calls(log_path: str) -> int:
    if not os.path.exists(log_path):
        return 0
    with open(log_path, encoding="utf-8") as fh:
        return len([line for line in fh if line.strip()])


class CrashAfterCheckpointResumeTests(unittest.IsolatedAsyncioTestCase):
    """核心路径：checkpoint 已持久化、终态未写入时崩溃，第二进程恢复。"""

    async def test_second_process_reuses_checkpoint_without_model_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")

            # 第一进程：确定性崩溃在 checkpoint 落盘之后、终态写入之前。
            proc = run_worker(db, log, "after_model_checkpoint")
            self.assertNotEqual(
                proc.returncode, 0, f"worker should crash; stdout={proc.stdout}"
            )
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)
            self.assertEqual(count_model_calls(log), 1)

            # 崩溃后的持久化状态：RUNNING + checkpoint 已写、终态未写。
            probe = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            try:
                crashed = await probe.get_run(run_id)
                self.assertIsNotNone(crashed)
                self.assertEqual(crashed.status, RunStatus.RUNNING)
                self.assertIsNone(crashed.output)
                checkpoints = await probe.get_checkpoints(run_id)
                self.assertEqual(len(checkpoints), 1)
            finally:
                probe.close()

            # 第二进程：全新 registry / store / runner，无任何第一进程对象。
            # 崩溃进程的租约仍在 TTL 内，恢复前把时钟推进到租约过期之后。
            registry = build_registry(log)
            adapter = registry.resolve("assistant", "1.0").model_adapter
            store, _ = await open_resume_store(db, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                terminal = await runner.resume_run(run_id)

                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(terminal.output, "crash-safe answer")
                self.assertEqual(terminal.snapshot.version, "1.0")
                # checkpoint 复用：本进程 adapter 一次模型都没有调用，
                # 跨进程日志也仍只有子进程的那一次调用。
                self.assertEqual(adapter.call_count, 0)
                self.assertEqual(count_model_calls(log), 1)

                inspection = await runner.inspect_run(run_id)
                self.assertEqual(len(inspection.steps), 1)
                self.assertEqual(len(inspection.attempts), 1)
                self.assertEqual(len(inspection.checkpoints), 1)
                # Ticket 05：checkpoint 携带完整序列化响应，内容可还原。
                self.assertEqual(
                    deserialize_model_response(
                        inspection.checkpoints[0].output
                    ).content,
                    "crash-safe answer",
                )
            finally:
                store.close()

    async def test_crash_before_checkpoint_reinvokes_model_at_least_once(
        self,
    ) -> None:
        # at-least-once：模型结果落盘前崩溃 -> 恢复时重新执行模型。
        # 绝不宣称 exactly-once。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")

            proc = run_worker(db, log, "before_model_checkpoint")
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)
            self.assertEqual(count_model_calls(log), 1)

            registry = build_registry(log)
            adapter = registry.resolve("assistant", "1.0").model_adapter
            store, _ = await open_resume_store(db, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                terminal = await runner.resume_run(run_id)
                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                # 第一次执行无 checkpoint 可复用 -> 重新执行（第二次调用）。
                self.assertEqual(adapter.call_count, 1)
                self.assertEqual(count_model_calls(log), 2)
            finally:
                store.close()


class DefinitionUnavailableTests(unittest.IsolatedAsyncioTestCase):
    """精确 Definition 缺失：WAITING / DEFINITION_UNAVAILABLE。"""

    async def test_reregistered_exact_definition_resumes_waiting_run(
        self,
    ) -> None:
        """公开 resume 会在旧版本恢复后继续已有安全恢复路径。

        第一进程在 Model checkpoint 已提交后硬崩溃；第二进程起初只
        注册 2.0，因此只能进入 ``DEFINITION_UNAVAILABLE``。随后应用
        重新注册精确的 1.0 并再次调用 ``resume_run``，必须复用既有
        checkpoint 完成 Run，既不能回退到 2.0，也不能重放已确认的模型
        调用。
        """
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            proc = run_worker(db, log, "after_model_checkpoint")
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)
            self.assertEqual(count_model_calls(log), 1)

            registry = build_registry(log, version="2.0")
            newer_adapter = registry.resolve(
                "assistant", "2.0"
            ).model_adapter
            store, _ = await open_resume_store(db, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                waiting = await runner.resume_run(run_id)
                self.assertEqual(waiting.status, RunStatus.WAITING)
                self.assertEqual(
                    waiting.waiting_reason, REASON_DEFINITION_UNAVAILABLE
                )
                self.assertEqual(newer_adapter.call_count, 0)

                # 应用恢复精确旧版本后，普通公开 resume 才重新进入
                # recovery 路径；无需也不得通过 resolution 伪造 Tool 处置。
                registry.register(
                    AgentDefinition(
                        definition_id="assistant",
                        version="1.0",
                        instructions="Answer deterministically (1.0).",
                        model_adapter=LoggingModelAdapter(
                            log_path=log,
                            responses=("crash-safe answer",),
                        ),
                    )
                )
                restored_adapter = registry.resolve(
                    "assistant", "1.0"
                ).model_adapter
                terminal = await runner.resume_run(run_id)

                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(terminal.output, "crash-safe answer")
                self.assertEqual(newer_adapter.call_count, 0)
                self.assertEqual(restored_adapter.call_count, 0)
                self.assertEqual(count_model_calls(log), 1)
            finally:
                store.close()

    async def test_resume_without_exact_definition_enters_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")

            proc = run_worker(db, log, "after_model_checkpoint")
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)
            self.assertEqual(count_model_calls(log), 1)

            # 只注册了 2.0（同 id、不同 version）——精确解析必须失败。
            registry = build_registry(log, version="2.0")
            new_version_adapter = registry.resolve(
                "assistant", "2.0"
            ).model_adapter
            store, _ = await open_resume_store(db, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                waiting = await runner.resume_run(run_id)

                self.assertEqual(waiting.status, RunStatus.WAITING)
                self.assertFalse(waiting.status.is_terminal)
                self.assertEqual(
                    waiting.waiting_reason, REASON_DEFINITION_UNAVAILABLE
                )
                # 绝不自动使用最新版本：2.0 的模型一次都没有被调用。
                self.assertEqual(new_version_adapter.call_count, 0)
                self.assertEqual(count_model_calls(log), 1)

                # 幂等：再次 resume 保持 WAITING，不推进、不报错。
                again = await runner.resume_run(run_id)
                self.assertEqual(again.status, RunStatus.WAITING)
                self.assertEqual(
                    again.waiting_reason, REASON_DEFINITION_UNAVAILABLE
                )
            finally:
                store.close()

    async def test_waiting_reason_is_persisted_in_metadata(self) -> None:
        # 跨进程验证：WAITING + reason 已落盘，重开数据库仍可读。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            log = os.path.join(tmp, "model_calls.log")
            proc = run_worker(db, log, "after_model_checkpoint")
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)

            registry = build_registry(log, version="2.0")
            store, _ = await open_resume_store(db, run_id)
            try:
                await Runner(registry=registry, store=store).resume_run(run_id)
            finally:
                store.close()

            reopened = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            try:
                record = await reopened.get_run(run_id)
                self.assertEqual(record.status, RunStatus.WAITING)
                self.assertEqual(
                    record.waiting_reason, REASON_DEFINITION_UNAVAILABLE
                )
            finally:
                reopened.close()


class ResumeContractTests(unittest.IsolatedAsyncioTestCase):
    """同进程 resume 语义与非法命令（共享 InMemory 契约）。"""

    def make_runner(
        self, crash_hook=None, clock=None
    ) -> tuple[Runner, DefinitionRegistry, InMemoryRunStore]:
        registry = DefinitionRegistry()
        registry.register(
            AgentDefinition(
                definition_id="assistant",
                version="1.0",
                instructions="Answer deterministically.",
                model_adapter=DeterministicModelAdapter(
                    responses=("in-memory answer",)
                ),
            )
        )
        store = InMemoryRunStore(
            payload_codec=PlaintextPayloadCodec(), clock=clock
        )
        return Runner(
            registry=registry, store=store, crash_hook=crash_hook
        ), registry, store

    async def test_resume_reuses_checkpoint_after_injected_crash(self) -> None:
        def hook(p: CrashPoint, run_id: str) -> None:
            if p is CrashPoint.AFTER_MODEL_CHECKPOINT:
                raise RuntimeError("injected crash")

        clock = FakeClock()
        runner, registry, store = self.make_runner(
            crash_hook=hook, clock=clock
        )
        adapter = registry.resolve("assistant", "1.0").model_adapter
        created = await runner.create_run("assistant", "1.0", input="hi")
        with self.assertRaises(RuntimeError):
            await runner.start_run(created.run_id)

        crashed = await store.get_run(created.run_id)
        self.assertEqual(crashed.status, RunStatus.RUNNING)
        self.assertEqual(len(await store.get_checkpoints(created.run_id)), 1)
        self.assertEqual(adapter.call_count, 1)

        # 崩溃 Runner 的租约仍有效：恢复前把时钟推进到租约过期之后，
        # 新 Runner 才能接管（ADR 0013）。
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        # 崩溃 hook 移除后恢复：复用 checkpoint，不重复调用模型。
        resumed = await Runner(registry=registry, store=store).resume_run(
            created.run_id
        )
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(resumed.output, "in-memory answer")
        self.assertEqual(adapter.call_count, 1)

    async def test_resume_terminal_run_is_rejected(self) -> None:
        runner, _, _ = self.make_runner()
        created = await runner.create_run("assistant", "1.0", input="hi")
        await runner.start_run(created.run_id)
        with self.assertRaises(IllegalRunTransitionError):
            await runner.resume_run(created.run_id)

    async def test_resume_missing_run_fails(self) -> None:
        runner, _, _ = self.make_runner()
        with self.assertRaises(RunNotFoundError):
            await runner.resume_run("no-such-run")

    async def test_resume_created_run_starts_like_start_run(self) -> None:
        # CREATED 状态的 Run 用 resume 与 start 走同一路径。
        runner, _, _ = self.make_runner()
        created = await runner.create_run("assistant", "1.0", input="hi")
        terminal = await runner.resume_run(created.run_id)
        self.assertEqual(terminal.status, RunStatus.SUCCEEDED)

    async def test_resume_created_run_without_definition_enters_waiting(
        self,
    ) -> None:
        # ADR 0023 对 resume 的统一语义：即使 Run 尚未启动（CREATED），
        # 精确 Definition 缺失也进入 WAITING，而不是抛错。
        runner, _, store = self.make_runner()
        created = await runner.create_run("assistant", "1.0", input="hi")

        # 用一个不含任何定义的全新 registry 恢复同一 Run。
        empty = Runner(registry=DefinitionRegistry(), store=store)
        waiting = await empty.resume_run(created.run_id)
        self.assertEqual(waiting.status, RunStatus.WAITING)
        self.assertEqual(
            waiting.waiting_reason, REASON_DEFINITION_UNAVAILABLE
        )
        self.assertFalse(waiting.status.is_terminal)


class ContextCrossProcessResumeTests(unittest.IsolatedAsyncioTestCase):
    """Ticket 04 跨进程：Context Step checkpoint 后崩溃，恢复复用 Items。

    第一进程带 Context Provider 执行并在 ``after_context_checkpoint``
    确定性崩溃；第二进程打开同一 SQLite、解析同一精确 Definition，
    必须复用已 checkpoint 的 Context Items 而不是再次查询外部数据。
    Provider 日志是跨进程硬证据：任何多余的 provider 调用都会在
    文件中留下第二行（且内容变为 changed）。
    """

    async def test_second_process_reuses_context_items_without_provider_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            model_log = os.path.join(tmp, "model_calls.log")
            provider_log = os.path.join(tmp, "provider_calls.log")

            # 第一进程：Context Step checkpoint 落盘后、Model Step 前崩溃。
            proc = run_worker(
                db, model_log, "after_context_checkpoint", provider_log
            )
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)
            # provider 已调用一次；模型尚未被调用。
            self.assertEqual(count_model_calls(provider_log), 1)
            self.assertEqual(count_model_calls(model_log), 0)

            # 崩溃后的持久化状态：RUNNING + 仅 CONTEXT checkpoint。
            probe = SQLiteRunStore(db, payload_codec=PlaintextPayloadCodec())
            try:
                crashed = await probe.get_run(run_id)
                self.assertEqual(crashed.status, RunStatus.RUNNING)
                self.assertIsNone(crashed.output)
                checkpoints = await probe.get_checkpoints(run_id)
                self.assertEqual(
                    [c.step_type for c in checkpoints], [StepType.CONTEXT]
                )
            finally:
                probe.close()

            # 第二进程：全新 registry / store / runner，无第一进程对象。
            registry = build_registry(model_log, provider_log=provider_log)
            provider = registry.resolve("assistant", "1.0").context_provider
            model_adapter = registry.resolve(
                "assistant", "1.0"
            ).model_adapter
            store, _ = await open_resume_store(db, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                terminal = await runner.resume_run(run_id)

                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                self.assertEqual(terminal.output, "crash-safe answer")
                # provider 不再被调用：本进程 call_count 为 0，跨进程
                # 日志仍只有子进程的 1 行。
                self.assertEqual(provider.call_count, 0)
                self.assertEqual(count_model_calls(provider_log), 1)
                # 模型只执行一次（复用 context 后完成 Model Step）。
                self.assertEqual(model_adapter.call_count, 1)
                self.assertEqual(count_model_calls(model_log), 1)
                # 模型收到的 Context Items 是 checkpoint 中的原始数据。
                request = model_adapter.last_request
                self.assertEqual(len(request.context_items), 1)
                self.assertEqual(
                    request.context_items[0].item_id, "ctx-1"
                )
                self.assertEqual(
                    request.context_items[0].content,
                    "original external context",
                )
                self.assertEqual(
                    request.context_items[0].source,
                    "fake-external-source",
                )
                # metadata 同样经 checkpoint 持久化并完整交付（AC 3）。
                self.assertEqual(
                    request.context_items[0].metadata,
                    {"attempt": "1"},
                )
                # 指令边界：instructions 不受外部数据影响。
                self.assertEqual(
                    request.instructions, "Answer deterministically."
                )

                inspection = await runner.inspect_run(run_id)
                self.assertEqual(
                    [s.step_type for s in inspection.steps],
                    [StepType.CONTEXT, StepType.MODEL],
                )
                self.assertEqual(len(inspection.attempts), 2)
                self.assertEqual(len(inspection.checkpoints), 2)
                context_ckpt = [
                    c
                    for c in inspection.checkpoints
                    if c.step_type is StepType.CONTEXT
                ][0]
                self.assertIn(
                    "original external context", context_ckpt.output
                )
            finally:
                store.close()

    async def test_crash_before_context_checkpoint_reinvokes_provider(
        self,
    ) -> None:
        # at-least-once：Context Step 的 checkpoint 落盘前崩溃 -> 恢复时
        # 重新执行 Context Step（外部数据被再次查询），不宣称 exactly-once。
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "run.db")
            model_log = os.path.join(tmp, "model_calls.log")
            provider_log = os.path.join(tmp, "provider_calls.log")

            proc = run_worker(
                db, model_log, "before_context_checkpoint", provider_log
            )
            self.assertEqual(proc.returncode, _CRASH_EXIT_CODE)
            run_id = parse_run_id(proc.stdout)
            self.assertEqual(count_model_calls(provider_log), 1)
            self.assertEqual(count_model_calls(model_log), 0)

            registry = build_registry(model_log, provider_log=provider_log)
            model_adapter = registry.resolve(
                "assistant", "1.0"
            ).model_adapter
            store, _ = await open_resume_store(db, run_id)
            try:
                runner = Runner(registry=registry, store=store)
                terminal = await runner.resume_run(run_id)
                self.assertEqual(terminal.status, RunStatus.SUCCEEDED)
                # 无 checkpoint 可复用：provider 被第二次调用（跨进程日志
                # 两行），模型收到的是第二次执行的内容。
                self.assertEqual(count_model_calls(provider_log), 2)
                self.assertEqual(count_model_calls(model_log), 1)
                request = model_adapter.last_request
                self.assertEqual(
                    request.context_items[0].content,
                    "CHANGED external context (must not be used)",
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
