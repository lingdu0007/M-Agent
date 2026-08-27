"""LLM-as-judge 作为独立版本化 Agent Run（Ticket 18）。

Judge Run 与 subject Run 完全隔离：使用专用 Eval RunStore（与 subject
RunStore 分开）、独立 binding 与执行预算（Judge Definition 自带
ModelBindingSet）、无业务 Tool、无 Session、无外部写入、无递归 Judge
（Definition 零工具 + 只消费最小授权 Projection，结构上不可能再调用
任何 Agent/Judge）。

Judge 结果 ``hard`` 恒为 False：deterministic hard/safety failure 永不
被 Judge 或任何质量分覆盖。Verdict 解析失败归一为 evaluator failure
（ERROR），绝不崩溃、绝不伪造结论。崩溃恢复与 subject 相同语义：
已完成 Judge Run 不重复 dispatch。

设计取舍（显式声明）：Judge 的 ERROR 结果同样 append-only 落盘
（包括解析失败、provider 抖动等瞬时性失败）——同一 execution 内
不提供重试通道；需要重跑 Judge 必须创建新 execution（新 Suite
版本或新 evaluator 版本）。这是 append-only 事实与可恢复性之间
的确定性取舍：宁可要求新 execution，也不允许改写已保存事实。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from ..._definition import DefinitionRegistry
from ..._errors import RunNotFoundError
from ..._runner import Runner
from ..._status import RunStatus
from ..._store import RunStore
from ._case import EvaluatorRef
from ._errors import EvalError, EvalFixtureBoundaryError
from ._evaluator import (
    EvalFailureKind,
    EvaluatorOutcome,
    EvaluatorResult,
    EvaluatorResultRecord,
)
from ._identity import canonical_json, digest_of
from ._projection import EvidenceRequirements, ObservationProjection

__all__ = [
    "JudgeBinding",
    "JudgeRunExecutor",
    "REASON_JUDGE_RUN_FAILED",
    "REASON_JUDGE_VERDICT_INVALID",
    "REASON_JUDGE_VERDICT_RENDERED",
    "parse_judge_verdict",
]

#: Judge 层稳定 reason code。
REASON_JUDGE_VERDICT_INVALID = "JUDGE_VERDICT_INVALID"
REASON_JUDGE_VERDICT_RENDERED = "JUDGE_VERDICT_RENDERED"
REASON_JUDGE_RUN_FAILED = "JUDGE_RUN_FAILED"


class JudgeBinding(BaseModel):
    """Case 冻结的 judge evaluator 与其独立 Judge Definition 的绑定。

    ``requirements`` 声明 Judge 可见的证据字段（配合 Projection
    policy 最小授权）；Judge Definition 携带独立 binding 与执行预算。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    judge: EvaluatorRef
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    requirements: EvidenceRequirements


def parse_judge_verdict(text: str, judge: EvaluatorRef) -> EvaluatorResult:
    """解析 Judge Run 输出的结构化 verdict。

    期望输出是 JSON 对象 ``{"verdict": "PASS"|"FAIL", "score":
    " number|null, "rationale": str}``；任何解析失败都归一为
    evaluator failure（ERROR / EVALUATOR / JUDGE_VERDICT_INVALID），
    绝不崩溃、绝不把非法输出当 PASS。
    """
    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise TypeError("judge verdict must be a JSON object")
        verdict = payload["verdict"]
        score = payload.get("score")
        rationale = payload.get("rationale", "")
        if verdict not in ("PASS", "FAIL"):
            raise ValueError(f"unknown verdict {verdict!r}")
        if score is not None and not isinstance(score, (int, float)):
            raise ValueError("score must be a number or null")
        if not isinstance(rationale, str):
            raise ValueError("rationale must be a string")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return EvaluatorResult(
            evaluator=judge,
            outcome=EvaluatorOutcome.ERROR,
            failure_kind=EvalFailureKind.EVALUATOR,
            reason_code=REASON_JUDGE_VERDICT_INVALID,
            detail=f"{type(exc).__name__}: {exc}",
            hard=False,
        )
    outcome = (
        EvaluatorOutcome.PASS
        if verdict == "PASS"
        else EvaluatorOutcome.FAIL
    )
    return EvaluatorResult(
        evaluator=judge,
        outcome=outcome,
        failure_kind=(
            EvalFailureKind.NONE
            if outcome is EvaluatorOutcome.PASS
            else EvalFailureKind.SUBJECT
        ),
        reason_code=REASON_JUDGE_VERDICT_RENDERED,
        detail=rationale,
        score=float(score) if score is not None else None,
        hard=False,
    )


