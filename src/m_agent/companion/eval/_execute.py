"""EXECUTE：隔离 Eval Store 与 fixture boundary 内的受控执行（AC 2）。

EvalExecutor 只在调用方显式提供的隔离 RunStore（以及可选的隔离
SessionStore）中创建 subject Run：生产 Store 从不进入本组件。fixture
boundary fail-closed：live 语义 Model Adapter 或 fixture 未声明的外部
效果工具在任何 Run 创建之前即被拒绝；Case 声明的静态能力缺失时产生
UNSUPPORTED Observation（零 Run、零 provider 请求）。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from ..._clock import Clock, SystemClock
from ..._definition import DefinitionRegistry
from ..._errors import DefinitionNotFoundError
from ..._runner import Runner
from ..._store import RunStore
from ..._tools import ToolEffect
from .._session import SessionScope, SessionStore, SessionTurn
from ._case import EvalSuite, EvalSuiteItem
from ._errors import EvalFixtureBoundaryError
from ._identity import digest_of
from ._observation import (
    REASON_MODEL_CAPABILITIES_MISSING,
    REASON_VARIANT_UNRESOLVED,
    EvalMode,
    EvalObservation,
    EvidenceCompleteness,
    observation_from_inspection,
)
from ._store import EvalExecutionRecord, EvalStore

__all__ = [
    "EvalExecutor",
    "EvalFixtureBoundaryError",
    "EvalSuiteExecutionResult",
]


class EvalSuiteExecutionResult(BaseModel):
    """一次 Suite 执行的可观测结果（execution + 逐 item Observation）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    execution: EvalExecutionRecord
    observations: tuple[EvalObservation, ...]


