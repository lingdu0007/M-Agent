"""Routing Store：immutable Routing Decision 的持久化与审计。

Store 只追加事实，绝不改写历史：同一 decision_id 的重复保存必须
内容一致（幂等重放），内容不一致是确定性冲突；加载返回的是持久化
的事实本身——恢复或审计从不重新运行 Router，也不依据当前 Catalog
或 evidence 重算历史选择。

持久化边界：Decision（含候选过滤 reason trace、排序读数与结果）、
产生它的完整 evidence snapshot 输入，以及后续追加的 Run identity
绑定（RoutingDecisionBinding）都以规范 JSON payload 整体保存。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ._evidence import RoutingEvidence
from ._router import (
    RoutingDecision,
    RoutingDecisionBinding,
    RoutingError,
)

__all__ = [
    "InMemoryRoutingStore",
    "RoutingDecisionConflictError",
    "RoutingStoreError",
    "SQLiteRoutingStore",
    "StoredRoutingDecision",
]


class RoutingStoreError(RoutingError):
    """Routing Store 的公共错误基类。"""


class RoutingDecisionConflictError(RoutingStoreError):
    """同一 decision_id 以不同内容重复保存。"""


class _FrozenStoreValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StoredRoutingDecision(_FrozenStoreValue):
    """一次持久化的路由事实：Decision、输入 snapshots 与 Run 绑定。"""

    decision: RoutingDecision
    evidence: RoutingEvidence
    run_bindings: tuple[RoutingDecisionBinding, ...] = Field(
        default_factory=tuple
    )

def _verify_pair(decision: RoutingDecision, evidence: RoutingEvidence) -> None:
    """Decision 与 evidence 输入必须来自同一次路由（digest 一致）。"""
    if decision.evidence_digest != evidence.evidence_digest():
        raise RoutingStoreError(
            "decision evidence digest does not match the provided evidence"
        )


def _decision_payload(decision: RoutingDecision) -> str:
    return json.dumps(
        decision.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _evidence_payload(evidence: RoutingEvidence) -> str:
    return json.dumps(
        evidence.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class InMemoryRoutingStore:
    """进程内 append-only Routing Store（与 SQLite 实现共享行为契约）。"""

    def __init__(self) -> None:
        self._records: dict[str, StoredRoutingDecision] = {}
        self._order: list[str] = []

    def save_decision(
        self, decision: RoutingDecision, *, evidence: RoutingEvidence
    ) -> None:
        """幂等保存一次 Decision 及其输入 snapshots；内容冲突即失败。"""
        _verify_pair(decision, evidence)
        existing = self._records.get(decision.decision_id)
        if existing is not None:
            if (
                _decision_payload(existing.decision)
                != _decision_payload(decision)
                or _evidence_payload(existing.evidence)
                != _evidence_payload(evidence)
            ):
                raise RoutingDecisionConflictError(
                    "decision id already stored with different content"
                )
            return
        self._records[decision.decision_id] = StoredRoutingDecision(
            decision=decision, evidence=evidence
        )
        self._order.append(decision.decision_id)

    def load_decision(
        self, decision_id: str
    ) -> StoredRoutingDecision | None:
        """按原样返回持久化事实；不存在返回 None，绝不重算。"""
        return self._records.get(decision_id)

    def attach_run_binding(
        self, decision_id: str, binding: RoutingDecisionBinding
    ) -> None:
        """追加一条 Run identity 绑定（幂等，未知 decision 即失败）。"""
        record = self._records.get(decision_id)
        if record is None:
            raise RoutingStoreError("cannot bind a run to an unknown decision")
        if binding.decision_id != decision_id:
            raise RoutingStoreError("binding does not reference this decision")
        if any(
            existing == binding for existing in record.run_bindings
        ):
            return
        self._records[decision_id] = StoredRoutingDecision(
            decision=record.decision,
            evidence=record.evidence,
            run_bindings=record.run_bindings + (binding,),
        )

    def decision_ids(self) -> tuple[str, ...]:
        """按保存顺序返回全部 decision id（审计视图）。"""
        return tuple(self._order)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_decisions (
    decision_id TEXT PRIMARY KEY,
    decision_payload TEXT NOT NULL,
    evidence_payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routing_run_bindings (
    decision_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    binding_payload TEXT NOT NULL,
    PRIMARY KEY (decision_id, run_id)
);
"""