class JudgeRunExecutor:
    """在专用 Eval RunStore 中执行独立版本化 Judge Agent Run。

    :param registry: Judge Definition 注册表（与 subject 注册表隔离，
        Judge Definition 不注册业务工具）。
    :param run_store: **专用** Judge RunStore；调用方负责与 subject
        Eval RunStore 分开（引擎层显式校验两者不是同一实例）。
    """

    def __init__(
        self,
        *,
        registry: DefinitionRegistry,
        run_store: RunStore,
    ) -> None:
        self._registry = registry
        self._run_store = run_store
        self._runner = Runner(registry=registry, store=run_store)

    @property
    def run_store(self) -> RunStore:
        """本执行器绑定的专用 Judge RunStore（引擎校验隔离用）。"""
        return self._run_store

    @staticmethod
    def judge_run_id(
        execution_id: str, item_id: str, judge: EvaluatorRef
    ) -> str:
        """由 execution + item + judge 身份派生的稳定 Judge Run identity。"""
        return "eval-judge-" + digest_of(
            "eval-judge-run", execution_id, item_id,
            judge.evaluator_id, judge.version,
        )[:32]

    @staticmethod
    def result_id(
        execution_id: str, item_id: str, judge: EvaluatorRef
    ) -> str:
        """Judge 结果记录的确定性身份。"""
        return digest_of(
            "eval-judge-result", execution_id, item_id,
            judge.evaluator_id, judge.version,
        )

    async def run_judge(
        self,
        *,
        binding: JudgeBinding,
        projection: ObservationProjection,
        execution_id: str,
        item_id: str,
        case_id: str = "",
        variant_id: str = "",
        repetition_index: int = 0,
    ) -> EvaluatorResultRecord:
        """执行（或恢复）一次独立 Judge Run 并归一化为结果记录。

        Judge 输入只由 Projection 的 delivered 字段构成（最小脱敏）；
        崩溃恢复与 subject 相同：已终态 Run 不重复 dispatch，非终态
        Run 续跑。结果 ``hard`` 恒为 False。
        """
        definition = self._registry.resolve(
            binding.definition_id, binding.definition_version
        )
        if definition.tools:
            raise EvalFixtureBoundaryError(
                "judge definitions must not declare any tools; a judge"
                " run with business tools could perform external writes"
                " or invoke another judge (recursion)"
            )
        self._enforce_deterministic(definition)
        run_id = self.judge_run_id(execution_id, item_id, binding.judge)
        judge_input = canonical_json(
            projection.model_dump(mode="json")
        )
        try:
            run = await self._runner.get_run(run_id)
        except RunNotFoundError:
            await self._runner.create_run(
                definition.definition_id,
                definition.version,
                judge_input,
                run_id=run_id,
            )
            run = await self._runner.start_run(run_id)
        else:
            if run.input != judge_input:
                # 投影输入由 projection policy / binding requirements
                # 决定：同 run 身份下输入变化意味着策略或绑定已变，
                # 绝不静默复用旧输入下的 verdict。
                raise EvalError(
                    f"judge run {run_id!r} already exists with a"
                    " different projection input; the projection"
                    " policy or judge binding changed under a frozen"
                    " judge run identity - use a new execution"
                )
            if not run.status.is_terminal:
                run = await self._runner.resume_run(run_id)
        if run.status is not RunStatus.SUCCEEDED or run.output is None:
            return self._failure_record(
                binding=binding,
                projection=projection,
                execution_id=execution_id,
                item_id=item_id,
                case_id=case_id,
                variant_id=variant_id,
                repetition_index=repetition_index,
                run_id=run_id,
                detail=f"judge run ended in {run.status.value}",
            )
        verdict = parse_judge_verdict(run.output, binding.judge)
        return EvaluatorResultRecord(
            result_id=self.result_id(execution_id, item_id, binding.judge),
            execution_id=execution_id,
            observation_id=projection.observation_id,
            item_id=item_id,
            case_id=case_id,
            variant_id=variant_id,
            repetition_index=repetition_index,
            evaluator=verdict.evaluator,
            outcome=verdict.outcome,
            failure_kind=verdict.failure_kind,
            reason_code=verdict.reason_code,
            detail=verdict.detail,
            score=verdict.score,
            hard=False,
            evidence_refs=(projection.observation_id,),
            judge_run_id=run_id,
        )

    def _failure_record(
        self,
        *,
        binding: JudgeBinding,
        projection: ObservationProjection,
        execution_id: str,
        item_id: str,
        case_id: str,
        variant_id: str,
        repetition_index: int,
        run_id: str,
        detail: str,
    ) -> EvaluatorResultRecord:
        """Judge Run 未成功终态时的 evaluator failure 记录。"""
        return EvaluatorResultRecord(
            result_id=self.result_id(execution_id, item_id, binding.judge),
            execution_id=execution_id,
            observation_id=projection.observation_id,
            item_id=item_id,
            case_id=case_id,
            variant_id=variant_id,
            repetition_index=repetition_index,
            evaluator=binding.judge,
            outcome=EvaluatorOutcome.ERROR,
            failure_kind=EvalFailureKind.EVALUATOR,
            reason_code=REASON_JUDGE_RUN_FAILED,
            detail=detail,
            hard=False,
            evidence_refs=(projection.observation_id,),
            judge_run_id=run_id,
        )

    def _enforce_deterministic(self, definition) -> None:  # noqa: ANN001
        """离线 Judge 的 fixture boundary：Adapter 必须确定性。"""
        adapters = [definition.model_adapter]
        adapters.extend(definition.model_adapters.values())
        for adapter in adapters:
            if getattr(adapter, "deterministic", False) is not True:
                raise EvalFixtureBoundaryError(
                    "judge runs require deterministic model adapters;"
                    f" adapter {type(adapter).__name__} has live"
                    " semantics and is outside the eval fixture boundary"
                )
