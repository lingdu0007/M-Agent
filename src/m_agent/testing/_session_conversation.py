"""Offline session-conversation recovery scenario for Ticket 13.

Ticket 13 / ADR 0018-0021 / ADR 0042：durable Session 对话的真实子进程
崩溃恢复证据。三个已决议的跨 Store crash window——**Claim 后 Run 创建
前**、**Core 成功后 Session 提交前**、**Turn 追加与 Claim 清理原子
边界**——各连续重复三次：真实子进程只用公开 API 驱动会话，在公共
边界（SessionStore 端口 / Session PayloadCodec 端口）写 durable
sentinel 后以约定退出码硬退出；父进程 reopen 两个 SQLite Store，只
通过公开 ``SessionRunner`` / ``SessionStore`` / ``Runner`` API 对账，
恢复后分别得到正确的 ``CONFLICT``（第二个 Run 不得静默占用）、
``PENDING``（Core 成功 + 提交未完成的幂等重试窗口）与 ``COMMITTED``
（原子边界无半提交状态，重试恰好提交一次），且从不改写 Core 终态。

独立证据（journal + sentinel）写在两个 Store 之外：
``reconcile_session_recovery`` 把权威公开视图与 journal 派生事实对账，
并检出篡改（phantom turn）、duplicate-submit、wrong-scope 与
wrong-key 四类受控变异；``reconcile_session_protection`` 验证 Scope
fail-closed 与 Session Payload 独立保护（可搜索 metadata 与原始库文件
都不含对话正文、错误 key fail closed、正确 key 历史完整）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from ..adapters import (
    DeterministicModelAdapter,
    SQLiteRunStore,
)
from ..companion import (
    SessionClaimConflictError,
    SessionNotFoundError,
    SessionRunner,
    SessionScope,
    SessionTurn,
    SQLiteSessionStore,
)
from ..runtime import (
    AgentDefinition,
    DefinitionRegistry,
    ModelRequest,
    ModelResponse,
    PayloadCodec,
    RunNotFoundError,
    Runner,
    RunStatus,
)
from ._pack import AcceptanceCheckResult, AcceptanceCheckStatus, EvidenceLevel
from ._subprocess import isolated_subprocess_environment


_WINDOWS = (
    "after_claim_before_run_creation",
    "after_core_success_before_commit",
    "at_commit_atomic_boundary",
)
_WINDOW_REPETITIONS = 3
_CHILD_EXIT = 86

_SCOPE_TOKEN = "session-scenario-scope"
_OTHER_SCOPE_TOKEN = "session-scenario-other-scope"
_SESSION_ID = "session-scenario-1"
_DEFINITION_ID = "session-assistant"
_DEFINITION_VERSION = "1.0"
_CANARY_INPUT = "SESSION-SECRET-CANARY-T13"
_PROTECTION_FOLLOWUP = "second protection message"
_W1_FOLLOWUP_INPUT = "recovered follow up message"

_EXPECTED_WINDOW_STATUS = {
    "after_claim_before_run_creation": "CONFLICT",
    "after_core_success_before_commit": "PENDING",
    "at_commit_atomic_boundary": "COMMITTED",
}


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _append(path: Path, value: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class _KeyedSessionCodec(PayloadCodec):
    """Scenario keyed codec：对话正文经 key 指纹 + XOR 掩码落盘。

    错误 key 的解码在指纹校验处 fail closed；独立于 RunStore 的
    PayloadCodec 实例与 key 配置。
    """

    name = "scenario-keyed-session"
    _PREFIX = b"scenario-session:"
    _FINGERPRINT_LEN = 8

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("codec key must not be empty")
        self._key = key.encode("utf-8")
        self._fingerprint = hashlib.sha256(self._key).digest()[: self._FINGERPRINT_LEN]

    def _stream(self, size: int) -> bytes:
        repeats = size // len(self._key) + 1
        return (self._key * repeats)[:size]

    def encode(self, payload: str) -> bytes:
        data = payload.encode("utf-8")
        masked = bytes(a ^ b for a, b in zip(data, self._stream(len(data))))
        return self._PREFIX + self._fingerprint + masked

    def decode(self, encoded: bytes) -> str:
        prefix_len = len(self._PREFIX)
        if not encoded.startswith(self._PREFIX):
            raise ValueError("session payload does not use the scenario codec")
        fingerprint = encoded[prefix_len : prefix_len + self._FINGERPRINT_LEN]
        if fingerprint != self._fingerprint:
            raise ValueError("session payload was written with a different codec key")
        body = encoded[prefix_len + self._FINGERPRINT_LEN :]
        plain = bytes(a ^ b for a, b in zip(body, self._stream(len(body))))
        return plain.decode("utf-8")


class _BoundaryCrashCodec(PayloadCodec):
    """在 commit 事务内的 encode 边界写 sentinel 后硬退出。

    ``commit_turn`` 在单个 ``BEGIN IMMEDIATE`` 事务内调用
    ``encode``：崩溃发生在 Turn 追加与 Claim 清理的原子边界内，
    由 SQLite journal 保证 reopen 后绝不留下半提交状态。
    """

    name = "scenario-boundary-crash"

    def __init__(self, journal_path: Path, sentinel_path: Path) -> None:
        self._journal_path = journal_path
        self._sentinel_path = sentinel_path

    def encode(self, payload: str) -> bytes:
        _append(self._journal_path, {"kind": "commit_boundary_entered"})
        self._sentinel_path.write_text(
            "at_commit_atomic_boundary", encoding="utf-8"
        )
        os._exit(_CHILD_EXIT)

    def decode(self, encoded: bytes) -> str:
        raise ValueError("the boundary crash codec never decodes")


class _SessionScenarioModel(DeterministicModelAdapter):
    """确定性模型：回显输入，保证 Turn 输入/输出可对账。"""

    deterministic: bool = True

    def __init__(self) -> None:
        super().__init__(responses=("unused",))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        return ModelResponse(content=f"acknowledged:{request.input}")


def _registry() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id=_DEFINITION_ID,
            version=_DEFINITION_VERSION,
            instructions="echo the input deterministically",
            model_adapter=_SessionScenarioModel(),
        )
    )
    return registry


def _session_codec(codec_key: str, window: str, journal: Path, sentinel: Path):
    if window == "at_commit_atomic_boundary":
        return _BoundaryCrashCodec(journal, sentinel)
    return _KeyedSessionCodec(codec_key)


def _run_codec(codec_key: str) -> _KeyedSessionCodec:
    """RunStore 使用独立 key 的同一 Codec 类型：两个保护边界互不共享。"""
    return _KeyedSessionCodec(f"{codec_key}:runs")


def _crash_child(
    window: str,
    session_db: str,
    run_db: str,
    journal: str,
    sentinel: str,
    run_id_file: str,
    codec_key: str,
) -> None:
    """只用公开 API 驱动一条 Session 消息并在指定窗口硬退出。"""

    async def start() -> None:
        journal_path = Path(journal)
        sentinel_path = Path(sentinel)
        session_store = SQLiteSessionStore(
            session_db,
            payload_codec=_session_codec(
                codec_key, window, journal_path, sentinel_path
            ),
        )
        run_store = SQLiteRunStore(run_db, payload_codec=_run_codec(codec_key))
        try:
            runner = Runner(_registry(), run_store)
            scope = SessionScope(token=_SCOPE_TOKEN)
            await session_store.create_session(scope, _SESSION_ID)
            _append(journal_path, {"kind": "session_created"})
            run_id = uuid.uuid4().hex
            Path(run_id_file).write_text(run_id, encoding="utf-8")
            snapshot = await session_store.read_snapshot(scope, _SESSION_ID)
            await session_store.claim_run(
                scope, _SESSION_ID, run_id, expected_version=snapshot.version
            )
            _append(journal_path, {"kind": "claim_created", "run_id": run_id})
            if window == "after_claim_before_run_creation":
                sentinel_path.write_text(window, encoding="utf-8")
                os._exit(_CHILD_EXIT)
            run = await runner.create_run(
                _DEFINITION_ID,
                _DEFINITION_VERSION,
                f"crash window message: {window}",
                run_id=run_id,
                history=(),
            )
            _append(journal_path, {"kind": "run_created", "run_id": run_id})
            terminal = await runner.start_run(run.run_id)
            if terminal.status is RunStatus.SUCCEEDED:
                _append(journal_path, {"kind": "run_succeeded", "run_id": run_id})
            if window == "after_core_success_before_commit":
                sentinel_path.write_text(window, encoding="utf-8")
                os._exit(_CHILD_EXIT)
            claim = await session_store.get_claim(scope, _SESSION_ID)
            turn = SessionTurn(
                turn_id=uuid.uuid4().hex,
                session_id=_SESSION_ID,
                run_id=run_id,
                definition_id=terminal.definition_id,
                definition_version=terminal.definition_version,
                user_input=terminal.input,
                assistant_output=terminal.output or "",
                created_at=datetime.now(),
            )
            # at_commit_atomic_boundary：encode 在事务内崩溃。
            await session_store.commit_turn(
                scope, _SESSION_ID, turn, expected_version=claim.session_version
            )
            raise RuntimeError(f"window {window!r} did not terminate the child")
        finally:
            session_store.close()
            run_store.close()

    asyncio.run(start())


def reconcile_session_recovery(
    observation: Mapping[str, object],
    *,
    journal_events: Sequence[Mapping[str, object]],
) -> list[str]:
    """把公开 Session/Run 视图与独立 journal 事实对账。

    返回问题列表；空列表表示该窗口恢复符合已决议语义。变异输入
    （phantom turn、duplicate turn、被改写的 Core 终态等）必须产生
    非空问题列表，否则 Harness 为 ERROR（ADR 0042）。
    """
    problems: list[str] = []
    window = observation.get("window")
    kinds = [event.get("kind") for event in journal_events]
    claimed = {
        event.get("run_id")
        for event in journal_events
        if event.get("kind") == "claim_created"
    }
    created = {
        event.get("run_id")
        for event in journal_events
        if event.get("kind") == "run_created"
    }
    succeeded = {
        event.get("run_id")
        for event in journal_events
        if event.get("kind") == "run_succeeded"
    }

    turns = list(observation.get("turn_run_ids") or [])
    if len(set(turns)) != len(turns):
        problems.append("duplicate_turn_identity")
    attested = claimed | created | {observation.get("followup_run_id")}
    if any(run_id not in attested for run_id in turns):
        problems.append("unattested_turn_in_history")

    if window == "after_claim_before_run_creation":
        if claimed and created:
            problems.append("journal_does_not_match_window")
        if not observation.get("claim_survived_restart"):
            problems.append("claim_lost_across_restart")
        if not observation.get("second_run_blocked"):
            problems.append("second_run_silently_admitted")
        if not observation.get("reconciled_missing_run") or not observation.get(
            "claim_released_after_reconciliation"
        ):
            problems.append("ghost_claim_not_reconciled")
        if observation.get("followup_commit_status") != "COMMITTED":
            problems.append("conversation_blocked_after_reconciliation")
        never_created = (claimed | created) - created - {observation.get("followup_run_id")}
        if any(run_id in never_created for run_id in turns):
            problems.append("phantom_turn_from_never_created_run")
        if observation.get("observed_status") != "CONFLICT":
            problems.append("expected_conflict_not_observed")
    elif window == "after_core_success_before_commit":
        if not (claimed and created and succeeded) or "turn_committed" in kinds:
            problems.append("journal_does_not_match_window")
        if observation.get("core_status_after_crash") != "SUCCEEDED":
            problems.append("core_terminal_rewritten")
        if observation.get("commit_status_after_crash") != "PENDING":
            problems.append("pending_not_observed")
        if observation.get("recovery_commit_status") != "COMMITTED":
            problems.append("idempotent_retry_failed")
        if not observation.get("claim_cleared"):
            problems.append("claim_not_cleared_after_commit")
        if observation.get("core_status_after_recovery") != "SUCCEEDED":
            problems.append("core_terminal_rewritten")
        if set(turns) != succeeded:
            problems.append("successful_run_not_committed_once")
        if observation.get("observed_status") != "PENDING":
            problems.append("expected_pending_not_observed")
    elif window == "at_commit_atomic_boundary":
        if "commit_boundary_entered" not in kinds:
            problems.append("journal_does_not_match_window")
        if not observation.get("sentinel_reached"):
            problems.append("crash_not_at_boundary")
        if observation.get("partial_state_after_crash"):
            problems.append("partial_commit_state")
        if observation.get("commit_status_after_crash") != "PENDING":
            problems.append("pending_not_observed")
        if observation.get("recovery_commit_status") != "COMMITTED":
            problems.append("idempotent_retry_failed")
        if observation.get("replay_turn_count") != 1:
            problems.append("duplicate_turn_after_replay")
        if set(turns) != succeeded:
            problems.append("successful_run_not_committed_once")
        if observation.get("core_status_after_recovery") != "SUCCEEDED":
            problems.append("core_terminal_rewritten")
        if observation.get("observed_status") != "COMMITTED":
            problems.append("expected_committed_not_observed")
    else:
        problems.append("unknown_window")
    return problems


def reconcile_session_protection(
    observation: Mapping[str, object],
) -> list[str]:
    """验证 Scope fail-closed 与 Session Payload 独立保护观察。"""
    problems: list[str] = []
    if not observation.get("wrong_scope_access_blocked"):
        problems.append("wrong_scope_access_not_closed")
    if not observation.get("wrong_key_decode_failed"):
        problems.append("wrong_key_decode_succeeded")
    if not observation.get("searchable_metadata_clean"):
        problems.append("conversation_text_in_searchable_metadata")
    if not observation.get("raw_database_free_of_plaintext"):
        problems.append("plaintext_history_on_disk")
    if not observation.get("correct_key_history_intact"):
        problems.append("history_corrupted_with_correct_key")
    if not observation.get("metadata_readable_without_payload_key"):
        problems.append("metadata_requires_payload_key")
    if not observation.get("error_messages_free_of_conversation_text"):
        problems.append("error_message_leaks_conversation_text")
    return problems


async def _recover_once(
    window: str,
    repetition: int,
    directory: Path,
    codec_key: str,
) -> tuple[dict, dict, list[dict]]:
    session_db = directory / f"{window}-{repetition}.session.sqlite3"
    run_db = directory / f"{window}-{repetition}.runs.sqlite3"
    journal = directory / f"{window}-{repetition}.journal.jsonl"
    sentinel = directory / f"{window}-{repetition}.sentinel"
    run_id_file = directory / f"{window}-{repetition}.run-id"
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from m_agent.testing._session_conversation import _crash_child; "
                "_crash_child(*__import__('sys').argv[1:])"
            ),
            window,
            str(session_db),
            str(run_db),
            str(journal),
            str(sentinel),
            str(run_id_file),
            codec_key,
        ],
        env=isolated_subprocess_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if (
        child.returncode != _CHILD_EXIT
        or not sentinel.is_file()
        or not run_id_file.is_file()
    ):
        raise RuntimeError(
            f"session crash child did not reach {window}: {child.stderr}"
        )
    run_id = run_id_file.read_text(encoding="utf-8")
    events = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
        if line
    ]
    session_store = SQLiteSessionStore(
        session_db, payload_codec=_KeyedSessionCodec(codec_key)
    )
    run_store = SQLiteRunStore(run_db, payload_codec=_run_codec(codec_key))
    try:
        core = Runner(_registry(), run_store)
        runner = SessionRunner(runner=core, session_store=session_store)
        scope = SessionScope(token=_SCOPE_TOKEN)
        claim = await session_store.get_claim(scope, _SESSION_ID)
        claim_survived_restart = claim is not None and claim.run_id == run_id
        observation: dict[str, object] = {
            "window": window,
            "repetition": repetition,
            "claim_survived_restart": claim_survived_restart,
        }
        if window == "after_claim_before_run_creation":
            try:
                await runner.submit(
                    scope,
                    _SESSION_ID,
                    _DEFINITION_ID,
                    _DEFINITION_VERSION,
                    "overtake attempt",
                )
                second_run_blocked = False
            except SessionClaimConflictError:
                second_run_blocked = True
            try:
                await runner.resume(scope, _SESSION_ID)
                reconciled_missing_run = False
            except RunNotFoundError:
                reconciled_missing_run = True
            claim_released = (
                await session_store.get_claim(scope, _SESSION_ID) is None
            )
            followup = await runner.submit(
                scope,
                _SESSION_ID,
                _DEFINITION_ID,
                _DEFINITION_VERSION,
                _W1_FOLLOWUP_INPUT,
            )
            snapshot = await session_store.read_snapshot(scope, _SESSION_ID)
            observation.update(
                {
                    "second_run_blocked": second_run_blocked,
                    "reconciled_missing_run": reconciled_missing_run,
                    "claim_released_after_reconciliation": claim_released,
                    "followup_run_id": followup.run.run_id,
                    "followup_commit_status": followup.commit_status.value,
                    "turn_run_ids": [turn.run_id for turn in snapshot.turns],
                    "version": snapshot.version,
                    "observed_status": "CONFLICT" if second_run_blocked else "",
                }
            )
        elif window == "after_core_success_before_commit":
            authoritative = await core.get_run(run_id)
            commit_status_after_crash = await runner.commit_status(
                scope, _SESSION_ID, run_id
            )
            recovery = await runner.resume(scope, _SESSION_ID)
            snapshot = await session_store.read_snapshot(scope, _SESSION_ID)
            still = await core.get_run(run_id)
            observation.update(
                {
                    "core_status_after_crash": authoritative.status.value,
                    "commit_status_after_crash": commit_status_after_crash.value,
                    "recovery_commit_status": recovery.commit_status.value,
                    "turn_run_ids": [turn.run_id for turn in snapshot.turns],
                    "version": snapshot.version,
                    "claim_cleared": (
                        await session_store.get_claim(scope, _SESSION_ID) is None
                    ),
                    "core_status_after_recovery": still.status.value,
                    "observed_status": commit_status_after_crash.value,
                }
            )
        else:
            snapshot_after = await session_store.read_snapshot(scope, _SESSION_ID)
            claim_after = await session_store.get_claim(scope, _SESSION_ID)
            partial_state = bool(
                snapshot_after.version != 0
                or snapshot_after.turns
                or claim_after is None
                or claim_after.run_id != run_id
            )
            commit_status_after_crash = await runner.commit_status(
                scope, _SESSION_ID, run_id
            )
            recovery = await runner.resume(scope, _SESSION_ID)
            snapshot = await session_store.read_snapshot(scope, _SESSION_ID)
            run = await core.get_run(run_id)
            replay_turn = SessionTurn(
                turn_id=uuid.uuid4().hex,
                session_id=_SESSION_ID,
                run_id=run_id,
                definition_id=run.definition_id,
                definition_version=run.definition_version,
                user_input=run.input,
                assistant_output=run.output or "",
                created_at=datetime.now(),
            )
            replay = await session_store.commit_turn(
                scope, _SESSION_ID, replay_turn, expected_version=0
            )
            snapshot_after_replay = await session_store.read_snapshot(
                scope, _SESSION_ID
            )
            observation.update(
                {
                    "sentinel_reached": sentinel.is_file(),
                    "partial_state_after_crash": partial_state,
                    "commit_status_after_crash": commit_status_after_crash.value,
                    "recovery_commit_status": recovery.commit_status.value,
                    "turn_run_ids": [turn.run_id for turn in snapshot.turns],
                    "version": snapshot.version,
                    "replay_status": replay.status.value,
                    "replay_turn_count": len(snapshot_after_replay.turns),
                    "core_status_after_recovery": run.status.value,
                    "observed_status": recovery.commit_status.value,
                }
            )
        problems = reconcile_session_recovery(observation, journal_events=events)
        observation["problems"] = problems
    finally:
        session_store.close()
        run_store.close()
    evidence = {
        "journal_digest": "sha256:"
        + hashlib.sha256(journal.read_bytes()).hexdigest(),
        "sentinel_digest": "sha256:"
        + hashlib.sha256(sentinel.read_bytes()).hexdigest(),
    }
    return observation, evidence, events


def _scan_session_database(path: Path, forbidden_texts: Sequence[str]) -> dict:
    """独立读取 Session 数据库：metadata 与原始文件都不得含对话正文。"""
    metadata_values: list[object] = []
    with sqlite3.connect(path) as connection:
        statements = connection.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
        metadata_values.extend(statements)
        for table in ("sessions", "session_claims", "session_turns"):
            if table == "session_turns":
                metadata_values.extend(
                    connection.execute(
                        "SELECT turn_index, turn_id, session_id, run_id,"
                        " definition_id, definition_version, created_at"
                        " FROM session_turns"
                    ).fetchall()
                )
            else:
                metadata_values.extend(
                    connection.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
                )
    metadata_text = repr(metadata_values)
    raw_bytes = path.read_bytes()
    return {
        "searchable_metadata_clean": all(
            text not in metadata_text for text in forbidden_texts
        ),
        "raw_database_free_of_plaintext": all(
            text.encode("utf-8") not in raw_bytes for text in forbidden_texts
        ),
        "raw_database_digest": "sha256:" + hashlib.sha256(raw_bytes).hexdigest(),
    }


def _scope_row_counts(path: Path, token: str) -> dict[str, int]:
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE scope_token=?",  # noqa: S608
                (token,),
            ).fetchone()[0]
            for table in ("sessions", "session_claims", "session_turns")
        }


async def _protection_probes(
    directory: Path, codec_key: str
) -> tuple[dict, dict, dict]:
    session_db = directory / "protection.session.sqlite3"
    run_db = directory / "protection.runs.sqlite3"
    session_store = SQLiteSessionStore(
        session_db, payload_codec=_KeyedSessionCodec(codec_key)
    )
    run_store = SQLiteRunStore(run_db, payload_codec=_run_codec(codec_key))
    try:
        core = Runner(_registry(), run_store)
        runner = SessionRunner(runner=core, session_store=session_store)
        scope = SessionScope(token=_SCOPE_TOKEN)
        other_scope = SessionScope(token=_OTHER_SCOPE_TOKEN)
        await session_store.create_session(scope, _SESSION_ID)
        first = await runner.submit(
            scope, _SESSION_ID, _DEFINITION_ID, _DEFINITION_VERSION, _CANARY_INPUT
        )
        second = await runner.submit(
            scope,
            _SESSION_ID,
            _DEFINITION_ID,
            _DEFINITION_VERSION,
            _PROTECTION_FOLLOWUP,
        )
        committed = [
            first.commit_status.value,
            second.commit_status.value,
        ] == ["COMMITTED", "COMMITTED"]

        # wrong-scope：所有写读路径都 fail closed（与不存在不可区分）。
        scope_probe_results: list[str] = []
        for attempt in (
            lambda: session_store.read_snapshot(other_scope, _SESSION_ID),
            lambda: session_store.claim_run(
                other_scope, _SESSION_ID, "scope-run", expected_version=0
            ),
            lambda: session_store.release_claim(other_scope, _SESSION_ID, "scope-run"),
            lambda: session_store.find_turn_by_run(
                other_scope, _SESSION_ID, first.run.run_id
            ),
        ):
            try:
                await attempt()
                scope_probe_results.append("cross_scope_access_succeeded")
            except SessionNotFoundError:
                pass
        wrong_scope_access_blocked = not scope_probe_results and (
            await session_store.get_session(other_scope, _SESSION_ID) is None
            and await session_store.get_claim(other_scope, _SESSION_ID) is None
        )

        # 错误信息不泄露对话正文。
        error_texts: list[str] = []
        for attempt in (
            lambda: session_store.read_snapshot(
                scope, _SESSION_ID, expected_version=99
            ),
            lambda: session_store.read_snapshot(other_scope, _SESSION_ID),
            lambda: session_store.claim_run(
                scope, _SESSION_ID, "late-run", expected_version=99
            ),
        ):
            try:
                await attempt()
            except Exception as error:  # noqa: BLE001 - 收集错误文本
                error_texts.append(str(error))
        error_messages_free_of_conversation_text = all(
            _CANARY_INPUT not in text and _PROTECTION_FOLLOWUP not in text
            for text in error_texts
        )

        # 独立读取：可搜索 metadata 与原始库文件都不含对话正文；
        # 同时覆盖真实恢复落盘的 W1 follow-up Turn。
        w1_database = directory / "after_claim_before_run_creation-0.session.sqlite3"
        forbidden = [_CANARY_INPUT, _PROTECTION_FOLLOWUP]
        scan = _scan_session_database(session_db, forbidden)
        if w1_database.is_file():
            w1_scan = _scan_session_database(w1_database, [_W1_FOLLOWUP_INPUT])
            scan["searchable_metadata_clean"] = (
                scan["searchable_metadata_clean"]
                and w1_scan["searchable_metadata_clean"]
            )
            scan["raw_database_free_of_plaintext"] = (
                scan["raw_database_free_of_plaintext"]
                and w1_scan["raw_database_free_of_plaintext"]
            )

        # 错误 key fail closed；metadata 不依赖 payload key。
        wrong = SQLiteSessionStore(
            session_db, payload_codec=_KeyedSessionCodec(f"{codec_key}-wrong")
        )
        try:
            try:
                await wrong.read_snapshot(scope, _SESSION_ID)
                wrong_key_decode_failed = False
            except ValueError:
                wrong_key_decode_failed = True
            try:
                await wrong.find_turn_by_run(scope, _SESSION_ID, first.run.run_id)
                wrong_key_decode_failed = False
            except ValueError:
                pass
            wrong_record = await wrong.get_session(scope, _SESSION_ID)
            metadata_readable_without_payload_key = (
                wrong_record is not None and wrong_record.version == 2
            )
        finally:
            wrong.close()

        # 正确 key reopen：历史完整。
        right = SQLiteSessionStore(
            session_db, payload_codec=_KeyedSessionCodec(codec_key)
        )
        try:
            snapshot = await right.read_snapshot(scope, _SESSION_ID)
            correct_key_history_intact = (
                snapshot.version == 2
                and [turn.user_input for turn in snapshot.turns]
                == [_CANARY_INPUT, _PROTECTION_FOLLOWUP]
                and [turn.assistant_output for turn in snapshot.turns]
                == [
                    f"acknowledged:{_CANARY_INPUT}",
                    f"acknowledged:{_PROTECTION_FOLLOWUP}",
                ]
            )
        finally:
            right.close()

        scope_rows = _scope_row_counts(session_db, _OTHER_SCOPE_TOKEN)
        observation = {
            "committed": committed,
            "wrong_scope_access_blocked": wrong_scope_access_blocked,
            "wrong_key_decode_failed": wrong_key_decode_failed,
            "searchable_metadata_clean": scan["searchable_metadata_clean"],
            "raw_database_free_of_plaintext": scan["raw_database_free_of_plaintext"],
            "correct_key_history_intact": correct_key_history_intact,
            "metadata_readable_without_payload_key": metadata_readable_without_payload_key,
            "error_messages_free_of_conversation_text": error_messages_free_of_conversation_text,
        }
        observation["problems"] = reconcile_session_protection(observation)
        independent = {
            **scan,
            "cross_scope_rows_absent": scope_rows,
        }
        return observation, independent, scan
    finally:
        session_store.close()
        run_store.close()


def _mutation_evidence(
    observations: Sequence[Mapping[str, object]],
    journal_events: Mapping[str, Sequence[Mapping[str, object]]],
    protection_observation: Mapping[str, object],
) -> dict[str, object]:
    """四类受控变异必须全部被公共对账 seam 检出（ADR 0042）。"""
    by_window: dict[str, Mapping[str, object]] = {}
    for observation in observations:
        by_window.setdefault(observation["window"], observation)
    w2 = by_window["after_core_success_before_commit"]
    w3 = by_window["at_commit_atomic_boundary"]

    tampered = {**w2, "turn_run_ids": [*w2["turn_run_ids"], "phantom-run"]}
    tamper_problems = reconcile_session_recovery(
        tampered, journal_events=journal_events["after_core_success_before_commit"]
    )
    duplicated = {
        **w3,
        "turn_run_ids": [*w3["turn_run_ids"], *w3["turn_run_ids"]],
        "replay_turn_count": 2,
    }
    duplicate_problems = reconcile_session_recovery(
        duplicated, journal_events=journal_events["at_commit_atomic_boundary"]
    )
    wrong_scope_problems = reconcile_session_protection(
        {**protection_observation, "wrong_scope_access_blocked": False}
    )
    wrong_key_problems = reconcile_session_protection(
        {**protection_observation, "wrong_key_decode_failed": False}
    )
    core_rewrite = {**w2, "core_status_after_recovery": "FAILED"}
    core_rewrite_problems = reconcile_session_recovery(
        core_rewrite, journal_events=journal_events["after_core_success_before_commit"]
    )
    return {
        "tamper_detected": bool(tamper_problems),
        "duplicate_submit_detected": bool(duplicate_problems),
        "wrong_scope_detected": bool(wrong_scope_problems),
        "wrong_key_detected": bool(wrong_key_problems),
        "core_rewrite_detected": bool(core_rewrite_problems),
        "tamper_problems": tamper_problems,
        "duplicate_submit_problems": duplicate_problems,
        "wrong_scope_problems": wrong_scope_problems,
        "wrong_key_problems": wrong_key_problems,
        "core_rewrite_problems": core_rewrite_problems,
    }


def run_session_conversation() -> tuple[
    tuple[AcceptanceCheckResult, ...],
    dict[str, str | int | bool],
    dict[str, str],
]:
    """运行 session-conversation Scenario 的完整离线证明。

    返回 ``(checks, evidence_view, independent_evidence)``，与
    :func:`m_agent.testing.run_durable_effects_recovery` 相同的形态。
    """

    async def execute():
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            codec_key = uuid.uuid4().hex
            observations: list[dict] = []
            independent: list[dict] = []
            journals: dict[str, list] = {window: [] for window in _WINDOWS}
            for window in _WINDOWS:
                for repetition in range(_WINDOW_REPETITIONS):
                    observation, evidence, events = await _recover_once(
                        window, repetition, directory, codec_key
                    )
                    observations.append(observation)
                    independent.append(evidence)
                    journals[window].extend(events)
            protection, protection_independent, _ = await _protection_probes(
                directory, codec_key
            )
            return observations, independent, journals, protection, protection_independent

    observations, independent, journals, protection, protection_independent = (
        asyncio.run(execute())
    )

    by_window: dict[str, list[dict]] = {window: [] for window in _WINDOWS}
    for observation in observations:
        by_window[observation["window"]].append(observation)
    windows_clean = all(not observation["problems"] for observation in observations)
    outcome_clean = all(
        observation["observed_status"] == _EXPECTED_WINDOW_STATUS[window]
        for window, window_observations in by_window.items()
        for observation in window_observations
    )
    core_preserved = all(
        observation.get("core_status_after_recovery") in (None, "SUCCEEDED")
        for observation in observations
    )
    claim_clean = all(
        observation.get("claim_survived_restart") for observation in observations
    ) and all(
        observation.get("second_run_blocked")
        for observation in by_window["after_claim_before_run_creation"]
    )
    protection_clean = not protection["problems"]
    mutations = _mutation_evidence(observations, journals, protection)
    mutation_detected = (
        mutations["tamper_detected"]
        and mutations["duplicate_submit_detected"]
        and mutations["wrong_scope_detected"]
        and mutations["wrong_key_detected"]
        and mutations["core_rewrite_detected"]
    )

    recovery_digest = _digest(observations)
    journal_digest = _digest(independent)
    claim_digest = _digest(
        {
            window: [
                {
                    key: observation.get(key)
                    for key in ("claim_survived_restart", "second_run_blocked")
                }
                for observation in window_observations
            ]
            for window, window_observations in by_window.items()
        }
    )
    protection_digest = _digest(protection)
    protection_independent_digest = _digest(protection_independent)
    mutation_digest = _digest(
        {
            key: value
            for key, value in mutations.items()
            if not key.endswith("problems")
        }
    )
    evidence_view: dict[str, str | int | bool] = {
        "recovery_window_repetitions": len(observations),
        "recovery_windows_clean": windows_clean,
        "recovery_window_outcomes_correct": outcome_clean,
        "recovery_windows_core_terminal_preserved": core_preserved,
        "recovery_window_conflict_observed": outcome_clean,
        "recovery_window_pending_observed": outcome_clean,
        "recovery_window_committed_observed": outcome_clean,
        "claim_no_ttl_repetitions": len(observations),
        "claim_no_ttl_observed": claim_clean,
        "payload_protection_observed": protection_clean,
        "scope_isolation_observed": protection_clean,
        "mutation_detected": mutation_detected,
        "mutation_tamper_detected": mutations["tamper_detected"],
        "mutation_duplicate_submit_detected": mutations["duplicate_submit_detected"],
        "mutation_wrong_scope_detected": mutations["wrong_scope_detected"],
        "mutation_wrong_key_detected": mutations["wrong_key_detected"],
        "recovery_windows_authoritative_digest": recovery_digest,
        "claim_no_ttl_authoritative_digest": claim_digest,
        "payload_protection_authoritative_digest": protection_digest,
        "scope_isolation_authoritative_digest": protection_digest,
        "mutation_authoritative_digest": mutation_digest,
    }
    independent_evidence = {
        "recovery_windows_journal_digest": journal_digest,
        "claim_no_ttl_journal_digest": journal_digest,
        "payload_protection_independent_digest": protection_independent_digest,
        "scope_isolation_independent_digest": protection_independent_digest,
        "mutation_independent_digest": _digest(
            {
                "tamper": mutations["tamper_problems"],
                "duplicate_submit": mutations["duplicate_submit_problems"],
                "wrong_scope": mutations["wrong_scope_problems"],
                "wrong_key": mutations["wrong_key_problems"],
                "core_rewrite": mutations["core_rewrite_problems"],
            }
        ),
    }

    def result(check_id: str, passed: bool, digest: str) -> AcceptanceCheckResult:
        return AcceptanceCheckResult(
            check_id=check_id,
            status=(
                AcceptanceCheckStatus.PASS if passed else AcceptanceCheckStatus.FAIL
            ),
            evidence_level=EvidenceLevel.CONTRACT,
            reason_code="session_recovery_observed",
            evidence_digest=digest,
        )

    checks = (
        result(
            "session.conversation.recovery-windows",
            windows_clean and outcome_clean and core_preserved,
            recovery_digest,
        ),
        result("session.conversation.claim-no-ttl", claim_clean, claim_digest),
        result(
            "session.conversation.payload-protection", protection_clean, protection_digest
        ),
        result("session.conversation.scope-isolation", protection_clean, protection_digest),
        result(
            "session.conversation.mutation",
            mutation_detected,
            evidence_view["mutation_authoritative_digest"],
        ),
    )
    return checks, evidence_view, independent_evidence
