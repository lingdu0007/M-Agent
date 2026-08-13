"""RunStore 协议与 InMemoryRunStore 实现。

ADR 0006：Run Store 是持久化权威 Run Status、Run Step 与 Checkpoint
的边界，恢复只读取 Run Store。
ADR 0033：可查询的 Run Metadata 与 Run Payload 分离，Payload 只能经
上层应用显式配置的 :class:`PayloadCodec` 读写——本模块的内部存储
布局（内存 dict 与 SQLite 表）在两种实现中保持一致：
metadata 不含任何内容字段，内容一律以编码字节存入 payload 区。
PRD：InMemoryRunStore 用于确定性测试与本地实验；测试断言只通过
公开 Runner 与检查 API 驱动，本模块的协议是唯一低层补充边界。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from ._clock import Clock, SystemClock
from ._codec import PayloadCodec
from ._definition import DefinitionSnapshot
from ._failure import sanitize_error_code
from ._errors import (
    DuplicateRunError,
    LeaseNotHeldError,
    RunNotFoundError,
    StaleRunVersionError,
)
from ._run import RunRecord
from ._status import RunStatus, validate_transition
from ._steps import (
    FailureClassification,
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepStatus,
    utc_now,
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


class InMemoryRunStore:
    """进程内 RunStore，用于确定性测试与本地实验。

    Metadata 与编码后的 Payload 分离存储：``_payloads`` 只保存经
    :class:`PayloadCodec` 编码的字节，任何内容字段都不以明文形式
    出现在 metadata 区。状态转换做集中合法性校验
    （IllegalRunTransitionError），状态更新做乐观版本控制
    （StaleRunVersionError），失败均不改动权威记录。
    """

    def __init__(
        self,
        payload_codec: PayloadCodec,
        clock: Clock | None = None,
    ) -> None:
        self._codec = payload_codec
        self._clock = clock if clock is not None else SystemClock()
        self._runs: dict[str, _StoredRun] = {}
        self._payloads: dict[tuple[str, str], bytes] = {}
        self._steps: dict[str, list[StepRecord]] = {}
        self._attempts: dict[str, list[_StoredAttempt]] = {}
        self._checkpoints: dict[str, list[_StoredCheckpoint]] = {}

    # -- Run Lease ----------------------------------------------------

    async def acquire_lease(
        self,
        run_id: str,
        owner: str,
        ttl: timedelta,
        *,
        expected_version: int,
    ) -> RunLease:
        """排他获取或续约 Run Lease。

        无有效租约 / 已过期 / 原 owner 续约时成功并原子更新持久状态；
        其他 owner 持有 active lease 时抛 :class:`LeaseNotHeldError`。
        """
        current = self._runs.get(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale lease acquisition for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; lease was not mutated"
            )
        now = self._clock.now()
        if (
            current.lease_owner is not None
            and current.lease_owner != owner
            and current.lease_expires_at is not None
            and current.lease_expires_at > now
        ):
            raise LeaseNotHeldError(
                f"cannot acquire lease for run {run_id}: held by "
                f"{current.lease_owner!r} until {current.lease_expires_at}"
            )
        expires_at = now + ttl
        self._runs[run_id] = replace(
            current, lease_owner=owner, lease_expires_at=expires_at
        )
        return RunLease(run_id=run_id, owner=owner, expires_at=expires_at)

    async def release_lease(
        self, run_id: str, owner: str, *, expected_version: int
    ) -> None:
        """owner 匹配时清除租约；不匹配抛 :class:`LeaseNotHeldError`。"""
        current = self._runs.get(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale lease release for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; lease was not mutated"
            )
        if current.lease_owner != owner:
            raise LeaseNotHeldError(
                f"cannot release lease for run {run_id}: owner is "
                f"{current.lease_owner!r}, not {owner!r}"
            )
        self._runs[run_id] = replace(
            current, lease_owner=None, lease_expires_at=None
        )

    async def get_lease(self, run_id: str) -> RunLease | None:
        stored = self._runs.get(run_id)
        if (
            stored is None
            or stored.lease_owner is None
            or stored.lease_expires_at is None
        ):
            return None
        return RunLease(
            run_id=run_id,
            owner=stored.lease_owner,
            expires_at=stored.lease_expires_at,
        )

    async def assert_lease(
        self, run_id: str, expected_version: int, owner: str
    ) -> None:
        """校验 Step dispatch 使用的权威版本与有效租约，不做续租。"""
        current = self._runs.get(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale dispatch for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; no step was started"
            )
        self._check_lease(current, owner)

    def _check_lease(self, stored: _StoredRun, owner: str) -> None:
        """owner 必须持有未过期租约，否则抛 :class:`LeaseNotHeldError`。"""
        now = self._clock.now()
        if (
            stored.lease_owner != owner
            or stored.lease_expires_at is None
            or stored.lease_expires_at <= now
        ):
            raise LeaseNotHeldError(
                f"run {stored.run_id} lease is not held by {owner!r} "
                f"(owner={stored.lease_owner!r}, "
                f"expires={stored.lease_expires_at}, now={now})"
            )

    # -- Run 生命周期 -------------------------------------------------

    async def create_run(self, run: RunRecord) -> RunRecord:
        if run.run_id in self._runs:
            raise DuplicateRunError(f"run {run.run_id} already exists")
        stored, payloads = _split_run(run, self._codec)
        self._runs[run.run_id] = stored
        for field, encoded in payloads.items():
            self._payloads[(run.run_id, field)] = encoded
        return stored.to_record(
            input=self._codec.decode(payloads[FIELD_RUN_INPUT]),
            output=(
                self._codec.decode(payloads[FIELD_RUN_OUTPUT])
                if FIELD_RUN_OUTPUT in payloads
                else None
            ),
            snapshot=_restore_snapshot(
                stored.snapshot,
                self._codec.decode(payloads[FIELD_RUN_SNAPSHOT])
                if FIELD_RUN_SNAPSHOT in payloads
                else None,
            ),
        )

    async def get_run(self, run_id: str) -> RunRecord | None:
        stored = self._runs.get(run_id)
        if stored is None:
            return None
        return stored.to_record(
            input=self._read_payload(run_id, FIELD_RUN_INPUT),
            output=self._read_payload(run_id, FIELD_RUN_OUTPUT),
            snapshot=_restore_snapshot(
                stored.snapshot,
                self._read_payload(run_id, FIELD_RUN_SNAPSHOT),
            ),
        )

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
        lease_owner: str | None = None,
    ) -> RunRecord:
        current = self._runs.get(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale update for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; run record was not mutated"
            )
        if lease_owner is not None:
            self._check_lease(current, lease_owner)
        validate_transition(current.status, status)
        # WAITING 字段只在 WAITING 状态有效（Ticket 07）：转入 WAITING
        # 用参数写入 reason / 目标 Step；转出 WAITING（RUNNING / 终态）
        # 一律清空，保证权威记录里不会残留过期目标。
        if status is RunStatus.WAITING:
            new_waiting_reason = waiting_reason
            new_waiting_step_id = waiting_step_id
        else:
            new_waiting_reason = None
            new_waiting_step_id = None
        updated = _StoredRun(
            run_id=current.run_id,
            definition_id=current.definition_id,
            definition_version=current.definition_version,
            status=status,
            snapshot=(
                _snapshot_metadata(snapshot)
                if snapshot is not None
                else current.snapshot
            ),
            version=current.version + 1,
            waiting_reason=new_waiting_reason,
            waiting_step_id=new_waiting_step_id,
            lease_owner=current.lease_owner,
            lease_expires_at=current.lease_expires_at,
            created_at=current.created_at,
            updated_at=utc_now(),
        )
        self._runs[run_id] = updated
        if output is not None:
            self._payloads[(run_id, FIELD_RUN_OUTPUT)] = self._codec.encode(
                output
            )
        if snapshot is not None:
            self._payloads[(run_id, FIELD_RUN_SNAPSHOT)] = self._codec.encode(
                snapshot.model_dump_json()
            )
        return updated.to_record(
            input=self._read_payload(run_id, FIELD_RUN_INPUT),
            output=self._read_payload(run_id, FIELD_RUN_OUTPUT),
            snapshot=_restore_snapshot(
                updated.snapshot,
                self._read_payload(run_id, FIELD_RUN_SNAPSHOT),
            ),
        )

    # -- Step 记录 ----------------------------------------------------

    async def record_step(
        self,
        step: StepRecord,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepRecord:
        current = self._runs.get(step.run_id)
        if current is None:
            raise RunNotFoundError(f"run {step.run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale Step mutation for run {step.run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; Step was not recorded"
            )
        if lease_owner is not None:
            self._check_lease(current, lease_owner)
        stored_step = step.model_copy(deep=True)
        steps = self._steps.setdefault(step.run_id, [])
        for index, existing in enumerate(steps):
            if existing.step_id == step.step_id:
                steps[index] = stored_step
                break
        else:
            steps.append(stored_step)
        return stored_step

    async def record_attempt(
        self,
        attempt: StepAttempt,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepAttempt:
        current = self._runs.get(attempt.run_id)
        if current is None:
            raise RunNotFoundError(f"run {attempt.run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale Attempt mutation for run {attempt.run_id}: expected "
                f"version {expected_version}, authoritative version is "
                f"{current.version}; Attempt was not recorded"
            )
        if lease_owner is not None:
            self._check_lease(current, lease_owner)
        stored, payloads = _split_attempt(attempt, self._codec)
        attempts = self._attempts.setdefault(attempt.run_id, [])
        for index, existing in enumerate(attempts):
            if existing.attempt_id == attempt.attempt_id:
                attempts[index] = stored
                break
        else:
            attempts.append(stored)
        for field, encoded in payloads.items():
            self._payloads[(attempt.run_id, field)] = encoded
        return stored.to_record(output=attempt.output, error=attempt.error)

    async def record_checkpoint(
        self,
        checkpoint: StepCheckpoint,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepCheckpoint:
        current = self._runs.get(checkpoint.run_id)
        if current is None:
            raise RunNotFoundError(f"run {checkpoint.run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale Checkpoint mutation for run {checkpoint.run_id}: "
                f"expected version {expected_version}, authoritative version "
                f"is {current.version}; Checkpoint was not recorded"
            )
        if lease_owner is not None:
            self._check_lease(current, lease_owner)
        stored, payloads = _split_checkpoint(checkpoint, self._codec)
        self._checkpoints.setdefault(checkpoint.run_id, []).append(stored)
        for field, encoded in payloads.items():
            self._payloads[(checkpoint.run_id, field)] = encoded
        return stored.to_record(output=checkpoint.output)

    async def get_steps(self, run_id: str) -> list[StepRecord]:
        return [s.model_copy(deep=True) for s in self._steps.get(run_id, [])]

    async def get_attempts(self, run_id: str) -> list[StepAttempt]:
        return [
            a.to_record(
                output=self._read_payload(
                    run_id, attempt_output_field(a.attempt_id)
                ),
                error=self._read_payload(
                    run_id, attempt_error_field(a.attempt_id)
                ),
            )
            for a in self._attempts.get(run_id, [])
        ]

    async def get_checkpoints(self, run_id: str) -> list[StepCheckpoint]:
        return [
            c.to_record(
                output=self._read_payload(
                    run_id, checkpoint_output_field(c.step_id)
                )
            )
            for c in self._checkpoints.get(run_id, [])
        ]

    # -- 内部 payload 读取 -------------------------------------------

    def _read_payload(self, run_id: str, field: str) -> str | None:
        encoded = self._payloads.get((run_id, field))
        if encoded is None:
            return None
        return self._codec.decode(encoded)

    def raw_payload_bytes(self, run_id: str, field: str) -> bytes | None:
        """返回未经解码的编码字节（仅测试用：验证确实经过 Codec）。"""
        return self._payloads.get((run_id, field))
