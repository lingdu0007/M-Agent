"""InMemoryRunStore 与 SQLiteRunStore 共享的行为契约测试。

两种 RunStore 实现必须通过同一份生命周期、
Step 记录、乐观版本控制与 Payload 处理契约；本模块以 mixin 形式
提供，具体 Store 实现各自继承（须同时继承
``unittest.IsolatedAsyncioTestCase``）并只提供 ``make_store()``
工厂。测试方法均为 async，由 IsolatedAsyncioTestCase 驱动。

断言只通过 RunStore 公开接口与（测试专用）原始编码字节视图驱动，
不触碰实现细节。SpyCodec 用于证明 Payload 内容只经配置的
Payload Codec 往返。
"""

from __future__ import annotations

from datetime import timedelta

from m_agent.runtime import (
    DEFAULT_LEASE_TTL,
    DefinitionSnapshot,
    DuplicateRunError,
    FailureClassification,
    IllegalRunTransitionError,
    LeaseNotHeldError,
    PayloadCodec,
    RunNotFoundError,
    RunRecord,
    RunStatus,
    StaleRunVersionError,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    StepType,
)
from m_agent.adapters import (
    DeterministicModelAdapter,
    FakeClock,
    PlaintextPayloadCodec,
)
from m_agent import (
    RunRecord,
    RunStatus,
)
from m_agent.runtime import (
    ModelBinding,
    ModelBindingSet,
    ModelPurpose,
    ModelRequirements,
)


class SpyCodec(PayloadCodec):
    """记录 encode/decode 输入的包装 Codec（仅测试用）。"""

    name = "spy"

    def __init__(self, inner: PayloadCodec) -> None:
        self._inner = inner
        self.encoded: list[str] = []
        self.decoded: list[bytes] = []

    def encode(self, payload: str) -> bytes:
        self.encoded.append(payload)
        return self._inner.encode(payload)

    def decode(self, encoded: bytes) -> str:
        self.decoded.append(encoded)
        return self._inner.decode(encoded)


def created_record(run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        definition_id="assistant",
        definition_version="1.0",
        input="hi",
        status=RunStatus.CREATED,
    )


def snapshot() -> DefinitionSnapshot:
    primary = ModelBinding(
        purpose=ModelPurpose.PRIMARY,
        contract=DeterministicModelAdapter().model_contract,
        requirements=ModelRequirements(),
    )
    return DefinitionSnapshot(
        definition_id="assistant",
        version="1.0",
        instructions="x",
        model_bindings=ModelBindingSet(
            bindings=(
                primary,
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.CONTEXT_COMPRESSION,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            ),
        ),
    )


