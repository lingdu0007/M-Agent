"""SQLiteRunStore：可跨进程恢复的 SQLite RunStore 实现。

ADR 0006 / PRD User Story 53：SQLiteRunStore 是本地与单服务部署的
持久化参考实现，必须支持进程重启后的恢复。Run 权威状态、Step、
Step Attempt、Checkpoint、乐观版本号、Run Lease（owner / 期限）与
定义引用都持久化在 SQLite；Run Payload（input / output / checkpoint
内容）只经配置的 :class:`PayloadCodec` 编码后存入 ``run_payloads``
表，metadata 表不含任何内容字段（ADR 0033）。

部署边界（Ticket 03 起）：

- 每个进程实例持有自己的连接；写入操作同步执行并在返回前 commit，
  因此 checkpoint 一旦返回即已落盘，进程崩溃（含 ``os._exit``）不会
  丢失已确认的 checkpoint；
- Run Lease（ADR 0013）以 runs 表的两列持久化：``acquire_lease`` /
  ``release_lease`` 与 Step / Attempt / Checkpoint / transition mutation
  都在单条 SQL 中校验 expected version；Runner mutation 还同时校验
  未过期租约，杜绝旧 owner 的迟到提交；
- 租约到期时间由构造时注入的 clock 决定（默认系统时钟；测试用
  :class:`FakeClock` 确定性推进过期），运行时不提供后台扫描、
  自动 takeover、queue 或 scheduler——接管完全由上层应用显式发起。
"""

from __future__ import annotations

