"""In-memory implementation of the Runtime Core :class:`RunStore` port."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

from .._clock import Clock, SystemClock
from .._codec import PayloadCodec
from .._definition import DefinitionSnapshot
from .._errors import (
    DuplicateRunError,
    LeaseNotHeldError,
    RunNotFoundError,
    StaleRunVersionError,
)
from .._run import RunRecord
from .._status import RunStatus, validate_transition
from .._steps import StepAttempt, StepCheckpoint, StepRecord, StepType, utc_now
from .._policy import PolicyDecisionRecord
from .._store import (
    FIELD_RUN_INPUT,
    FIELD_RUN_OUTPUT,
    FIELD_RUN_SNAPSHOT,
    RunLease,
    _StoredAttempt,
    _StoredCheckpoint,
    _StoredRun,
    _restore_snapshot,
    _snapshot_metadata,
    _split_attempt,
    _split_checkpoint,
    _split_run,
    attempt_error_field,
    attempt_output_field,
    checkpoint_output_field,
)


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
        self._policy_decisions: dict[str, list[PolicyDecisionRecord]] = {}

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

    async def prepare_model_dispatch(
        self,
        run_id: str,
        expected_version: int,
        owner: str,
        *,
        guard: Callable[[], None],
    ) -> None:
        """Validate a Model binding, then its lease, without an await gap."""
        current = self._runs.get(run_id)
        if current is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale dispatch for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; no step was started"
            )
        guard()
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
        error_code: str | None = None,
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
            error_code=error_code,
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

    async def reserve_model_attempt(
        self,
        step: StepRecord,
        attempt: StepAttempt,
        *,
        run_max_attempts: int | None,
        purpose_max_attempts: int | None,
        expected_version: int,
        lease_owner: str,
    ) -> bool:
        if (
            step.step_type is not StepType.MODEL
            or step.run_id != attempt.run_id
            or step.step_id != attempt.step_id
            or attempt.model_purpose is None
        ):
            raise ValueError("model reservation requires one MODEL StepAttempt")
        current = self._runs.get(attempt.run_id)
        if current is None:
            raise RunNotFoundError(f"run {attempt.run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale model reservation for run {attempt.run_id}: expected "
                f"version {expected_version}, authoritative version is "
                f"{current.version}; model attempt was not reserved"
            )
        self._check_lease(current, lease_owner)
        attempts = self._attempts.setdefault(attempt.run_id, [])
        model_attempts = [
            stored for stored in attempts if stored.model_purpose is not None
        ]
        purpose_attempts = [
            stored
            for stored in model_attempts
            if stored.model_purpose == attempt.model_purpose.value
        ]
        if (
            run_max_attempts is not None
            and len(model_attempts) >= run_max_attempts
        ) or (
            purpose_max_attempts is not None
            and len(purpose_attempts) >= purpose_max_attempts
        ):
            return False
        stored_step = step.model_copy(deep=True)
        steps = self._steps.setdefault(step.run_id, [])
        for index, existing in enumerate(steps):
            if existing.step_id == step.step_id:
                steps[index] = stored_step
                break
        else:
            steps.append(stored_step)
        stored, _ = _split_attempt(attempt, self._codec)
        attempts.append(stored)
        return True

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

    async def record_policy_decision(
        self,
        record: PolicyDecisionRecord,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> PolicyDecisionRecord:
        current = self._runs.get(record.run_id)
        if current is None:
            raise RunNotFoundError(f"run {record.run_id} not found")
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale Policy mutation for run {record.run_id}: expected version "
                f"{expected_version}, authoritative version is {current.version}"
            )
        if lease_owner is not None:
            self._check_lease(current, lease_owner)
        self._policy_decisions.setdefault(record.run_id, []).append(record)
        return record

    async def get_policy_decisions(
        self, run_id: str
    ) -> list[PolicyDecisionRecord]:
        return list(self._policy_decisions.get(run_id, []))

    # -- 内部 payload 读取 -------------------------------------------

    def _read_payload(self, run_id: str, field: str) -> str | None:
        encoded = self._payloads.get((run_id, field))
        if encoded is None:
            return None
        return self._codec.decode(encoded)

    def raw_payload_bytes(self, run_id: str, field: str) -> bytes | None:
        """返回未经解码的编码字节（仅测试用：验证确实经过 Codec）。"""
        return self._payloads.get((run_id, field))