class RunStoreContractMixin:
    """RunStore 行为契约。子类必须同时继承
    ``unittest.IsolatedAsyncioTestCase`` 并实现 ``make_store()``。

    ``make_store(codec=None, clock=None)``：codec 为 None 时使用
    :class:`PlaintextPayloadCodec`（测试中的显式 dev/test 选择）；
    clock 为 None 时使用系统时钟，传入 :class:`FakeClock` 可确定性
    驱动租约过期与接管。
    """

    def make_store(  # pragma: no cover
        self,
        codec: PayloadCodec | None = None,
        clock: FakeClock | None = None,
    ):
        raise NotImplementedError

    async def test_create_and_get_roundtrip(self) -> None:
        store = self.make_store()
        record = await store.create_run(created_record())
        self.assertEqual(record.status, RunStatus.CREATED)
        self.assertEqual(record.version, 1)

        stored = await store.get_run("run-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.run_id, "run-1")
        self.assertEqual(stored.definition_version, "1.0")
        self.assertEqual(stored.input, "hi")

    async def test_get_missing_run_returns_none(self) -> None:
        store = self.make_store()
        self.assertIsNone(await store.get_run("missing"))

    async def test_duplicate_create_rejected(self) -> None:
        store = self.make_store()
        await store.create_run(created_record())
        with self.assertRaises(DuplicateRunError):
            await store.create_run(created_record())

    async def test_legal_transition_increments_version(self) -> None:
        store = self.make_store()
        created = await store.create_run(created_record())
        running = await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
            snapshot=snapshot(),
        )
        self.assertEqual(running.status, RunStatus.RUNNING)
        self.assertEqual(running.version, 2)
        self.assertEqual(running.snapshot.definition_id, "assistant")

        succeeded = await store.transition_run(
            "run-1",
            expected_version=running.version,
            status=RunStatus.SUCCEEDED,
            output="done",
        )
        self.assertEqual(succeeded.status, RunStatus.SUCCEEDED)
        self.assertEqual(succeeded.version, 3)
        self.assertEqual(succeeded.output, "done")

    async def test_stale_version_update_rejected_without_mutation(
        self,
    ) -> None:
        store = self.make_store()
        created = await store.create_run(created_record())
        succeeded = await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )

        with self.assertRaises(StaleRunVersionError):
            await store.transition_run(
                "run-1",
                expected_version=created.version,
                status=RunStatus.SUCCEEDED,
            )

        # 权威记录保持为 RUNNING、version=2，未被回退。
        current = await store.get_run("run-1")
        self.assertEqual(current.status, RunStatus.RUNNING)
        self.assertEqual(current.version, 2)

    async def test_illegal_transition_rejected_without_mutation(self) -> None:
        store = self.make_store()
        created = await store.create_run(created_record())

        # CREATED 直接跳到 SUCCEEDED 非法。
        with self.assertRaises(IllegalRunTransitionError):
            await store.transition_run(
                "run-1",
                expected_version=created.version,
                status=RunStatus.SUCCEEDED,
            )

        # 终态不可再转换。
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )
        running = await store.get_run("run-1")
        await store.transition_run(
            "run-1",
            expected_version=running.version,
            status=RunStatus.SUCCEEDED,
        )
        with self.assertRaises(IllegalRunTransitionError):
            await store.transition_run(
                "run-1",
                expected_version=3,
                status=RunStatus.RUNNING,
            )

        # 所有非法尝试后权威记录仍是终态 SUCCEEDED。
        final = await store.get_run("run-1")
        self.assertEqual(final.status, RunStatus.SUCCEEDED)
        self.assertEqual(final.version, 3)

    async def test_transition_missing_run_fails(self) -> None:
        store = self.make_store()
        with self.assertRaises(RunNotFoundError):
            await store.transition_run(
                "missing", expected_version=1, status=RunStatus.RUNNING
            )

    async def test_step_attempt_checkpoint_roundtrip(self) -> None:
        store = self.make_store()
        await store.create_run(created_record())

        step = StepRecord(
            step_id="step-1",
            run_id="run-1",
            step_type=StepType.MODEL,
            status=StepStatus.SUCCEEDED,
        )
        await store.record_step(step, expected_version=1)
        attempt = StepAttempt(
            attempt_id="attempt-1",
            step_id="step-1",
            run_id="run-1",
            status=StepStatus.SUCCEEDED,
            output="deterministic answer",
        )
        await store.record_attempt(attempt, expected_version=1)
        checkpoint = StepCheckpoint(
            run_id="run-1",
            step_id="step-1",
            attempt_id="attempt-1",
            output="deterministic answer",
        )
        await store.record_checkpoint(checkpoint, expected_version=1)

        steps = await store.get_steps("run-1")
        attempts = await store.get_attempts("run-1")
        checkpoints = await store.get_checkpoints("run-1")
        self.assertEqual([s.step_id for s in steps], ["step-1"])
        self.assertEqual([a.attempt_id for a in attempts], ["attempt-1"])
        self.assertEqual([c.step_id for c in checkpoints], ["step-1"])
        self.assertEqual(checkpoints[0].output, "deterministic answer")

    async def test_inflight_step_and_attempt_update_by_stable_identity(
        self,
    ) -> None:
        # dispatch 前的 RUNNING Step/Attempt 与 outcome 后的
        # SUCCEEDED 记录必须使用同一稳定 identity，而不是追加第二份记录。
        store = self.make_store()
        await store.create_run(created_record())
        await store.record_step(
            StepRecord(
                step_id="tool-step-1",
                run_id="run-1",
                step_type=StepType.TOOL,
                status=StepStatus.RUNNING,
            ),
            expected_version=1,
        )
        await store.record_attempt(
            StepAttempt(
                attempt_id="tool-attempt-1",
                step_id="tool-step-1",
                run_id="run-1",
                status=StepStatus.RUNNING,
            ),
            expected_version=1,
        )
        await store.record_step(
            StepRecord(
                step_id="tool-step-1",
                run_id="run-1",
                step_type=StepType.TOOL,
                status=StepStatus.SUCCEEDED,
            ),
            expected_version=1,
        )
        await store.record_attempt(
            StepAttempt(
                attempt_id="tool-attempt-1",
                step_id="tool-step-1",
                run_id="run-1",
                status=StepStatus.SUCCEEDED,
                output="structured outcome",
            ),
            expected_version=1,
        )
        await store.record_checkpoint(
            StepCheckpoint(
                run_id="run-1",
                step_id="tool-step-1",
                attempt_id="tool-attempt-1",
                step_type=StepType.TOOL,
                output="structured outcome",
            ),
            expected_version=1,
        )

        steps = await store.get_steps("run-1")
        attempts = await store.get_attempts("run-1")
        checkpoints = await store.get_checkpoints("run-1")
        self.assertEqual(
            [(step.step_id, step.status) for step in steps],
            [("tool-step-1", StepStatus.SUCCEEDED)],
        )
        self.assertEqual(
            [
                (attempt.attempt_id, attempt.status, attempt.output)
                for attempt in attempts
            ],
            [("tool-attempt-1", StepStatus.SUCCEEDED, "structured outcome")],
        )
        self.assertEqual(checkpoints[0].attempt_id, attempts[0].attempt_id)

    async def test_record_step_rejects_stale_expected_version(self) -> None:
        store = self.make_store()
        created = await store.create_run(created_record())
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )
        step = StepRecord(
            step_id="step-stale",
            run_id="run-1",
            step_type=StepType.MODEL,
            status=StepStatus.SUCCEEDED,
        )

        with self.assertRaises(StaleRunVersionError):
            await store.record_step(
                step,
                expected_version=created.version,
            )

        self.assertEqual(await store.get_steps("run-1"), [])

    async def test_record_attempt_rejects_stale_expected_version(self) -> None:
        store = self.make_store()
        created = await store.create_run(created_record())
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )
        attempt = StepAttempt(
            attempt_id="attempt-stale",
            step_id="step-1",
            run_id="run-1",
            status=StepStatus.SUCCEEDED,
            output="must-not-persist",
        )

        with self.assertRaises(StaleRunVersionError):
            await store.record_attempt(
                attempt,
                expected_version=created.version,
            )

        self.assertEqual(await store.get_attempts("run-1"), [])

    async def test_record_checkpoint_rejects_stale_expected_version(self) -> None:
        store = self.make_store()
        created = await store.create_run(created_record())
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )
        checkpoint = StepCheckpoint(
            run_id="run-1",
            step_id="step-stale",
            attempt_id="attempt-stale",
            output="must-not-persist",
        )

        with self.assertRaises(StaleRunVersionError):
            await store.record_checkpoint(
                checkpoint,
                expected_version=created.version,
            )

        self.assertEqual(await store.get_checkpoints("run-1"), [])

    async def test_failed_attempt_classification_roundtrip(self) -> None:
        # 失败 Attempt 的分类 / 错误标识 / 时间证据作为
        # metadata 持久化，InMemory 与 SQLite 都必须在 roundtrip 后
        # 原样还原（分类绝不依赖异常消息字符串）。
        store = self.make_store()
        await store.create_run(created_record())
        failed = StepAttempt(
            attempt_id="attempt-fail",
            step_id="step-1",
            run_id="run-1",
            status=StepStatus.FAILED,
            error="upstream timed out",
            classification=FailureClassification.TRANSIENT,
            error_code="rate_limited",
        )
        await store.record_attempt(failed, expected_version=1)
        succeeded = StepAttempt(
            attempt_id="attempt-ok",
            step_id="step-1",
            run_id="run-1",
            status=StepStatus.SUCCEEDED,
            output="answer",
        )
        await store.record_attempt(succeeded, expected_version=1)

        attempts = await store.get_attempts("run-1")
        by_id = {a.attempt_id: a for a in attempts}
        self.assertEqual(
            by_id["attempt-fail"].classification,
            FailureClassification.TRANSIENT,
        )
        self.assertEqual(by_id["attempt-fail"].error_code, "rate_limited")
        self.assertEqual(by_id["attempt-fail"].error, "upstream timed out")
        # 成功 Attempt 不携带失败分类字段。
        self.assertIsNone(by_id["attempt-ok"].classification)
        self.assertIsNone(by_id["attempt-ok"].error_code)

    async def test_checkpoint_step_type_roundtrip(self) -> None:
        # checkpoint 携带 step_type（MODEL / CONTEXT），
        # 恢复时据此区分已确认的 Context 与 Model Step。
        store = self.make_store()
        await store.create_run(created_record())
        model_ckpt = StepCheckpoint(
            run_id="run-1",
            step_id="step-model",
            attempt_id="attempt-model",
            step_type=StepType.MODEL,
            output="model output",
        )
        context_ckpt = StepCheckpoint(
            run_id="run-1",
            step_id="step-context",
            attempt_id="attempt-context",
            step_type=StepType.CONTEXT,
            output="context output",
        )
        await store.record_checkpoint(model_ckpt, expected_version=1)
        await store.record_checkpoint(context_ckpt, expected_version=1)

        checkpoints = await store.get_checkpoints("run-1")
        by_step = {c.step_id: c for c in checkpoints}
        self.assertEqual(by_step["step-model"].step_type, StepType.MODEL)
        self.assertEqual(by_step["step-context"].step_type, StepType.CONTEXT)
        self.assertEqual(by_step["step-context"].output, "context output")

    async def test_run_enters_waiting_with_machine_readable_reason(
        self,
    ) -> None:
        # RUNNING -> WAITING 合法，waiting_reason 持久化。
        store = self.make_store()
        created = await store.create_run(created_record())
        running = await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
            snapshot=snapshot(),
        )
        waiting = await store.transition_run(
            "run-1",
            expected_version=running.version,
            status=RunStatus.WAITING,
            waiting_reason="DEFINITION_UNAVAILABLE",
        )
        self.assertEqual(waiting.status, RunStatus.WAITING)
        self.assertEqual(waiting.waiting_reason, "DEFINITION_UNAVAILABLE")
        self.assertFalse(waiting.status.is_terminal)

    async def test_payload_roundtrips_only_through_codec(self) -> None:
        # 内容字段（input/output/checkpoint）只经 Codec 往返：
        # 每次写入都会 encode，每次读取都会 decode，且持久化的原始
        # 字节不等于明文（plaintext codec 带前缀标记）。
        codec = SpyCodec(PlaintextPayloadCodec())
        store = self.make_store(codec=codec)
        created = await store.create_run(created_record())
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )
        running = await store.get_run("run-1")
        succeeded = await store.transition_run(
            "run-1",
            expected_version=running.version,
            status=RunStatus.SUCCEEDED,
            output="final output",
        )
        attempt = StepAttempt(
            attempt_id="attempt-1",
            step_id="step-1",
            run_id="run-1",
            status=StepStatus.SUCCEEDED,
            output="step output",
        )
        await store.record_attempt(
            attempt, expected_version=succeeded.version
        )
        checkpoint = StepCheckpoint(
            run_id="run-1",
            step_id="step-1",
            attempt_id="attempt-1",
            output="step output",
        )
        await store.record_checkpoint(
            checkpoint, expected_version=succeeded.version
        )

        # 写入路径：input / output / attempt output / checkpoint output
        # 都经过 encode，且明文内容不在持久化字节里出现。
        self.assertIn("hi", codec.encoded)
        self.assertIn("final output", codec.encoded)
        self.assertIn("step output", codec.encoded)
        raw_input = store.raw_payload_bytes("run-1", "run:input")
        self.assertIsNotNone(raw_input)
        self.assertNotEqual(raw_input, b"hi")
        self.assertEqual(raw_input, PlaintextPayloadCodec().encode("hi"))

        # 读取路径：get_run / get_checkpoints 都经 decode 还原。
        final = await store.get_run("run-1")
        self.assertEqual(final.output, "final output")
        checkpoints = await store.get_checkpoints("run-1")
        self.assertEqual(checkpoints[0].output, "step output")
        self.assertGreaterEqual(len(codec.decoded), 3)

    # -- Run Lease 契约（ADR 0013） -----------------------------------

    async def test_acquire_lease_is_persisted_and_visible(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        await store.create_run(created_record())
        lease = await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        self.assertEqual(lease.run_id, "run-1")
        self.assertEqual(lease.owner, "owner-a")
        self.assertEqual(lease.expires_at, clock.now() + DEFAULT_LEASE_TTL)

        # 租约 owner / 期限通过 get_lease 与 Run 记录都可验证。
        stored = await store.get_lease("run-1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.owner, "owner-a")
        record = await store.get_run("run-1")
        self.assertEqual(record.lease_owner, "owner-a")
        self.assertEqual(record.lease_expires_at, lease.expires_at)
        # 获取租约不改变 Run 的乐观版本号。
        self.assertEqual(record.version, 1)

    async def test_acquire_lease_rejects_stale_expected_version(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        created = await store.create_run(created_record())
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
        )

        with self.assertRaises(StaleRunVersionError):
            await store.acquire_lease(
                "run-1",
                "owner-a",
                DEFAULT_LEASE_TTL,
                expected_version=created.version,
            )

        self.assertIsNone(await store.get_lease("run-1"))

    async def test_second_owner_rejected_while_lease_active(self) -> None:
        store = self.make_store(clock=FakeClock())
        await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        with self.assertRaises(LeaseNotHeldError):
            await store.acquire_lease(
                "run-1", "owner-b", DEFAULT_LEASE_TTL, expected_version=1
            )
        # 权威租约未被改动。
        lease = await store.get_lease("run-1")
        self.assertEqual(lease.owner, "owner-a")

    async def test_same_owner_renews_lease(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        await store.create_run(created_record())
        first = await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        clock.advance(timedelta(seconds=10))
        renewed = await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        self.assertEqual(renewed.owner, "owner-a")
        self.assertGreater(renewed.expires_at, first.expires_at)

    async def test_expired_lease_allows_takeover(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        # 推进时钟越过租约过期点：其他 owner 才能接管。
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        lease = await store.acquire_lease(
            "run-1", "owner-b", DEFAULT_LEASE_TTL, expected_version=1
        )
        self.assertEqual(lease.owner, "owner-b")

    async def test_mutation_without_active_lease_rejected(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        created = await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        step = StepRecord(
            step_id="step-1",
            run_id="run-1",
            step_type=StepType.MODEL,
            status=StepStatus.SUCCEEDED,
        )
        # 其他 owner（owner-b）没有租约：任何权威写入都被拒绝。
        with self.assertRaises(LeaseNotHeldError):
            await store.record_step(
                step,
                expected_version=created.version,
                lease_owner="owner-b",
            )
        with self.assertRaises(LeaseNotHeldError):
            await store.transition_run(
                "run-1",
                expected_version=created.version,
                status=RunStatus.RUNNING,
                lease_owner="owner-b",
            )
        # 权威状态未被改动。
        record = await store.get_run("run-1")
        self.assertEqual(record.status, RunStatus.CREATED)
        self.assertEqual(record.version, 1)
        self.assertEqual(await store.get_steps("run-1"), [])

    async def test_expired_lease_cannot_commit_even_as_former_owner(
        self,
    ) -> None:
        # 旧 owner 的租约过期后，即使 owner 名匹配也不允许再提交
        # （lease_expires_at 已过），防止接管后旧 owner 迟到写入。
        clock = FakeClock()
        store = self.make_store(clock=clock)
        created = await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        step = StepRecord(
            step_id="step-1",
            run_id="run-1",
            step_type=StepType.MODEL,
            status=StepStatus.SUCCEEDED,
        )
        with self.assertRaises(LeaseNotHeldError):
            await store.record_step(
                step,
                expected_version=created.version,
                lease_owner="owner-a",
            )
        with self.assertRaises(LeaseNotHeldError):
            await store.transition_run(
                "run-1",
                expected_version=created.version,
                status=RunStatus.RUNNING,
                lease_owner="owner-a",
            )

    async def test_stale_owner_cannot_commit_after_takeover(self) -> None:
        clock = FakeClock()
        store = self.make_store(clock=clock)
        created = await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        clock.advance(DEFAULT_LEASE_TTL + timedelta(seconds=1))
        # B 接管并推进状态。
        await store.acquire_lease(
            "run-1", "owner-b", DEFAULT_LEASE_TTL, expected_version=1
        )
        running = await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
            snapshot=snapshot(),
            lease_owner="owner-b",
        )
        # A 迟到提交：owner 不匹配，权威记录不被改动。
        with self.assertRaises(LeaseNotHeldError):
            await store.transition_run(
                "run-1",
                expected_version=running.version,
                status=RunStatus.SUCCEEDED,
                output="stale output",
                lease_owner="owner-a",
            )
        final = await store.get_run("run-1")
        self.assertEqual(final.status, RunStatus.RUNNING)
        self.assertEqual(final.version, 2)
        self.assertEqual(final.output, None)
        # A 也不能释放 B 的租约。
        with self.assertRaises(LeaseNotHeldError):
            await store.release_lease(
                "run-1", "owner-a", expected_version=final.version
            )

    async def test_held_lease_stale_version_still_rejected(self) -> None:
        # 版本检查与租约检查互相独立：持有有效租约但基于过期版本号的
        # 提交仍然被拒绝（乐观版本控制不被租约绕过）。
        clock = FakeClock()
        store = self.make_store(clock=clock)
        created = await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
            snapshot=snapshot(),
            lease_owner="owner-a",
        )
        with self.assertRaises(StaleRunVersionError):
            await store.transition_run(
                "run-1",
                expected_version=created.version,
                status=RunStatus.SUCCEEDED,
                lease_owner="owner-a",
            )

    async def test_release_lease_clears_ownership(self) -> None:
        store = self.make_store(clock=FakeClock())
        await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        await store.release_lease(
            "run-1", "owner-a", expected_version=1
        )
        self.assertIsNone(await store.get_lease("run-1"))
        # 释放后其他 owner 立即可获取。
        lease = await store.acquire_lease(
            "run-1", "owner-b", DEFAULT_LEASE_TTL, expected_version=1
        )
        self.assertEqual(lease.owner, "owner-b")

    async def test_release_lease_rejects_stale_expected_version(self) -> None:
        store = self.make_store(clock=FakeClock())
        created = await store.create_run(created_record())
        await store.acquire_lease(
            "run-1",
            "owner-a",
            DEFAULT_LEASE_TTL,
            expected_version=created.version,
        )
        await store.transition_run(
            "run-1",
            expected_version=created.version,
            status=RunStatus.RUNNING,
            lease_owner="owner-a",
        )

        with self.assertRaises(StaleRunVersionError):
            await store.release_lease(
                "run-1", "owner-a", expected_version=created.version
            )

        self.assertEqual((await store.get_lease("run-1")).owner, "owner-a")

    async def test_release_lease_wrong_owner_rejected(self) -> None:
        store = self.make_store(clock=FakeClock())
        await store.create_run(created_record())
        await store.acquire_lease(
            "run-1", "owner-a", DEFAULT_LEASE_TTL, expected_version=1
        )
        with self.assertRaises(LeaseNotHeldError):
            await store.release_lease(
                "run-1", "owner-b", expected_version=1
            )
        self.assertEqual((await store.get_lease("run-1")).owner, "owner-a")
