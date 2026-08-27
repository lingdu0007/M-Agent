"""Ticket 18 AC 1：InMemoryEvalStore 与 SQLiteEvalStore 通过共享契约套件。

两种实现各自绑定 ``tests/eval_store_contract.py`` 的实现无关契约；
SQLite 额外验证跨进程重开数据库后已保存事实原样可读、幂等重放与
冲突语义与首写进程一致（durable 恢复基础）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from eval_store_contract import EvalStoreContractMixin
from m_agent.companion.eval import InMemoryEvalStore, SQLiteEvalStore


class InMemoryEvalStoreContractTests(
    EvalStoreContractMixin, unittest.IsolatedAsyncioTestCase
):
    """InMemoryEvalStore 绑定共享契约套件。"""

    def make_store(self):
        return InMemoryEvalStore()


class SQLiteEvalStoreContractTests(
    EvalStoreContractMixin, unittest.IsolatedAsyncioTestCase
):
    """SQLiteEvalStore 绑定同一共享契约套件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "eval-store.sqlite3"

    def make_store(self):
        store = SQLiteEvalStore(self._path)
        self.addCleanup(store.close)
        return store


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
import unittest

class SQLiteEvalStoreReopenTests(unittest.IsolatedAsyncioTestCase):
    """SQLite 专属：跨进程重开的 durable 语义。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._path = Path(self._tmp.name) / "eval-store.sqlite3"

    async def test_reopen_preserves_records_and_conflict_semantics(self) -> None:
        from m_agent.companion.eval import (
            EvalBaselineRecord,
            EvalExecutionRecord,
            EvalMode,
            EvalObservation,
            EvalRecordConflictError,
            ComparisonPolicy,
            EvaluatorOutcome,
            EvaluatorRef,
            EvaluatorResultRecord,
            EvidenceCompleteness,
            SQLiteEvalStore,
        )
        from m_agent.runtime import RunStatus

        execution = EvalExecutionRecord(
            execution_id="exec-1",
            suite_id="suite-1",
            suite_version="1.0",
            suite_digest="d" * 64,
            mode=EvalMode.EXECUTE,
            item_ids=("item-1",),
        )
        observation = EvalObservation(
            observation_id="obs-1",
            mode=EvalMode.EXECUTE,
            subject_run_id="run-1",
            completeness=EvidenceCompleteness.COMPLETE,
            reason_code="SUBJECT_TERMINAL",
            execution_id="exec-1",
            run_status=RunStatus.SUCCEEDED,
        )
        result = EvaluatorResultRecord(
            result_id="result-1",
            execution_id="exec-1",
            observation_id="obs-1",
            item_id="item-1",
            evaluator=EvaluatorRef(
                evaluator_id="output-match", version="1.0"
            ),
            outcome=EvaluatorOutcome.PASS,
            failure_kind="NONE",
            reason_code="SUBJECT_OUTPUT_MATCHED",
        )
        baseline = EvalBaselineRecord(
            baseline_id="baseline-1",
            report_id="report-1",
            report_revision=1,
            suite_id="suite-1",
            suite_version="1.0",
            comparison_policy=ComparisonPolicy(
                policy_id="compare", version="1.0"
            ),
        )
        with SQLiteEvalStore(self._path) as store:
            await store.record_execution(execution)
            await store.record_observation(observation)
            await store.record_evaluator_result(result)
            await store.record_baseline(baseline)

        with SQLiteEvalStore(self._path) as reopened:
            self.assertEqual(
                await reopened.get_execution("exec-1"), execution
            )
            self.assertEqual(
                await reopened.get_observation("obs-1"), observation
            )
            self.assertEqual(
                await reopened.get_evaluator_result("result-1"), result
            )
            self.assertEqual(
                await reopened.get_baseline("baseline-1"), baseline
            )
            view = await reopened.execution_view("exec-1")
            self.assertIsNotNone(view)
            assert view is not None  # type narrowing for mypy
            self.assertEqual(view.observation_ids, ("obs-1",))
            await reopened.record_observation(observation)
            with self.assertRaises(EvalRecordConflictError):
                await reopened.record_execution(
                    EvalExecutionRecord(
                        execution_id="exec-1",
                        suite_id="suite-1",
                        suite_version="1.0",
                        suite_digest="d" * 64,
                        mode=EvalMode.EXECUTE,
                        item_ids=("item-1", "item-2"),
                    )
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
