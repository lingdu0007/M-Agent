"""Ticket 17 AC 8：InMemory EvalStore 的不可变身份与最小公开 view。

execution 与 observation 以不可变身份 append-only 保存：同内容重复
记录幂等，异内容同身份确定性冲突；公开 view 足以支撑验收，同时
不暴露未授权 payload（run input/output/history 不出现在 view 序列化
结果中）。
"""

from __future__ import annotations

import unittest
from typing import Any

from m_agent.companion.eval import (
    EvalExecutionRecord,
    EvalMode,
    EvalObservation,
    EvalRecordConflictError,
    EvidenceCompleteness,
    InMemoryEvalStore,
)
from m_agent.runtime import RunStatus


def _observation(**overrides) -> EvalObservation:
    values: dict[str, Any] = dict(
        observation_id="obs-1",
        mode=EvalMode.EXECUTE,
        subject_run_id="run-1",
        definition_id="assistant",
        definition_version="1.0",
        completeness=EvidenceCompleteness.COMPLETE,
        reason_code="SUBJECT_TERMINAL",
        execution_id="exec-1",
        run_status=RunStatus.SUCCEEDED,
        run_input="secret-grade input",
        run_output="secret-grade output",
    )
    values.update(overrides)
    return EvalObservation(**values)


def _execution(**overrides) -> EvalExecutionRecord:
    values: dict[str, Any] = dict(
        execution_id="exec-1",
        suite_id="suite-1",
        suite_version="1.0",
        suite_digest="d" * 64,
        mode=EvalMode.EXECUTE,
        item_ids=("item-1",),
    )
    values.update(overrides)
    return EvalExecutionRecord(**values)


class InMemoryEvalStoreTests(unittest.IsolatedAsyncioTestCase):
    """AC 8：不可变身份与公开 view。"""

    async def test_observation_identity_is_immutable(self) -> None:
        store = InMemoryEvalStore()
        observation = _observation()
        await store.record_observation(observation)
        # 同内容幂等重放：崩溃恢复安全的 append-only 语义。
        await store.record_observation(observation)
        self.assertEqual(
            await store.get_observation("obs-1"), observation
        )
        conflicting = _observation(run_output="different output")
        with self.assertRaises(EvalRecordConflictError):
            await store.record_observation(conflicting)
        # 冲突不改动已保存事实。
        self.assertEqual(
            await store.get_observation("obs-1"), observation
        )

    async def test_execution_identity_is_immutable(self) -> None:
        store = InMemoryEvalStore()
        execution = _execution()
        await store.record_execution(execution)
        await store.record_execution(execution)
        self.assertEqual(await store.get_execution("exec-1"), execution)
        with self.assertRaises(EvalRecordConflictError):
            await store.record_execution(
                _execution(item_ids=("item-1", "item-2"))
            )
        self.assertEqual(await store.get_execution("exec-1"), execution)

    async def test_observation_view_hides_unauthorized_payloads(self) -> None:
        store = InMemoryEvalStore()
        await store.record_observation(_observation())
        view = await store.observation_view("obs-1")
        self.assertIsNotNone(view)
        assert view is not None  # type narrowing for mypy
        self.assertEqual(view.observation_id, "obs-1")
        self.assertEqual(view.completeness, EvidenceCompleteness.COMPLETE)
        self.assertEqual(view.subject_run_id, "run-1")
        # view 是最小公开面：不携带 run input/output/history 任何 payload。
        dumped = view.model_dump_json()
        self.assertNotIn("secret-grade", dumped)
        for forbidden in ("run_input", "run_output", "conversation_history"):
            self.assertNotIn(forbidden, view.model_dump())

    async def test_execution_view_links_observation_ids(self) -> None:
        store = InMemoryEvalStore()
        await store.record_execution(_execution())
        await store.record_observation(_observation())
        view = await store.execution_view("exec-1")
        self.assertIsNotNone(view)
        assert view is not None  # type narrowing for mypy
        self.assertEqual(view.execution_id, "exec-1")
        self.assertEqual(view.suite_digest, "d" * 64)
        self.assertEqual(view.observation_ids, ("obs-1",))

    async def test_unknown_ids_return_none(self) -> None:
        store = InMemoryEvalStore()
        self.assertIsNone(await store.get_observation("missing"))
        self.assertIsNone(await store.get_execution("missing"))
        self.assertIsNone(await store.observation_view("missing"))
        self.assertIsNone(await store.execution_view("missing"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
