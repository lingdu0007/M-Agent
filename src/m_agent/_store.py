"""Runtime Core RunStore protocol and shared serialization primitives.

ADR 0006：Run Store 是持久化权威 Run Status、Run Step 与 Checkpoint
的边界，恢复只读取 Run Store。
ADR 0033：可查询的 Run Metadata 与 Run Payload 分离，Payload 只能经
上层应用显式配置的 :class:`PayloadCodec` 读写——本模块的内部存储
布局由 Adapter 实现保持一致：
metadata 不含任何内容字段，内容一律以编码字节存入 payload 区。
具体的内存与 SQLite Adapter 位于 :mod:`m_agent.adapters`；本模块只保留
Runtime Core 所需的协议、Lease 与与内容无关的共享序列化逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from ._codec import PayloadCodec
from ._definition import DefinitionSnapshot
from ._failure import sanitize_error_code
from ._run import RunRecord
from ._model import ModelPurpose
from ._status import RunStatus
from ._steps import (
    FailureClassification,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
)

#: Payload 区内字段名（metadata 与 payload 的固定拆分键）。
FIELD_RUN_INPUT = "run:input"
FIELD_RUN_OUTPUT = "run:output"
FIELD_RUN_SNAPSHOT = "run:snapshot"


def attempt_output_field(attempt_id: str) -> str:
    return f"attempt:{attempt_id}:output"


def attempt_error_field(attempt_id: str) -> str:
    return f"attempt:{attempt_id}:error"


def checkpoint_output_field(step_id: str) -> str:
    return f"checkpoint:{step_id}:output"


@dataclass(frozen=True)
class RunLease:
    """Run Store 授予单个 Runner 的排他推进权（ADR 0013）。

    持有者在 ``expires_at`` 之前可以推进该 Run；过期后其他 Runner
    才能接管。owner / 期限作为持久状态保存在 RunStore 中。
    """

    run_id: str
    owner: str
    expires_at: datetime


@dataclass(frozen=True)
class _StoredRun:
    """Run 的 metadata 视图：不含 input/output/snapshot 内容字段。"""

    run_id: str
    definition_id: str
    definition_version: str
    status: RunStatus
    snapshot: DefinitionSnapshot | None
    version: int
    waiting_reason: str | None
    waiting_step_id: str | None
    error_code: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def to_record(
        self,
        input: str,
        output: str | None,
        snapshot: DefinitionSnapshot | None,
    ) -> RunRecord:
        return RunRecord(
            run_id=self.run_id,
            definition_id=self.definition_id,
            definition_version=self.definition_version,
            input=input,
            status=self.status,
            snapshot=snapshot,
            output=output,
            waiting_reason=self.waiting_reason,
            waiting_step_id=self.waiting_step_id,
            error_code=self.error_code,
            version=self.version,
            lease_owner=self.lease_owner,
            lease_expires_at=self.lease_expires_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True)
class _StoredAttempt:
    """StepAttempt 的 metadata 视图：不含 output/error 内容字段。"""

    attempt_id: str
    step_id: str
    run_id: str
    status: StepStatus
    error: str | None
    classification: str | None
    error_code: str | None
    model_purpose: str | None
    created_at: datetime

    def to_record(self, output: str | None, error: str | None) -> StepAttempt:
        return StepAttempt(
            attempt_id=self.attempt_id,
            step_id=self.step_id,
            run_id=self.run_id,
            status=self.status,
            output=output,
            error=error,
            classification=(
                FailureClassification(self.classification)
                if self.classification is not None
                else None
            ),
            error_code=self.error_code,
            model_purpose=(
                ModelPurpose(self.model_purpose)
                if self.model_purpose is not None
                else None
            ),
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class _StoredCheckpoint:
    """StepCheckpoint 的 metadata 视图：不含 output 内容字段。"""

    run_id: str
    step_id: str
    attempt_id: str
    step_type: StepType
    created_at: datetime

    def to_record(self, output: str) -> StepCheckpoint:
        return StepCheckpoint(
            run_id=self.run_id,
            step_id=self.step_id,
            attempt_id=self.attempt_id,
            step_type=self.step_type,
            output=output,
            created_at=self.created_at,
        )


@runtime_checkable
class RunStore(Protocol):
    """Run Store 行为契约：权威状态、Step 记录、Run Lease 与乐观版本控制。

    Payload 内容（input / output / checkpoint 内容）只通过本 Store
    配置的 :class:`PayloadCodec` 存取；调用方看到的一律是解码后的
    字符串，本协议不暴露编码字节。

    租约原语（ADR 0013）：``acquire_lease`` 是排他的——只有无有效
    租约或原 owner 续约时成功。Lease、Step、Attempt、Checkpoint 与
    Run transition 的每个权威 mutation 都必须携带 ``expected_version``；
    Runner 写入还会原子校验调用方仍持有**未过期**的租约。dispatch 前
    的 ``assert_lease`` 执行相同 guard，杜绝过期 owner 启动新 Step。
    """

    async def create_run(self, run: RunRecord) -> RunRecord: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...

    async def acquire_lease(
        self,
        run_id: str,
        owner: str,
        ttl: timedelta,
        *,
        expected_version: int,
    ) -> RunLease: ...

    async def release_lease(
        self, run_id: str, owner: str, *, expected_version: int
    ) -> None: ...

    async def get_lease(self, run_id: str) -> RunLease | None: ...

    async def assert_lease(
        self, run_id: str, expected_version: int, owner: str
    ) -> None: ...

    async def transition_run(
        self,
        run_id: str,
        expected_version: int,
        *,
        status: RunStatus,
        snapshot: DefinitionSnapshot | None = None,
        output: str | None = None,
        waiting_reason: str | None = None,
        waiting_step_id: str | None = None,
        error_code: str | None = None,
        lease_owner: str | None = None,
    ) -> RunRecord: ...

    async def record_step(
        self,
        step: StepRecord,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepRecord: ...

    async def record_attempt(
        self,
        attempt: StepAttempt,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepAttempt: ...

    async def record_checkpoint(
        self,
        checkpoint: StepCheckpoint,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepCheckpoint: ...

    async def get_steps(self, run_id: str) -> list[StepRecord]: ...

    async def get_attempts(self, run_id: str) -> list[StepAttempt]: ...

    async def get_checkpoints(self, run_id: str) -> list[StepCheckpoint]: ...


def _split_run(run: RunRecord, codec: PayloadCodec) -> tuple[_StoredRun, dict[str, bytes]]:
    """把公共 RunRecord 拆成 metadata 视图 + 编码后的 payload 区。"""
    payloads: dict[str, bytes] = {FIELD_RUN_INPUT: codec.encode(run.input)}
    if run.output is not None:
        payloads[FIELD_RUN_OUTPUT] = codec.encode(run.output)
    if run.snapshot is not None:
        payloads[FIELD_RUN_SNAPSHOT] = codec.encode(run.snapshot.model_dump_json())
    stored = _StoredRun(
        run_id=run.run_id,
        definition_id=run.definition_id,
        definition_version=run.definition_version,
        status=run.status,
        snapshot=_snapshot_metadata(run.snapshot),
        version=run.version,
        waiting_reason=run.waiting_reason,
        waiting_step_id=run.waiting_step_id,
        error_code=run.error_code,
        lease_owner=run.lease_owner,
        lease_expires_at=run.lease_expires_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
    return stored, payloads


def _split_attempt(
    attempt: StepAttempt, codec: PayloadCodec
) -> tuple[_StoredAttempt, dict[str, bytes]]:
    payloads: dict[str, bytes] = {}
    if attempt.output is not None:
        payloads[attempt_output_field(attempt.attempt_id)] = codec.encode(
            attempt.output
        )
    if attempt.error is not None:
        payloads[attempt_error_field(attempt.attempt_id)] = codec.encode(
            attempt.error
        )
    stored = _StoredAttempt(
        attempt_id=attempt.attempt_id,
        step_id=attempt.step_id,
        run_id=attempt.run_id,
        status=attempt.status,
        error=None,
        classification=(
            attempt.classification.value
            if attempt.classification is not None
            else None
        ),
        error_code=(
            sanitize_error_code(attempt.error_code)
            if attempt.error_code is not None
            else None
        ),
        model_purpose=(
            attempt.model_purpose.value
            if attempt.model_purpose is not None
            else None
        ),
        created_at=attempt.created_at,
    )
    return stored, payloads


def _snapshot_metadata(
    snapshot: DefinitionSnapshot | None,
) -> DefinitionSnapshot | None:
    """保留可查询快照字段，排除必须受保护的 Agent Instruction。"""
    if snapshot is None:
        return None
    return snapshot.model_copy(update={"instructions": ""})


def _snapshot_metadata_json(snapshot: DefinitionSnapshot | None) -> str | None:
    """序列化可查询快照字段，绝不在 metadata JSON 中写入 instruction。"""
    if snapshot is None:
        return None
    return snapshot.model_dump_json(exclude={"instructions"})


def _restore_snapshot(
    metadata_snapshot: DefinitionSnapshot | None,
    payload: str | None,
) -> DefinitionSnapshot | None:
    """用受保护 payload 还原完整 Definition Snapshot。"""
    if metadata_snapshot is None:
        return None
    if payload is None:
        raise ValueError("run metadata references a missing snapshot payload")
    return DefinitionSnapshot.model_validate_json(payload)


def _split_checkpoint(
    checkpoint: StepCheckpoint, codec: PayloadCodec
) -> tuple[_StoredCheckpoint, dict[str, bytes]]:
    payloads: dict[str, bytes] = {
        checkpoint_output_field(checkpoint.step_id): codec.encode(
            checkpoint.output
        )
    }
    stored = _StoredCheckpoint(
        run_id=checkpoint.run_id,
        step_id=checkpoint.step_id,
        attempt_id=checkpoint.attempt_id,
        step_type=checkpoint.step_type,
        created_at=checkpoint.created_at,
    )
    return stored, payloads
