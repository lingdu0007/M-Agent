"""SQLiteEvalStore：跨进程 durable 的 append-only EvalStore（Ticket 18）。

与 :class:`~m_agent.companion.eval.InMemoryEvalStore` 共享同一份行为
契约（不可变身份、幂等重放、确定性冲突、最小公开 view），并通过
``tests/eval_store_contract.py`` 的共享契约套件同时验证。

持久化边界：

- 六类事实（execution / observation / evaluator result / report
  revision / baseline / recommendation）都以规范 JSON payload 整体
  持久化；observation 的 external evidence 只接受可判定编码
  （EvidenceArtifact 与 JSON 标量），其余类型写入时确定性拒绝。
- 每个 mutation 在单个 ``BEGIN IMMEDIATE`` 事务内「校验 + 写入 +
  commit」：进程在事务中途硬退出由 SQLite journal 自动回滚，绝不
  留下半提交状态；同 id 异内容在写入前即抛确定性冲突。
- 跨进程恢复：每个进程实例持有自己的连接，重开数据库后已保存
  事实原样可读，幂等重放与冲突语义与首写进程完全一致。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ._baseline import EvalBaselineRecord
from ._errors import EvalError, EvalRecordConflictError
from ._evaluator import EvaluatorResultRecord
from ._evidence import EvidenceArtifact
from ._identity import canonical_json
from ._observation import EvalObservation
from ._recommendation import ModelRecommendationRecord
from ._report import ReportRevisionRecord
from ._store import (
    EvalExecutionRecord,
    EvalExecutionView,
    EvalObservationView,
    EvalReportView,
    _report_view,
)

__all__ = ["SQLiteEvalStore"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_executions (
    execution_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_observations (
    observation_id TEXT PRIMARY KEY,
    execution_id TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_execution_observations (
    execution_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    observation_id TEXT NOT NULL,
    PRIMARY KEY (execution_id, seq),
    UNIQUE (execution_id, observation_id)
);
CREATE TABLE IF NOT EXISTS eval_evaluator_results (
    result_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_reports (
    report_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (report_id, revision)
);
CREATE TABLE IF NOT EXISTS eval_baselines (
    baseline_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eval_recommendations (
    recommendation_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


def _encode_external(item: object) -> object:
    """把一条 external evidence 编码为可持久化的判定结构。"""
    if isinstance(item, EvidenceArtifact):
        return {"kind": "evidence_artifact", "data": item.model_dump(mode="json")}
    if item is None or isinstance(item, (str, int, float, bool)):
        return {"kind": "json", "data": item}
    raise EvalError(
        "observation external evidence is not persistable in"
        " SQLiteEvalStore; only EvidenceArtifact and JSON scalars are"
        " supported"
    )


def _decode_external(item: object) -> object:
    """按编码标记还原 external evidence；损坏编码确定性失败。"""
    if isinstance(item, dict) and item.get("kind") == "evidence_artifact":
        return EvidenceArtifact.model_validate(item["data"])
    if isinstance(item, dict) and item.get("kind") == "json":
        return item.get("data")
    raise EvalError("corrupted external evidence encoding in the eval store")


def _observation_payload(observation: EvalObservation) -> str:
    """Observation 的规范 JSON（external evidence 特殊编码）。"""
    data = observation.model_dump(mode="json", exclude={"external_evidence"})
    data["external_evidence"] = [
        _encode_external(item) for item in observation.external_evidence
    ]
    return canonical_json(data)


def _observation_from_payload(payload: str) -> EvalObservation:
    """从规范 JSON 还原 Observation（external evidence 解码）。"""
    data = json.loads(payload)
    data["external_evidence"] = [
        _decode_external(item) for item in data.get("external_evidence", ())
    ]
    return EvalObservation.model_validate(data)


class SQLiteEvalStore:
    """把六类 Eval 事实持久化到单个 SQLite 文件的 append-only EvalStore。

    :param path: 数据库文件路径；父目录必须已存在。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SQLiteEvalStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def path(self) -> str:
        return self._path

    # -- 内部：append-only 通用写入 ------------------------------------

    def _append(
        self,
        table: str,
        key_columns: tuple[str, ...],
        key_values: tuple[object, ...],
        insert_sql: str,
        insert_values: tuple[object, ...],
        payload_column: str = "payload",
    ) -> sqlite3.Row | None:
        """``BEGIN IMMEDIATE`` 事务内完成「读旧值 + 比对 + 写入」。

        返回已存在行（调用方比对内容）；不存在则写入并返回 None。
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            where = " AND ".join(f"{column}=?" for column in key_columns)
            existing = self._conn.execute(
                f"SELECT * FROM {table} WHERE {where}", key_values
            ).fetchone()
            if existing is None:
                self._conn.execute(insert_sql, insert_values)
                self._conn.commit()
                return None
            self._conn.commit()
            return existing
        except BaseException:
            self._conn.rollback()
            raise

    @staticmethod
    def _conflict(kind: str, record_id: str) -> EvalRecordConflictError:
        return EvalRecordConflictError(
            f"{kind} {record_id!r} already stored with different content;"
            " eval records are immutable"
        )

    def _row_payload(self, row: sqlite3.Row) -> str:
        return str(row["payload"])

    # -- execution ------------------------------------------------------

    async def record_execution(
        self, execution: EvalExecutionRecord
    ) -> EvalExecutionRecord:
        existing = self._append(
            "eval_executions",
            ("execution_id",),
            (execution.execution_id,),
            "INSERT INTO eval_executions (execution_id, payload)"
            " VALUES (?,?)",
            (execution.execution_id, execution.model_dump_json()),
        )
        if existing is None:
            return execution
        stored = EvalExecutionRecord.model_validate_json(
            self._row_payload(existing)
        )
        if stored != execution:
            raise self._conflict("execution", execution.execution_id)
        return stored

    async def get_execution(
        self, execution_id: str
    ) -> EvalExecutionRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM eval_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        return EvalExecutionRecord.model_validate_json(self._row_payload(row))

    # -- observation ----------------------------------------------------

    async def record_observation(
        self, observation: EvalObservation
    ) -> EvalObservation:
        """append-only 写入 observation 并在**同一事务**内维护关联。

        observation 行与 execution 关联行在单个 ``BEGIN IMMEDIATE``
        事务内提交：进程在两者之间崩溃不会留下「已落盘但未关联」
        的永久缺口。幂等重放同样补建缺失的关联行（崩溃窗口的对账
        修复），同 id 异内容仍确定性冲突。
        """
        payload = _observation_payload(observation)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            existing = self._conn.execute(
                "SELECT payload FROM eval_observations WHERE"
                " observation_id=?",
                (observation.observation_id,),
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    "INSERT INTO eval_observations (observation_id,"
                    " execution_id, payload) VALUES (?,?,?)",
                    (
                        observation.observation_id,
                        observation.execution_id,
                        payload,
                    ),
                )
            else:
                stored = _observation_from_payload(str(existing["payload"]))
                if stored != observation:
                    raise self._conflict(
                        "observation", observation.observation_id
                    )
            if observation.execution_id is not None:
                self._link_observation_locked(
                    observation.execution_id, observation.observation_id
                )
            self._conn.commit()
            if existing is None:
                return observation
            return _observation_from_payload(str(existing["payload"]))
        except BaseException:
            self._conn.rollback()
            raise

    def _link_observation_locked(
        self, execution_id: str, observation_id: str
    ) -> None:
        """把 observation 追加进 execution 的有序关联（append 顺序）。

        调用方必须已持有 ``BEGIN IMMEDIATE`` 事务（与 observation
        写入同事务，崩溃窗口不存在）；已存在的关联是幂等 no-op。
        """
        existing = self._conn.execute(
            "SELECT 1 FROM eval_execution_observations WHERE"
            " execution_id=? AND observation_id=?",
            (execution_id, observation_id),
        ).fetchone()
        if existing is not None:
            return
        next_seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM"
            " eval_execution_observations WHERE execution_id=?",
            (execution_id,),
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO eval_execution_observations (execution_id,"
            " seq, observation_id) VALUES (?,?,?)",
            (execution_id, next_seq, observation_id),
        )

    async def get_observation(
        self, observation_id: str
    ) -> EvalObservation | None:
        row = self._conn.execute(
            "SELECT payload FROM eval_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        if row is None:
            return None
        return _observation_from_payload(self._row_payload(row))

    async def observation_view(
        self, observation_id: str
    ) -> EvalObservationView | None:
        observation = await self.get_observation(observation_id)
        if observation is None:
            return None
        return EvalObservationView(
            observation_id=observation.observation_id,
            mode=observation.mode.value,
            subject_run_id=observation.subject_run_id,
            definition_id=observation.definition_id,
            definition_version=observation.definition_version,
            variant_id=observation.variant_id,
            completeness=observation.completeness.value,
            reason_code=observation.reason_code,
            execution_id=observation.execution_id,
            collected_at=observation.collected_at,
        )

    async def execution_view(
        self, execution_id: str
    ) -> EvalExecutionView | None:
        execution = await self.get_execution(execution_id)
        if execution is None:
            return None
        rows = self._conn.execute(
            "SELECT observation_id FROM eval_execution_observations"
            " WHERE execution_id=? ORDER BY seq",
            (execution_id,),
        ).fetchall()
        return EvalExecutionView(
            execution_id=execution.execution_id,
            suite_id=execution.suite_id,
            suite_version=execution.suite_version,
            suite_digest=execution.suite_digest,
            mode=execution.mode,
            item_ids=execution.item_ids,
            created_at=execution.created_at,
            observation_ids=tuple(row["observation_id"] for row in rows),
        )

    # -- evaluator result ------------------------------------------------

    async def record_evaluator_result(
        self, result: EvaluatorResultRecord
    ) -> EvaluatorResultRecord:
        existing = self._append(
            "eval_evaluator_results",
            ("result_id",),
            (result.result_id,),
            "INSERT INTO eval_evaluator_results (result_id, payload)"
            " VALUES (?,?)",
            (result.result_id, result.model_dump_json()),
        )
        if existing is None:
            return result
        stored = EvaluatorResultRecord.model_validate_json(
            self._row_payload(existing)
        )
        if stored != result:
            raise self._conflict("evaluator result", result.result_id)
        return stored

    async def get_evaluator_result(
        self, result_id: str
    ) -> EvaluatorResultRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM eval_evaluator_results WHERE result_id=?",
            (result_id,),
        ).fetchone()
        if row is None:
            return None
        return EvaluatorResultRecord.model_validate_json(self._row_payload(row))

    # -- report revision ------------------------------------------------

    async def record_report(
        self, report: ReportRevisionRecord
    ) -> ReportRevisionRecord:
        existing = self._append(
            "eval_reports",
            ("report_id", "revision"),
            (report.report_id, report.revision),
            "INSERT INTO eval_reports (report_id, revision, payload)"
            " VALUES (?,?,?)",
            (report.report_id, report.revision, report.model_dump_json()),
        )
        if existing is None:
            return report
        stored = ReportRevisionRecord.model_validate_json(
            self._row_payload(existing)
        )
        if stored != report:
            raise self._conflict(
                f"report revision {report.report_id}#{report.revision}",
                report.report_id,
            )
        return stored

    async def get_report(
        self, report_id: str, revision: int
    ) -> ReportRevisionRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM eval_reports WHERE report_id=? AND"
            " revision=?",
            (report_id, revision),
        ).fetchone()
        if row is None:
            return None
        return ReportRevisionRecord.model_validate_json(self._row_payload(row))

    async def latest_report_revision(self, report_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(revision) FROM eval_reports WHERE report_id=?",
            (report_id,),
        ).fetchone()
        value = row[0] if row is not None else None
        return int(value) if value is not None else None

    async def report_view(
        self, report_id: str, revision: int
    ) -> EvalReportView | None:
        report = await self.get_report(report_id, revision)
        if report is None:
            return None
        return _report_view(report)

    # -- baseline / recommendation ---------------------------------------

    async def record_baseline(
        self, baseline: EvalBaselineRecord
    ) -> EvalBaselineRecord:
        existing = self._append(
            "eval_baselines",
            ("baseline_id",),
            (baseline.baseline_id,),
            "INSERT INTO eval_baselines (baseline_id, payload)"
            " VALUES (?,?)",
            (baseline.baseline_id, baseline.model_dump_json()),
        )
        if existing is None:
            return baseline
        stored = EvalBaselineRecord.model_validate_json(
            self._row_payload(existing)
        )
        if stored != baseline:
            raise self._conflict("baseline", baseline.baseline_id)
        return stored

    async def get_baseline(
        self, baseline_id: str
    ) -> EvalBaselineRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM eval_baselines WHERE baseline_id=?",
            (baseline_id,),
        ).fetchone()
        if row is None:
            return None
        return EvalBaselineRecord.model_validate_json(self._row_payload(row))

    async def record_recommendation(
        self, recommendation: ModelRecommendationRecord
    ) -> ModelRecommendationRecord:
        existing = self._append(
            "eval_recommendations",
            ("recommendation_id",),
            (recommendation.recommendation_id,),
            "INSERT INTO eval_recommendations (recommendation_id, payload)"
            " VALUES (?,?)",
            (recommendation.recommendation_id, recommendation.model_dump_json()),
        )
        if existing is None:
            return recommendation
        stored = ModelRecommendationRecord.model_validate_json(
            self._row_payload(existing)
        )
        if stored != recommendation:
            raise self._conflict(
                "recommendation", recommendation.recommendation_id
            )
        return stored

    async def get_recommendation(
        self, recommendation_id: str
    ) -> ModelRecommendationRecord | None:
        row = self._conn.execute(
            "SELECT payload FROM eval_recommendations WHERE"
            " recommendation_id=?",
            (recommendation_id,),
        ).fetchone()
        if row is None:
            return None
        return ModelRecommendationRecord.model_validate_json(
            self._row_payload(row)
        )
