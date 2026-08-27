"""Ticket 17 AC 3/AC 4：OBSERVE 严格只读与 immutable Observation 状态。

可复现证据：

- 只读探针 Store 记录 OBSERVE 全程零 mutation 调用，写入前后状态
  对比一致；get_run 只以显式选择的 run_id 被调用（无扫描）；
- missing -> UNAVAILABLE、非终态 -> INCONCLUSIVE、终态 -> COMPLETE；
- sampled selection -> SAMPLED 且不得表述为全量证明；
- Observation 不可变；SAMPLED 必须携带 sampling 披露。
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from m_agent.adapters import (
    DeterministicModelAdapter,
    InMemoryRunStore,
    PlaintextPayloadCodec,
)
from m_agent.companion.eval import (
    REASON_SAMPLING_APPLIED,
    REASON_SUBJECT_NOT_TERMINAL,
    REASON_SUBJECT_RUN_MISSING,
    REASON_SUBJECT_TERMINAL,
    EvidenceCompleteness,
    EvalMode,
    EvalObservation,
    ObservationSelection,
    SamplingDisclosure,
)
from m_agent.runtime import (
    AgentDefinition,
    DefinitionRegistry,
    RunStatus,
    Runner,
)


class _ZeroWriteProbeStore:
    """OBSERVE 零写入探针：读方法放行并记录，写方法直接失败。"""

    _WRITES = frozenset({
        "create_run", "acquire_lease", "release_lease", "transition_run",
        "record_step", "record_attempt", "record_checkpoint",
        "record_policy_decision", "commit_turn", "claim_run",
        "release_claim", "prepare_model_dispatch", "reserve_model_attempt",
    })

    def __init__(self, inner):  # noqa: ANN001
        self._inner = inner
        self.read_calls: list[tuple[str, tuple]] = []
        self.mutation_calls: list[str] = []
        self.get_run_ids: list[str] = []
        self._armed = False

    def arm(self) -> None:
        """播种完成后武装：此后任何 mutation 调用都让探针失败。"""
        # 武装即开启新的观测窗口：播种阶段的放行留痕一并清空，
        # mutation_calls 只记录观测窗口内的违规调用。
        self.mutation_calls.clear()
        self._armed = True

    def __getattr__(self, name):  # noqa: ANN001
        if name in _ZeroWriteProbeStore._WRITES:
            async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
                self.mutation_calls.append(name)
                if self._armed:
                    raise AssertionError(
                        f"OBSERVE must not mutate subject state: {name}"
                    )
                return await getattr(self._inner, name)(*args, **kwargs)
            return forbidden

        async def read(*args, **kwargs):  # noqa: ANN002, ANN003
            self.read_calls.append((name, args))
            if name == "get_run":
                self.get_run_ids.append(args[0])
            return await getattr(self._inner, name)(*args, **kwargs)
        return read

    async def state_snapshot(self, run_ids):  # noqa: ANN001
        return {
            run_id: await self._inner.get_run(run_id) for run_id in run_ids
        }


def _runner(store) -> Runner:
    adapter = DeterministicModelAdapter(responses=("final answer",))
    registry = DefinitionRegistry()
    registry.register(
        AgentDefinition.for_adapter(
            definition_id="assistant",
            version="1.0",
            instructions="i",
            model_adapter=adapter,
        )
    )
    return Runner(registry=registry, store=store)


async def _seed_runs(probe) -> None:  # noqa: ANN001
    runner = _runner(probe)
    await runner.create_run("assistant", "1.0", "a", run_id="run-a")
    await runner.start_run("run-a")
    await runner.create_run("assistant", "1.0", "b", run_id="run-b")
    await runner.start_run("run-b")
    # run-c 只创建不启动：OBSERVE 必须给出 INCONCLUSIVE 而非伪造结论。
    await runner.create_run("assistant", "1.0", "c", run_id="run-c")


class ObserveReadOnlyTests(unittest.IsolatedAsyncioTestCase):
    """AC 3：显式选择 + 零写入 + 无扫描。"""

    async def test_observe_reads_only_explicitly_selected_runs(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        await _seed_runs(probe)
        probe.read_calls.clear()
        probe.get_run_ids.clear()
        probe.arm()

        observer = EvalObserver(runner=_runner(probe))
        selection = ObservationSelection(
            selection_id="selection-1",
            version="1.0",
            run_ids=("run-a",),
        )
        observations = await observer.observe_selection(selection)

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].subject_run_id, "run-a")
        # get_run 只以显式选择的 run_id 调用：没有扫描其他 Run。
        self.assertEqual(probe.get_run_ids, ["run-a"])

    async def test_observe_is_zero_write_with_state_comparison(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        await _seed_runs(probe)
        known = ["run-a", "run-b", "run-c"]
        before = await probe.state_snapshot(known)
        probe.read_calls.clear()
        probe.arm()

        observer = EvalObserver(runner=_runner(probe))
        observations = await observer.observe_selection(
            ObservationSelection(
                selection_id="selection-1",
                version="1.0",
                run_ids=("run-a", "run-b", "run-c"),
            )
        )
        self.assertEqual(len(observations), 3)

        # 零写入：任何 mutation 方法被调用都会让探针直接失败并留痕。
        self.assertEqual(probe.mutation_calls, [])
        after = await probe.state_snapshot(known)
        self.assertEqual(before, after)

    async def test_observe_selection_rejects_duplicates(self) -> None:
        with self.assertRaises(ValidationError):
            ObservationSelection(
                selection_id="selection-1",
                version="1.0",
                run_ids=("run-a", "run-a"),
            )


class ObserveNormalizationTests(unittest.IsolatedAsyncioTestCase):
    """AC 4：OBSERVE 归一化的稳定 completeness 状态。"""

    async def test_missing_run_normalizes_unavailable(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        observer = EvalObserver(runner=_runner(probe))
        observation = await observer.observe_run("run-missing")
        self.assertEqual(observation.completeness, EvidenceCompleteness.UNAVAILABLE)
        self.assertEqual(observation.reason_code, REASON_SUBJECT_RUN_MISSING)
        self.assertIsNone(observation.run_status)

    async def test_nonterminal_run_normalizes_inconclusive(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        await _seed_runs(probe)
        probe.arm()
        observer = EvalObserver(runner=_runner(probe))
        observation = await observer.observe_run("run-c")
        self.assertEqual(observation.completeness, EvidenceCompleteness.INCONCLUSIVE)
        self.assertEqual(observation.reason_code, REASON_SUBJECT_NOT_TERMINAL)
        self.assertEqual(observation.run_status, RunStatus.CREATED)

    async def test_terminal_run_normalizes_complete(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        await _seed_runs(probe)
        probe.arm()
        observer = EvalObserver(runner=_runner(probe))
        observation = await observer.observe_run("run-a")
        self.assertEqual(observation.completeness, EvidenceCompleteness.COMPLETE)
        self.assertEqual(observation.reason_code, REASON_SUBJECT_TERMINAL)
        self.assertEqual(observation.run_status, RunStatus.SUCCEEDED)
        self.assertEqual(observation.run_output, "final answer")
        self.assertEqual(observation.mode, EvalMode.OBSERVE)
        self.assertTrue(observation.claims_full_population())


class ObservationEvidenceStatesTests(unittest.IsolatedAsyncioTestCase):
    """AC 4：sampled 披露与 Observation 不可变性。"""

    async def test_sampled_selection_marks_sampled_without_full_population(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        await _seed_runs(probe)
        probe.arm()
        observer = EvalObserver(runner=_runner(probe))
        observations = await observer.observe_selection(
            ObservationSelection(
                selection_id="selection-1",
                version="1.0",
                run_ids=("run-a",),
                sampling=SamplingDisclosure(
                    selection_method="RANDOM_SEED",
                    seed="42",
                    included=1,
                    candidates=3,
                ),
            )
        )
        observation = observations[0]
        self.assertEqual(observation.completeness, EvidenceCompleteness.SAMPLED)
        self.assertEqual(observation.reason_code, REASON_SAMPLING_APPLIED)
        self.assertIsNotNone(observation.sampling)
        assert observation.sampling is not None  # type narrowing for mypy
        self.assertEqual(observation.sampling.included, 1)
        self.assertEqual(observation.sampling.candidates, 3)
        # sampled 证据不得表述为全量证明。
        self.assertFalse(observation.claims_full_population())

    async def test_sampled_underlying_missing_stays_unavailable(self) -> None:
        from m_agent.companion.eval import EvalObserver

        probe = _ZeroWriteProbeStore(
            InMemoryRunStore(PlaintextPayloadCodec())
        )
        observer = EvalObserver(runner=_runner(probe))
        observations = await observer.observe_selection(
            ObservationSelection(
                selection_id="selection-1",
                version="1.0",
                run_ids=("run-missing",),
                sampling=SamplingDisclosure(
                    selection_method="RANDOM_SEED",
                    seed="42",
                    included=1,
                    candidates=5,
                ),
            )
        )
        self.assertEqual(
            observations[0].completeness, EvidenceCompleteness.UNAVAILABLE
        )
        # 披露仍然保留：sampling 元数据不因更严重状态而丢失。
        self.assertIsNotNone(observations[0].sampling)

    async def test_observation_is_immutable(self) -> None:
        observation = EvalObservation(
            observation_id="obs-1",
            mode=EvalMode.OBSERVE,
            subject_run_id="run-a",
            definition_id="assistant",
            definition_version="1.0",
            completeness=EvidenceCompleteness.COMPLETE,
            reason_code=REASON_SUBJECT_TERMINAL,
        )
        with self.assertRaises(ValidationError):
            observation.run_output = "tampered"  # type: ignore[misc]

    async def test_sampled_observation_requires_disclosure(self) -> None:
        with self.assertRaises(ValidationError):
            EvalObservation(
                observation_id="obs-1",
                mode=EvalMode.OBSERVE,
                subject_run_id="run-a",
                definition_id="assistant",
                definition_version="1.0",
                completeness=EvidenceCompleteness.SAMPLED,
                reason_code=REASON_SAMPLING_APPLIED,
                sampling=None,
            )

    def test_completeness_states_are_distinct(self) -> None:
        values = {state.value for state in EvidenceCompleteness}
        self.assertEqual(
            values,
            {"COMPLETE", "SAMPLED", "UNSUPPORTED", "UNAVAILABLE", "INCONCLUSIVE"},
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