class SQLiteRoutingStore:
    """跨进程 durable 的 append-only Routing Store。

    每个 mutation 在单个 BEGIN IMMEDIATE 事务内「校验 + 写入 +
    commit」；同 decision_id 异内容在写入前即抛确定性冲突，重开
    数据库后已保存事实原样可读。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SQLITE_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def save_decision(
        self, decision: RoutingDecision, *, evidence: RoutingEvidence
    ) -> None:
        """幂等保存（事务内校验冲突）；内容一致的重放是 no-op。"""
        _verify_pair(decision, evidence)
        decision_json = _decision_payload(decision)
        evidence_json = _evidence_payload(evidence)
        cursor = self._connection.execute(
            "BEGIN IMMEDIATE"
        )
        try:
            row = self._connection.execute(
                "SELECT decision_payload, evidence_payload FROM"
                " routing_decisions WHERE decision_id = ?",
                (decision.decision_id,),
            ).fetchone()
            if row is not None:
                if row[0] != decision_json or row[1] != evidence_json:
                    raise RoutingDecisionConflictError(
                        "decision id already stored with different content"
                    )
                self._connection.commit()
                return
            self._connection.execute(
                "INSERT INTO routing_decisions (decision_id,"
                " decision_payload, evidence_payload) VALUES (?, ?, ?)",
                (decision.decision_id, decision_json, evidence_json),
            )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def load_decision(
        self, decision_id: str
    ) -> StoredRoutingDecision | None:
        """按原样读取持久化事实；不存在返回 None，绝不重算。"""
        row = self._connection.execute(
            "SELECT decision_payload, evidence_payload FROM"
            " routing_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        bindings = self._connection.execute(
            "SELECT binding_payload FROM routing_run_bindings WHERE"
            " decision_id = ? ORDER BY run_id",
            (decision_id,),
        ).fetchall()
        return StoredRoutingDecision(
            decision=RoutingDecision.model_validate(json.loads(row[0])),
            evidence=RoutingEvidence.model_validate(json.loads(row[1])),
            run_bindings=tuple(
                RoutingDecisionBinding.model_validate(json.loads(item[0]))
                for item in bindings
            ),
        )

    def attach_run_binding(
        self, decision_id: str, binding: RoutingDecisionBinding
    ) -> None:
        """事务内追加 Run identity 绑定；未知 decision 即失败。"""
        if binding.decision_id != decision_id:
            raise RoutingStoreError("binding does not reference this decision")
        binding_json = json.dumps(
            binding.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            row = self._connection.execute(
                "SELECT decision_payload FROM routing_decisions WHERE"
                " decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise RoutingStoreError(
                    "cannot bind a run to an unknown decision"
                )
            existing = self._connection.execute(
                "SELECT binding_payload FROM routing_run_bindings WHERE"
                " decision_id = ? AND run_id = ?",
                (decision_id, binding.run_id),
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO routing_run_bindings (decision_id, run_id,"
                    " binding_payload) VALUES (?, ?, ?)",
                    (decision_id, binding.run_id, binding_json),
                )
            elif existing[0] != binding_json:
                raise RoutingDecisionConflictError(
                    "run binding already stored with different content"
                )
            self._connection.commit()
        except Exception:
            self._connection.rollback()
            raise

    def decision_ids(self) -> tuple[str, ...]:
        """按保存顺序返回全部 decision id（审计视图）。"""
        rows = self._connection.execute(
            "SELECT decision_id FROM routing_decisions ORDER BY rowid"
        ).fetchall()
        return tuple(row[0] for row in rows)
