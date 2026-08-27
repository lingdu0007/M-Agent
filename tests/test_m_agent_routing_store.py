"""Ticket 20: Routing Store 的不可变 Decision 持久化契约测试。

Store 只追加事实、绝不改写历史：
- 保存的 Decision（含候选过滤 reason trace、排序读数与结果）与产生
  它的完整 evidence snapshot 输入按原样往返；
- 同一 decision_id 的内容一致重放幂等，内容不一致是确定性冲突；
- Run identity 绑定可追加且幂等；
- 恢复或审计从不重新运行 Router，也不依据当前输入重算历史选择。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from m_agent.runtime import RunStatus
from m_agent._definition import DefinitionSnapshot
from m_agent._run import RunRecord
from m_agent.companion.routing import (
    InMemoryRoutingStore,
    ModelRouter,
    RoutingDecisionConflictError,
    RoutingPolicyIdentity,
    RoutingStoreError,
    SQLiteRoutingStore,
    bind_decision_to_run,
)

from routing_fixtures import (
    AS_OF,
    make_catalog,
    make_contract,
    make_evidence,
    make_entry,
    make_pricing,
    make_policy,
    make_variant,
)

SECONDARY_IDENTITY = RoutingPolicyIdentity(
    policy_id="finance-default", version="4"
)


class _RoutingCase:
    """一次成功路由 + 一条可绑定 Run 的冻结事实。"""

    def __init__(self, identity: RoutingPolicyIdentity | None = None) -> None:
        identities = (RoutingPolicyIdentity(
            policy_id="finance-default", version="3"
        ), SECONDARY_IDENTITY)
        economy = make_variant(
            "variant-economy",
            make_contract("contract-economy"),
            policy_identities=identities,
        )
        premium = make_variant(
            "variant-premium",
            make_contract("contract-premium"),
            policy_identities=identities,
        )
        self.economy = economy
        self.premium = premium
        self.catalog = make_catalog(make_entry(economy), make_entry(premium))
        self.evidence = make_evidence((economy, premium))
        result = ModelRouter().select(
            catalog=self.catalog,
            policy=make_policy(identity=identity),
            evidence=self.evidence,
            as_of=AS_OF,
        )
        assert result.decision is not None
        self.decision = result.decision

    def run(self, run_id: str = "run-1") -> RunRecord:
        variant = self.decision.selected_variant
        return RunRecord(
            run_id=run_id,
            definition_id=variant.definition_id,
            definition_version=variant.definition_version,
            input="route me",
            status=RunStatus.CREATED,
            snapshot=DefinitionSnapshot(
                definition_id=variant.definition_id,
                version=variant.definition_version,
                instructions="offline agent.",
                model_bindings=variant.model_bindings,
            ),
        )


class _RoutingStoreContract:
    """InMemory 与 SQLite 共享的行为契约。"""

    def make_store(self):  # noqa: ANN201 - overridden per backend
        raise NotImplementedError

    def setUp(self) -> None:
        self.store = self.make_store()
        self.case = _RoutingCase()
        self.other_case = _RoutingCase(identity=SECONDARY_IDENTITY)

    def test_round_trip_returns_the_frozen_fact_verbatim(self) -> None:
        self.store.save_decision(
            self.case.decision, evidence=self.case.evidence
        )
        stored = self.store.load_decision(self.case.decision.decision_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.decision, self.case.decision)
        self.assertEqual(stored.evidence, self.case.evidence)
        self.assertEqual(stored.run_bindings, ())

    def test_replaying_the_same_decision_is_idempotent(self) -> None:
        self.store.save_decision(
            self.case.decision, evidence=self.case.evidence
        )
        self.store.save_decision(
            self.case.decision, evidence=self.case.evidence
        )
        self.assertEqual(
            self.store.decision_ids(), (self.case.decision.decision_id,)
        )

    def test_conflicting_content_for_the_same_id_fails(self) -> None:
        self.store.save_decision(
            self.case.decision, evidence=self.case.evidence
        )
        tampered = self.case.decision.model_copy(
            update={"catalog_version": "999"}
        )
        with self.assertRaises(RoutingDecisionConflictError):
            self.store.save_decision(tampered, evidence=self.case.evidence)

    def test_saving_with_mismatched_evidence_digest_fails(self) -> None:
        other_evidence = make_evidence(
            (self.case.economy, self.case.premium),
            pricing=(
                make_pricing(self.case.economy, input_price="4.50"),
                make_pricing(self.case.premium, input_price="9.00"),
            ),
        )
        with self.assertRaises(RoutingStoreError):
            self.store.save_decision(
                self.case.decision, evidence=other_evidence
            )

    def test_load_unknown_decision_returns_none(self) -> None:
        self.assertIsNone(self.store.load_decision("decision-missing"))

    def test_run_bindings_append_idempotently_and_survive_load(self) -> None:
        decision = self.case.decision
        self.store.save_decision(decision, evidence=self.case.evidence)
        binding = bind_decision_to_run(decision, self.case.run("run-1"))
        self.store.attach_run_binding(decision.decision_id, binding)
        self.store.attach_run_binding(decision.decision_id, binding)
        stored = self.store.load_decision(decision.decision_id)
        assert stored is not None
        self.assertEqual(stored.run_bindings, (binding,))
        second = bind_decision_to_run(decision, self.case.run("run-2"))
        self.store.attach_run_binding(decision.decision_id, second)
        merged = self.store.load_decision(decision.decision_id)
        assert merged is not None
        self.assertEqual(merged.run_bindings, (binding, second))

    def test_binding_an_unknown_decision_fails(self) -> None:
        binding = bind_decision_to_run(
            self.case.decision, self.case.run("run-1")
        )
        with self.assertRaises(RoutingStoreError):
            self.store.attach_run_binding("decision-missing", binding)

    def test_binding_for_another_decision_fails(self) -> None:
        self.store.save_decision(
            self.case.decision, evidence=self.case.evidence
        )
        binding = bind_decision_to_run(
            self.other_case.decision, self.other_case.run("run-9")
        )
        with self.assertRaises(RoutingStoreError):
            self.store.attach_run_binding(
                self.case.decision.decision_id, binding
            )

    def test_decision_ids_preserve_save_order(self) -> None:
        first = self.case.decision
        second = self.other_case.decision
        self.assertNotEqual(first.decision_id, second.decision_id)
        self.store.save_decision(first, evidence=self.case.evidence)
        self.store.save_decision(second, evidence=self.other_case.evidence)
        self.assertEqual(
            self.store.decision_ids(),
            (first.decision_id, second.decision_id),
        )


class InMemoryRoutingStoreTests(
    _RoutingStoreContract, unittest.TestCase
):
    def make_store(self):  # noqa: ANN201
        return InMemoryRoutingStore()


class SQLiteRoutingStoreTests(_RoutingStoreContract, unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()  # noqa: SIM115
        self._path = Path(self._tmp.name) / "routing-store.sqlite3"
        super().setUp()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def make_store(self):  # noqa: ANN201
        return SQLiteRoutingStore(self._path)

    def test_reopened_database_returns_history_without_recomputation(
        self,
    ) -> None:
        """durable 边界：重开 Store 后事实原样可读，绝不重算。"""
        decision = self.case.decision
        binding = bind_decision_to_run(decision, self.case.run("run-1"))
        self.store.save_decision(decision, evidence=self.case.evidence)
        self.store.attach_run_binding(decision.decision_id, binding)
        self.store.close()

        reopened = SQLiteRoutingStore(self._path)
        try:
            stored = reopened.load_decision(decision.decision_id)
        finally:
            reopened.close()
        assert stored is not None
        self.assertEqual(stored.decision, decision)
        self.assertEqual(stored.evidence, self.case.evidence)
        self.assertEqual(stored.run_bindings, (binding,))

    def test_reopened_database_rejects_conflicting_content(self) -> None:
        decision = self.case.decision
        self.store.save_decision(decision, evidence=self.case.evidence)
        self.store.close()
        reopened = SQLiteRoutingStore(self._path)
        try:
            tampered = decision.model_copy(update={"catalog_version": "999"})
            with self.assertRaises(RoutingDecisionConflictError):
                reopened.save_decision(
                    tampered, evidence=self.case.evidence
                )
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