from collections.abc import Callable
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .._clock import Clock, SystemClock
from .._codec import PayloadCodec
from .._definition import DefinitionSnapshot
from .._failure import redact_failure_message, sanitize_error_code
from .._errors import (
    DuplicateRunError,
    LeaseNotHeldError,
    RunNotFoundError,
    StaleRunVersionError,
)
from .._run import RunRecord
from .._model import ModelUsage
from .._status import RunStatus, validate_transition
from .._steps import (
    StepAttempt,
    StepCheckpoint,
    StepRecord,
    StepType,
    utc_now,
)
from .._store import (
    FIELD_RUN_INPUT,
    FIELD_RUN_OUTPUT,
    FIELD_RUN_SNAPSHOT,
    RunLease,
    _StoredAttempt,
    _StoredCheckpoint,
    _StoredRun,
    _split_attempt,
    _split_checkpoint,
    _split_run,
    _restore_snapshot,
    _snapshot_metadata,
    _snapshot_metadata_json,
    attempt_error_field,
    attempt_output_field,
    checkpoint_output_field,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id             TEXT PRIMARY KEY,
    definition_id      TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    status             TEXT NOT NULL,
    snapshot_json      TEXT,
    version            INTEGER NOT NULL,
    waiting_reason     TEXT,
    waiting_step_id    TEXT,
    error_code         TEXT,
    lease_owner        TEXT,
    lease_expires_at   TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_payloads (
    run_id  TEXT NOT NULL,
    field   TEXT NOT NULL,
    encoded BLOB NOT NULL,
    PRIMARY KEY (run_id, field)
);
CREATE TABLE IF NOT EXISTS steps (
    step_id    TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    step_type  TEXT NOT NULL,
    status     TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_attempts (
    attempt_id TEXT PRIMARY KEY,
    step_id    TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    status     TEXT NOT NULL,
    error      TEXT,
    classification TEXT,
    error_code TEXT,
    model_purpose TEXT,
    usage_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_checkpoints (
    step_id    TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    step_type  TEXT NOT NULL DEFAULT 'MODEL',
    created_at TEXT NOT NULL
);
"""


def _run_from_row(row: sqlite3.Row) -> _StoredRun:
    snapshot_data = (
        json.loads(row["snapshot_json"])
        if row["snapshot_json"] is not None
        else None
    )
    if snapshot_data is not None:
        snapshot_data.setdefault("instructions", "")
    return _StoredRun(
        run_id=row["run_id"],
        definition_id=row["definition_id"],
        definition_version=row["definition_version"],
        status=RunStatus(row["status"]),
        snapshot=(
            DefinitionSnapshot.model_validate(snapshot_data)
            if snapshot_data is not None
            else None
        ),
        version=row["version"],
        waiting_reason=row["waiting_reason"],
        waiting_step_id=row["waiting_step_id"],
        error_code=row["error_code"],
        lease_owner=row["lease_owner"],
        lease_expires_at=(
            datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _run_row(stored: _StoredRun) -> tuple:
    return (
        stored.run_id,
        stored.definition_id,
        stored.definition_version,
        stored.status.value,
        _snapshot_metadata_json(stored.snapshot),
        stored.version,
        stored.waiting_reason,
        stored.waiting_step_id,
        stored.error_code,
        stored.lease_owner,
        (
            stored.lease_expires_at.isoformat()
            if stored.lease_expires_at is not None
            else None
        ),
        stored.created_at.isoformat(),
        stored.updated_at.isoformat(),
    )


class SQLiteRunStore:
    """把权威 Run 状态持久化到单个 SQLite 数据库文件的 RunStore。

    :param path: 数据库文件路径；父目录必须已存在。
    :param payload_codec: 上层应用显式配置的 Payload Codec
        （ADR 0033）；明文 Codec 仅用于开发与测试。
    :param clock: 租约有效期检查使用的时钟（默认系统时钟；测试用
        :class:`FakeClock` 确定性驱动租约过期与接管）。
    """

    def __init__(
        self,
        path: str | Path,
        payload_codec: PayloadCodec,
        clock: Clock | None = None,
    ) -> None:
        self._path = str(path)
        self._codec = payload_codec
        self._clock = clock if clock is not None else SystemClock()
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """幂等迁移旧数据库的 metadata/payload 边界。

        ``CREATE TABLE IF NOT EXISTS`` 不会为已存在的表补列；对旧版本
        数据库打开时用 PRAGMA 检查并按需 ALTER，保证等待中的 Run 记录
        可携带目标 Step 标识。
        """
        columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(runs)")
        }
        if "waiting_step_id" not in columns:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN waiting_step_id TEXT"
            )
        if "error_code" not in columns:
            self._conn.execute("ALTER TABLE runs ADD COLUMN error_code TEXT")
        step_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(steps)")
        }
        if "error_code" not in step_columns:
            self._conn.execute("ALTER TABLE steps ADD COLUMN error_code TEXT")
        attempt_columns = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(step_attempts)")
        }
        if "model_purpose" not in attempt_columns:
            self._conn.execute(
                "ALTER TABLE step_attempts ADD COLUMN model_purpose TEXT"
            )
        if "usage_json" not in attempt_columns:
            self._conn.execute(
                "ALTER TABLE step_attempts ADD COLUMN usage_json TEXT"
            )
        # Ticket 02 旧版本把完整 DefinitionSnapshot 直接写进
        # ``snapshot_json``，Step Attempt 的诊断也落在 ``error`` 列。
        # 打开时迁移到受保护 payload，确保重开后 metadata 不继续泄漏。
        snapshots = self._conn.execute(
            "SELECT run_id, snapshot_json FROM runs "
            "WHERE snapshot_json IS NOT NULL"
        ).fetchall()
        for row in snapshots:
            snapshot_data = json.loads(row["snapshot_json"])
            if "instructions" not in snapshot_data:
                continue
            snapshot = DefinitionSnapshot.model_validate(snapshot_data)
            metadata = _snapshot_metadata(snapshot)
            assert metadata is not None
            self._set_payload(
                row["run_id"],
                FIELD_RUN_SNAPSHOT,
                self._codec.encode(snapshot.model_dump_json()),
            )
            self._conn.execute(
                "UPDATE runs SET snapshot_json=? WHERE run_id=?",
                (_snapshot_metadata_json(metadata), row["run_id"]),
            )
        errors = self._conn.execute(
            "SELECT attempt_id, run_id, error FROM step_attempts "
            "WHERE error IS NOT NULL"
        ).fetchall()
        for row in errors:
            self._set_payload(
                row["run_id"],
                attempt_error_field(row["attempt_id"]),
                self._codec.encode(redact_failure_message(row["error"])),
            )
            self._conn.execute(
                "UPDATE step_attempts SET error=NULL WHERE attempt_id=?",
                (row["attempt_id"],),
            )
        # ``error_code`` is queryable metadata. Older databases may have
        # accepted arbitrary Adapter-provided strings before the shared
        # failure boundary was introduced, so normalize them on open too.
        codes = self._conn.execute(
            "SELECT attempt_id, error_code FROM step_attempts "
            "WHERE error_code IS NOT NULL"
        ).fetchall()
        for row in codes:
            safe_code = sanitize_error_code(row["error_code"])
            if safe_code != row["error_code"]:
                self._conn.execute(
                    "UPDATE step_attempts SET error_code=? WHERE attempt_id=?",
                    (safe_code, row["attempt_id"]),
                )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteRunStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._path

    # -- Run Lease ----------------------------------------------------

    async def acquire_lease(
        self,
        run_id: str,
        owner: str,
        ttl: timedelta,
        *,
        expected_version: int,
    ) -> RunLease:
        """原子排他获取或续约 Run Lease（ADR 0013）。

        单条 UPDATE 限定 ``lease_owner IS NULL``（无租约）、
        ``lease_owner = 自己``（续约）或 ``lease_expires_at <= now``
        （过期接管）三种可获取情形，并同时限定 expected version；
        版本冲突抛 :class:`StaleRunVersionError`，其他 owner 持有 active
        lease 时抛 :class:`LeaseNotHeldError`。租约变化不递增版本号。
        """
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id} not found")
        now = self._clock.now()
        expires_at = now + ttl
        cursor = self._conn.execute(
            "UPDATE runs SET lease_owner=?, lease_expires_at=?"
            " WHERE run_id=? AND version=?"
            " AND (lease_owner IS NULL OR lease_owner=?"
            " OR lease_expires_at <= ?)",
            (
                owner,
                expires_at.isoformat(),
                run_id,
                expected_version,
                owner,
                now.isoformat(),
            ),
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            current = _run_from_row(
                self._conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
            )
            if current.version != expected_version:
                raise StaleRunVersionError(
                    f"stale lease acquisition for run {run_id}: expected "
                    f"version {expected_version}, authoritative version is "
                    f"{current.version}; lease was not mutated"
                )
            raise LeaseNotHeldError(
                f"cannot acquire lease for run {run_id}: held by "
                f"{current.lease_owner!r} until {current.lease_expires_at}"
            )
        self._conn.commit()
        return RunLease(run_id=run_id, owner=owner, expires_at=expires_at)

    async def release_lease(
        self, run_id: str, owner: str, *, expected_version: int
    ) -> None:
        """owner 匹配时清除租约；不匹配抛 :class:`LeaseNotHeldError`。"""
        cursor = self._conn.execute(
            "UPDATE runs SET lease_owner=NULL, lease_expires_at=NULL"
            " WHERE run_id=? AND version=? AND lease_owner=?",
            (run_id, expected_version, owner),
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT version, lease_owner FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {run_id} not found")
            if row["version"] != expected_version:
                raise StaleRunVersionError(
                    f"stale lease release for run {run_id}: expected version "
                    f"{expected_version}, authoritative version is "
                    f"{row['version']}; lease was not mutated"
                )
            raise LeaseNotHeldError(
                f"cannot release lease for run {run_id}: owner is "
                f"{row['lease_owner']!r}, not {owner!r}"
            )
        self._conn.commit()

    async def get_lease(self, run_id: str) -> RunLease | None:
        row = self._conn.execute(
            "SELECT lease_owner, lease_expires_at FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if (
            row is None
            or row["lease_owner"] is None
            or row["lease_expires_at"] is None
        ):
            return None
        return RunLease(
            run_id=run_id,
            owner=row["lease_owner"],
            expires_at=datetime.fromisoformat(row["lease_expires_at"]),
        )

    async def assert_lease(
        self, run_id: str, expected_version: int, owner: str
    ) -> None:
        """在外部调用 dispatch 前校验版本与未过期租约，不做续租。"""
        row = self._conn.execute(
            "SELECT version, lease_owner, lease_expires_at FROM runs"
            " WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if row["version"] != expected_version:
            raise StaleRunVersionError(
                f"stale dispatch for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{row['version']}; no step was started"
            )
        expires_at = (
            datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        )
        now = self._clock.now()
        if row["lease_owner"] != owner or expires_at is None or expires_at <= now:
            raise LeaseNotHeldError(
                f"run {run_id} lease is not held by {owner!r} "
                f"(owner={row['lease_owner']!r}, expires={expires_at}, now={now})"
            )

    async def prepare_model_dispatch(
        self,
        run_id: str,
        expected_version: int,
        owner: str,
        *,
        guard: Callable[[], None],
    ) -> None:
        """Validate a Model binding, then its lease, without an await gap."""
        row = self._conn.execute(
            "SELECT version, lease_owner, lease_expires_at FROM runs"
            " WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id} not found")
        if row["version"] != expected_version:
            raise StaleRunVersionError(
                f"stale dispatch for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{row['version']}; no step was started"
            )
        guard()
        expires_at = (
            datetime.fromisoformat(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        )
        now = self._clock.now()
        if row["lease_owner"] != owner or expires_at is None or expires_at <= now:
            raise LeaseNotHeldError(
                f"run {run_id} lease is not held by {owner!r} "
                f"(owner={row['lease_owner']!r}, expires={expires_at}, now={now})"
            )

    def _lease_condition(self, owner: str | None) -> tuple[str, tuple]:
        """拼出 mutation 的租约条件；owner 为 None 时不检查租约。"""
        if owner is None:
            return "", ()
        now = self._clock.now()
        return (
            " AND lease_owner=? AND lease_expires_at > ?",
            (owner, now.isoformat()),
        )

    def _lease_conflict(
        self, run_id: str, owner: str
    ) -> "LeaseNotHeldError":
        current = _run_from_row(
            self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        )
        return LeaseNotHeldError(
            f"run {run_id} lease is not held by {owner!r} "
            f"(owner={current.lease_owner!r}, "
            f"expires={current.lease_expires_at}, "
            f"now={self._clock.now()})"
        )

    def _raise_lease_or_missing(
        self, run_id: str, owner: str
    ) -> None:
        """租约校验失败后的统一报错：run 不存在报 RunNotFoundError，
        否则报 LeaseNotHeldError（与 InMemory 实现错误类型一致）。"""
        exists = self._conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if exists is None:
            raise RunNotFoundError(f"run {run_id} not found")
        raise self._lease_conflict(run_id, owner)

    # -- Run 生命周期 -------------------------------------------------

    async def create_run(self, run: RunRecord) -> RunRecord:
        stored, payloads = _split_run(run, self._codec)
        cur = self._conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (stored.run_id,)
        )
        if cur.fetchone() is not None:
            raise DuplicateRunError(f"run {stored.run_id} already exists")
        self._conn.execute(
            "INSERT INTO runs (run_id, definition_id, definition_version,"
            " status, snapshot_json, version, waiting_reason,"
            " waiting_step_id, error_code, lease_owner, lease_expires_at, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            _run_row(stored),
        )
        for field, encoded in payloads.items():
            self._set_payload(stored.run_id, field, encoded)
        self._conn.commit()
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
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        stored = _run_from_row(row)
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
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFoundError(f"run {run_id} not found")
        current = _run_from_row(row)
        if current.version != expected_version:
            raise StaleRunVersionError(
                f"stale update for run {run_id}: expected version "
                f"{expected_version}, authoritative version is "
                f"{current.version}; run record was not mutated"
            )
        validate_transition(current.status, status)
        # WAITING 字段只在 WAITING 状态有效（Ticket 07）：转出 WAITING
        # 一律清空，避免权威记录残留过期目标 Step。
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
        # 乐观版本控制 + 租约检查以 SQL 原子方式执行：UPDATE 同时限定
        # run_id、期望版本号与（可选的）未过期租约 owner。另一个连接已
        # 推进（版本不匹配）或租约已被接管（owner 不匹配/过期）时影响
        # 0 行，随后按实际原因区分抛 StaleRunVersionError 或
        # LeaseNotHeldError，杜绝基于同一版本或已失效租约的迟到提交。
        # 注意：SET 子句不包含租约列——状态转换不改变租约（owner/期限
        # 由 acquire/release 单独维护）。
        lease_clause, lease_params = self._lease_condition(lease_owner)
        cursor = self._conn.execute(
            "UPDATE runs SET definition_id=?, definition_version=?, status=?,"
            " snapshot_json=?, version=?, waiting_reason=?, waiting_step_id=?,"
            " error_code=?, created_at=?, updated_at=? WHERE run_id=? AND version=?"
            + lease_clause,
            (
                updated.definition_id,
                updated.definition_version,
                updated.status.value,
                _snapshot_metadata_json(updated.snapshot),
                updated.version,
                updated.waiting_reason,
                updated.waiting_step_id,
                updated.error_code,
                updated.created_at.isoformat(),
                updated.updated_at.isoformat(),
                run_id,
                expected_version,
            )
            + lease_params,
        )
        if cursor.rowcount == 0:
            # 回滚残留事务，保持连接干净，再区分失败原因。
            self._conn.rollback()
            if lease_owner is not None:
                fresh = _run_from_row(
                    self._conn.execute(
                        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                )
                if fresh.version != expected_version:
                    raise StaleRunVersionError(
                        f"stale update for run {run_id}: expected version "
                        f"{expected_version}, but the authoritative run was "
                        f"already advanced; run record was not mutated"
                    )
                raise self._lease_conflict(run_id, lease_owner)
            raise StaleRunVersionError(
                f"stale update for run {run_id}: expected version "
                f"{expected_version}, but the authoritative run was already "
                f"advanced; run record was not mutated"
            )
        if output is not None:
            self._set_payload(run_id, FIELD_RUN_OUTPUT, self._codec.encode(output))
        if snapshot is not None:
            self._set_payload(
                run_id,
                FIELD_RUN_SNAPSHOT,
                self._codec.encode(snapshot.model_dump_json()),
            )
        self._conn.commit()
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
        lease_clause, lease_params = self._lease_condition(lease_owner)
        cursor = self._conn.execute(
            "INSERT OR REPLACE INTO steps (step_id, run_id, step_type, status,"
            " error_code, created_at) SELECT ?,?,?,?,?,? WHERE EXISTS ("
            " SELECT 1 FROM runs WHERE run_id=? AND version=?"
            + lease_clause
            + ")",
            (
                step.step_id,
                step.run_id,
                step.step_type.value,
                step.status.value,
                step.error_code,
                step.created_at.isoformat(),
                step.run_id,
                expected_version,
            )
            + lease_params,
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT version FROM runs WHERE run_id = ?", (step.run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {step.run_id} not found")
            if row["version"] != expected_version:
                raise StaleRunVersionError(
                    f"stale Step mutation for run {step.run_id}: expected "
                    f"version {expected_version}, authoritative version is "
                    f"{row['version']}; Step was not recorded"
                )
            if lease_owner is not None:
                raise self._lease_conflict(step.run_id, lease_owner)
        self._conn.commit()
        return step.model_copy(deep=True)

    async def record_attempt(
        self,
        attempt: StepAttempt,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepAttempt:
        stored, payloads = _split_attempt(attempt, self._codec)
        lease_clause, lease_params = self._lease_condition(lease_owner)
        cursor = self._conn.execute(
            "INSERT OR REPLACE INTO step_attempts (attempt_id, step_id, run_id,"
            " status, error, classification, error_code, model_purpose, usage_json, created_at)"
            " SELECT ?,?,?,?,?,?,?,?,?,? WHERE EXISTS (SELECT 1 FROM runs"
            " WHERE run_id=? AND version=?"
            + lease_clause
            + ")",
            (
                stored.attempt_id,
                stored.step_id,
                stored.run_id,
                stored.status.value,
                stored.error,
                stored.classification,
                stored.error_code,
                stored.model_purpose,
                (
                    stored.usage.model_dump_json()
                    if stored.usage is not None
                    else None
                ),
                stored.created_at.isoformat(),
                stored.run_id,
                expected_version,
            )
            + lease_params,
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT version FROM runs WHERE run_id = ?", (stored.run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {stored.run_id} not found")
            if row["version"] != expected_version:
                raise StaleRunVersionError(
                    f"stale Attempt mutation for run {stored.run_id}: expected "
                    f"version {expected_version}, authoritative version is "
                    f"{row['version']}; Attempt was not recorded"
                )
            if lease_owner is not None:
                raise self._lease_conflict(stored.run_id, lease_owner)
        for field, encoded in payloads.items():
            self._set_payload(stored.run_id, field, encoded)
        self._conn.commit()
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
        """Atomically consume a model budget slot and persist its attempt."""
        if (
            step.step_type is not StepType.MODEL
            or step.run_id != attempt.run_id
            or step.step_id != attempt.step_id
            or attempt.model_purpose is None
        ):
            raise ValueError("model reservation requires one MODEL StepAttempt")
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT version, lease_owner, lease_expires_at FROM runs "
                "WHERE run_id=?",
                (attempt.run_id,),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {attempt.run_id} not found")
            if row["version"] != expected_version:
                raise StaleRunVersionError(
                    f"stale model reservation for run {attempt.run_id}: expected "
                    f"version {expected_version}, authoritative version is "
                    f"{row['version']}; model attempt was not reserved"
                )
            expires_at = (
                datetime.fromisoformat(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            )
            if (
                row["lease_owner"] != lease_owner
                or expires_at is None
                or expires_at <= self._clock.now()
            ):
                raise LeaseNotHeldError(
                    f"run {attempt.run_id} lease is not held by {lease_owner!r}"
                )
            model_attempts = self._conn.execute(
                "SELECT COUNT(*) FROM step_attempts WHERE run_id=? "
                "AND model_purpose IS NOT NULL",
                (attempt.run_id,),
            ).fetchone()[0]
            purpose_attempts = self._conn.execute(
                "SELECT COUNT(*) FROM step_attempts WHERE run_id=? "
                "AND model_purpose=?",
                (attempt.run_id, attempt.model_purpose.value),
            ).fetchone()[0]
            if (
                run_max_attempts is not None
                and model_attempts >= run_max_attempts
            ) or (
                purpose_max_attempts is not None
                and purpose_attempts >= purpose_max_attempts
            ):
                self._conn.rollback()
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO steps (step_id, run_id, step_type, "
                "status, error_code, created_at) VALUES (?,?,?,?,?,?)",
                (
                    step.step_id,
                    step.run_id,
                    step.step_type.value,
                    step.status.value,
                    step.error_code,
                    step.created_at.isoformat(),
                ),
            )
            self._conn.execute(
                "INSERT INTO step_attempts (attempt_id, step_id, run_id, status, "
                "error, classification, error_code, model_purpose, usage_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt.attempt_id,
                    attempt.step_id,
                    attempt.run_id,
                    attempt.status.value,
                    None,
                    None,
                    None,
                    attempt.model_purpose.value,
                    None,
                    attempt.created_at.isoformat(),
                ),
            )
            self._conn.commit()
            return True
        except BaseException:
            self._conn.rollback()
            raise

    async def record_checkpoint(
        self,
        checkpoint: StepCheckpoint,
        *,
        expected_version: int,
        lease_owner: str | None = None,
    ) -> StepCheckpoint:
        stored, payloads = _split_checkpoint(checkpoint, self._codec)
        lease_clause, lease_params = self._lease_condition(lease_owner)
        cursor = self._conn.execute(
            "INSERT INTO step_checkpoints (step_id, run_id, attempt_id,"
            " step_type, created_at) SELECT ?,?,?,?,? WHERE EXISTS"
            " (SELECT 1 FROM runs WHERE run_id=? AND version=?"
            + lease_clause
            + ")",
            (
                stored.step_id,
                stored.run_id,
                stored.attempt_id,
                stored.step_type.value,
                stored.created_at.isoformat(),
                stored.run_id,
                expected_version,
            )
            + lease_params,
        )
        if cursor.rowcount == 0:
            self._conn.rollback()
            row = self._conn.execute(
                "SELECT version FROM runs WHERE run_id = ?", (stored.run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(f"run {stored.run_id} not found")
            if row["version"] != expected_version:
                raise StaleRunVersionError(
                    f"stale Checkpoint mutation for run {stored.run_id}: "
                    f"expected version {expected_version}, authoritative version "
                    f"is {row['version']}; Checkpoint was not recorded"
                )
            if lease_owner is not None:
                raise self._lease_conflict(stored.run_id, lease_owner)
        for field, encoded in payloads.items():
            self._set_payload(stored.run_id, field, encoded)
        self._conn.commit()
        return stored.to_record(output=checkpoint.output)

    async def get_steps(self, run_id: str) -> list[StepRecord]:
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        return [
            StepRecord(
                step_id=row["step_id"],
                run_id=row["run_id"],
                step_type=row["step_type"],
                status=row["status"],
                error_code=row["error_code"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    async def get_attempts(self, run_id: str) -> list[StepAttempt]:
        rows = self._conn.execute(
            "SELECT * FROM step_attempts WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        attempts: list[StepAttempt] = []
        for row in rows:
            stored = _StoredAttempt(
                attempt_id=row["attempt_id"],
                step_id=row["step_id"],
                run_id=row["run_id"],
                status=row["status"],
                error=row["error"],
                classification=row["classification"],
                error_code=row["error_code"],
                model_purpose=row["model_purpose"],
                usage=(
                    ModelUsage.model_validate_json(row["usage_json"])
                    if row["usage_json"] is not None
                    else None
                ),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            attempts.append(
                stored.to_record(
                    output=self._read_payload(
                        run_id, attempt_output_field(stored.attempt_id)
                    ),
                    error=self._read_payload(
                        run_id, attempt_error_field(stored.attempt_id)
                    ),
                )
            )
        return attempts

    async def get_checkpoints(self, run_id: str) -> list[StepCheckpoint]:
        rows = self._conn.execute(
            "SELECT * FROM step_checkpoints WHERE run_id = ?"
            " ORDER BY rowid",
            (run_id,),
        ).fetchall()
        checkpoints: list[StepCheckpoint] = []
        for row in rows:
            stored = _StoredCheckpoint(
                run_id=row["run_id"],
                step_id=row["step_id"],
                attempt_id=row["attempt_id"],
                step_type=StepType(row["step_type"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            checkpoints.append(
                stored.to_record(
                    output=self._read_payload(
                        run_id, checkpoint_output_field(stored.step_id)
                    )
                )
            )
        return checkpoints

    # -- 内部 payload 读写 -------------------------------------------

    def _set_payload(self, run_id: str, field: str, encoded: bytes) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO run_payloads (run_id, field, encoded)"
            " VALUES (?,?,?)",
            (run_id, field, encoded),
        )

    def _read_payload(self, run_id: str, field: str) -> str | None:
        row = self._conn.execute(
            "SELECT encoded FROM run_payloads WHERE run_id = ? AND field = ?",
            (run_id, field),
        ).fetchone()
        if row is None:
            return None
        return self._codec.decode(row["encoded"])

    def raw_payload_bytes(self, run_id: str, field: str) -> bytes | None:
        """返回未经解码的编码字节（仅测试用：验证确实经过 Codec）。"""
        row = self._conn.execute(
            "SELECT encoded FROM run_payloads WHERE run_id = ? AND field = ?",
            (run_id, field),
        ).fetchone()
        return bytes(row["encoded"]) if row is not None else None