class EvalExecutor:
    """在隔离 Eval Store 中执行 subject Run 的受控执行器。

    :param registry: Variant 指向的 Definition 注册表。
    :param run_store: 调用方提供的**隔离** Eval RunStore；生产 Store
        绝不传入本组件。
    :param session_store: 可选的**隔离** Eval SessionStore；提供时
        subject Run 的成功 Turn 提交到该 Store 的 eval 专属 scope。
    :param clock: 可注入时钟（Turn 时间戳的确定性来源）。
    """

    def __init__(
        self,
        *,
        registry: DefinitionRegistry,
        run_store: RunStore,
        session_store: SessionStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._registry = registry
        self._session_store = session_store
        self._clock = clock if clock is not None else SystemClock()
        self._runner = Runner(registry=registry, store=run_store)

    # -- 确定性身份 helper（恢复/重放共用，Ticket 18 依赖此稳定性） ----

    @staticmethod
    def subject_run_id(execution_id: str, item: EvalSuiteItem) -> str:
        """由 execution + item 派生的稳定 subject Run identity。"""
        return "eval-run-" + digest_of(
            "eval-subject-run", execution_id, item.item_id
        )[:32]

    @staticmethod
    def session_scope(execution_id: str) -> SessionScope:
        """隔离 SessionStore 中 eval 专属的 opaque scope。"""
        return SessionScope(token=f"eval-execution:{execution_id}")

    @staticmethod
    def session_id(item: EvalSuiteItem) -> str:
        """每个 Suite item 独占的隔离 Session identity。"""
        return f"eval-item:{item.item_id}"

    @staticmethod
    def observation_id(execution_id: str, item: EvalSuiteItem) -> str:
        """EXECUTE Observation 的确定性身份。"""
        return digest_of(
            "eval-observation", EvalMode.EXECUTE.value, execution_id,
            item.item_id,
        )

    # -- 单 item 执行 ----------------------------------------------------

    async def execute_item(
        self,
        *,
        suite: EvalSuite,
        item: EvalSuiteItem,
        execution_id: str,
    ) -> EvalObservation:
        """在 fixture boundary 内执行一个 Suite item 并归一化 Observation。

        顺序：解析 Variant -> fixture boundary 检查 -> 静态能力门槛
        （UNSUPPORTED 短路，零副作用）->（可选）隔离 Session claim ->
        创建并推进 subject Run ->（可选）按权威终态提交/释放 claim
        -> 归一化 immutable Observation。
        """
        case = suite.case_by_id(item.case_id)
        variant = item.variant
        run_id = self.subject_run_id(execution_id, item)
        try:
            definition = self._registry.resolve(
                variant.definition_id, variant.definition_version
            )
        except DefinitionNotFoundError:
            return EvalObservation(
                observation_id=self.observation_id(execution_id, item),
                mode=EvalMode.EXECUTE,
                subject_run_id=run_id,
                completeness=EvidenceCompleteness.UNAVAILABLE,
                reason_code=REASON_VARIANT_UNRESOLVED,
                execution_id=execution_id,
                variant_id=variant.variant_id,
            )
        self._enforce_fixture_boundary(case, definition)
        required = case.required_capabilities
        if required is not None and not self._capabilities_supported(
            definition, required
        ):
            return EvalObservation(
                observation_id=self.observation_id(execution_id, item),
                mode=EvalMode.EXECUTE,
                subject_run_id=run_id,
                completeness=EvidenceCompleteness.UNSUPPORTED,
                reason_code=REASON_MODEL_CAPABILITIES_MISSING,
                execution_id=execution_id,
                variant_id=variant.variant_id,
            )

        claim_binding = await self._claim_session(execution_id, item, run_id)
        try:
            await self._runner.create_run(
                definition.definition_id,
                definition.version,
                case.input,
                run_id=run_id,
                history=case.history,
            )
        except BaseException:
            # Run 从未创建：回滚隔离 claim（残余 claim 交给对账）。
            if (
                claim_binding is not None
                and self._session_store is not None
            ):
                scope, session_id, _version = claim_binding
                try:
                    await self._session_store.release_claim(
                        scope, session_id, run_id
                    )
                except Exception:
                    pass
            raise
        run = await self._runner.start_run(run_id)
        await self._complete_session(execution_id, item, run, claim_binding)
        inspection = await self._runner.inspect_run(run_id)
        return observation_from_inspection(
            observation_id=self.observation_id(execution_id, item),
            mode=EvalMode.EXECUTE,
            inspection=inspection,
            subject_run_id=run_id,
            execution_id=execution_id,
            variant_id=variant.variant_id,
        )

    # -- Suite 执行 ------------------------------------------------------

    async def execute_suite(
        self,
        suite: EvalSuite,
        *,
        store: EvalStore | None = None,
    ) -> EvalSuiteExecutionResult:
        """展开并执行整个 Suite；可选地把 execution 与 observations
        以不可变身份记录进 EvalStore。

        Suite 展开失败（如 Case Variant 未注册）在任何 Run 创建之前
        抛 :class:`m_agent.companion.eval.EvalSuiteError`。
        """
        items = suite.expand()
        execution = EvalExecutionRecord(
            execution_id=f"eval-exec-{uuid.uuid4().hex}",
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_digest=suite.content_digest(),
            mode=EvalMode.EXECUTE.value,
            item_ids=tuple(item.item_id for item in items),
        )
        if store is not None:
            await store.record_execution(execution)
        observations: list[EvalObservation] = []
        for item in items:
            observation = await self.execute_item(
                suite=suite, item=item, execution_id=execution.execution_id
            )
            if store is not None:
                await store.record_observation(observation)
            observations.append(observation)
        return EvalSuiteExecutionResult(
            execution=execution,
            observations=tuple(observations),
        )

    # -- 内部：fixture boundary 与能力门槛 -------------------------------

    def _enforce_fixture_boundary(self, case, definition) -> None:  # noqa: ANN001
        """fixture 之外的访问 fail-closed（零 Run、零 dispatch）。

        规则：

        - 所有 Model Adapter 必须是确定性 fake（live 语义 Adapter 属
          显式授权的 provider 工作，绝不进入离线 EXECUTE）；
        - effect 非 READ_ONLY 的工具、或任何 live（非确定性）工具，
          必须在 Case 的 Fixture Bundle ``declared_external_effects``
          中显式声明。
        """
        bundle = case.fixture_bundle
        adapters = [definition.model_adapter]
        adapters.extend(definition.model_adapters.values())
        for adapter in adapters:
            if getattr(adapter, "deterministic", False) is not True:
                raise EvalFixtureBoundaryError(
                    "eval EXECUTE requires deterministic model adapters; "
                    f"adapter {type(adapter).__name__} has live semantics "
                    "and is outside the fixture boundary"
                )
        for tool in definition.tools:
            declared = tool.name in bundle.declared_external_effects
            if declared:
                continue
            live_tool = getattr(tool, "deterministic", False) is not True
            external_effect = tool.effect is not ToolEffect.READ_ONLY
            if live_tool or external_effect:
                raise EvalFixtureBoundaryError(
                    f"tool {tool.name!r} accesses systems outside the "
                    "fixture bundle; declare it in "
                    "declared_external_effects or remove it from the "
                    "evaluated definition"
                )

    @staticmethod
    def _capabilities_supported(definition, required) -> bool:  # noqa: ANN001
        capabilities = getattr(definition.model_adapter, "capabilities", None)
        if capabilities is None:
            return False
        return capabilities.supports(required)

    # -- 内部：隔离 SessionStore 生命周期 --------------------------------

    async def _claim_session(
        self, execution_id: str, item: EvalSuiteItem, run_id: str
    ):
        """在隔离 SessionStore 中为 subject Run 建立原子 claim。

        返回 (scope, session_id, frozen_version)；未配置隔离 Store 时
        返回 None（sessionless 执行）。
        """
        if self._session_store is None:
            return None
        scope = self.session_scope(execution_id)
        session_id = self.session_id(item)
        record = await self._session_store.get_session(scope, session_id)
        if record is None:
            record = await self._session_store.create_session(
                scope, session_id
            )
        claim = await self._session_store.claim_run(
            scope, session_id, run_id, expected_version=record.version
        )
        return scope, session_id, claim.session_version

    async def _complete_session(
        self, execution_id: str, item: EvalSuiteItem, run, claim_binding  # noqa: ANN001
    ) -> None:
        """按权威 Run 终态完成隔离 Session 生命周期。

        SUCCEEDED -> 提交一条最小 Turn；REJECTED/FAILED/CANCELLED ->
        释放 claim；非终态（WAITING 等）-> 保留 claim。SessionStore
        故障不改写 Core 终态（与 SessionRunner 相同的分离原则）。
        """
        if claim_binding is None:
            return
        scope, session_id, frozen_version = claim_binding
        assert self._session_store is not None  # noqa: S101 - 类型收窄
        if run.status.value == "SUCCEEDED":
            turn = SessionTurn(
                turn_id=digest_of(
                    "eval-turn", execution_id, item.item_id
                ),
                session_id=session_id,
                run_id=run.run_id,
                definition_id=run.definition_id,
                definition_version=run.definition_version,
                user_input=run.input,
                assistant_output=run.output or "",
                created_at=self._clock.now(),
            )
            try:
                await self._session_store.commit_turn(
                    scope, session_id, turn, expected_version=frozen_version
                )
            except Exception:
                return  # 提交 pending：保留 claim，交由对账；不改写终态
            return
        if run.status.is_terminal:
            try:
                await self._session_store.release_claim(
                    scope, session_id, run.run_id
                )
            except Exception:
                return  # 释放失败：残余 claim 交给对账
