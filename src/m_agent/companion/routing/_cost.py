"""声明式 Run Cost Policy 与证据性成本估算（ADR 0041，Ticket 20）。

估算依据版本化 Pricing Snapshot 与冻结 Contract Limits，显式声明
currency、估算 formula 与 usage provenance：

- ``WORST_CASE_TOKEN_BUDGET``：以（计量输入或上下文窗口回退）+
  预留输出 × 执行预算次数为最坏情况上界，声明为上界而非精确费用；
- ``REPORTED_USAGE``：基于实际 usage 计量的证据性估算，usage 缺失
  或 provenance 与声明不符时记录证据缺口。

价格证据缺失/过期/漂移/integrity failure、币种不一致或输出价格
缺失时只记录证据缺口，绝不伪造精确费用。估算在结构上不可能宣称
保证结算预算（``settlement_guaranteed`` 恒 False）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..._model import UsageProvenance
from ._catalog import AgentVariant
from ._evidence import (
    RoutingEvidence,
    SnapshotStatus,
    evaluate_pricing_snapshot,
)
from ._policy import canonical_digest


class CostFormula(str, enum.Enum):
    """成本估算公式的显式声明。"""

    WORST_CASE_TOKEN_BUDGET = "WORST_CASE_TOKEN_BUDGET"
    REPORTED_USAGE = "REPORTED_USAGE"


#: 估算证据缺口的稳定 code（绝不伪造精确费用）。
GAP_PRICING_MISSING = "PRICING_MISSING"
GAP_PRICING_STALE = "PRICING_STALE"
GAP_PRICING_FINGERPRINT_DRIFT = "PRICING_FINGERPRINT_DRIFT"
GAP_PRICING_INTEGRITY_FAILURE = "PRICING_INTEGRITY_FAILURE"
GAP_CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
GAP_OUTPUT_PRICE_MISSING = "OUTPUT_PRICE_MISSING"
GAP_INPUT_SIZE_UNAVAILABLE = "INPUT_SIZE_UNAVAILABLE"
GAP_USAGE_UNAVAILABLE = "USAGE_UNAVAILABLE"
GAP_USAGE_PROVENANCE_MISMATCH = "USAGE_PROVENANCE_MISMATCH"


class _FrozenCostValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunCostPolicy(_FrozenCostValue):
    """声明式成本策略：currency、formula 与 usage provenance。"""

    policy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    formula: CostFormula
    usage_provenance: UsageProvenance

    @model_validator(mode="after")
    def _usage_formula_requires_usage(self) -> "RunCostPolicy":
        if (
            self.formula is CostFormula.REPORTED_USAGE
            and self.usage_provenance is UsageProvenance.UNAVAILABLE
        ):
            raise ValueError(
                "REPORTED_USAGE formula cannot rely on UNAVAILABLE usage "
                "provenance"
            )
        return self

    def content_digest(self) -> str:
        """策略声明的规范化内容摘要。"""
        return canonical_digest(self.model_dump(mode="json"))


class UsageObservation(_FrozenCostValue):
    """一次实际 usage 计量观测及其 provenance。"""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    provenance: UsageProvenance


class RunCostEstimate(_FrozenCostValue):
    """证据性成本估算结果：上界/下界与证据缺口。

    ``worst_case_upper_bound`` 声明本读数是最坏情况上界语义；
    ``settlement_guaranteed`` 在结构上恒为 ``False``——估算永不
    宣称保证结算预算。
    """

    policy_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    currency: str
    worst_case_upper_bound: bool
    upper_bound: Decimal | None
    lower_bound: Decimal | None
    evidence_gaps: tuple[str, ...] = Field(default_factory=tuple)
    pricing_snapshot_version: str | None = None
    contract_fingerprint: str | None = None
    settlement_guaranteed: bool = False

    @model_validator(mode="after")
    def _settlement_never_guaranteed(self) -> "RunCostEstimate":
        if self.settlement_guaranteed:
            raise ValueError(
                "run cost estimates structurally never guarantee settlement"
            )
        return self

    def model_copy(self, **kwargs):  # type: ignore[override]
        """复制估算值：复制路径同样不允许宣称结算保证。"""
        update = kwargs.get("update") or {}
        if update.get("settlement_guaranteed"):
            raise ValueError(
                "run cost estimates structurally never guarantee settlement"
            )
        return super().model_copy(**kwargs)


def estimate_run_cost(
    *,
    policy: RunCostPolicy,
    variant: AgentVariant,
    evidence: RoutingEvidence,
    as_of: datetime,
    sized_input_tokens: int | None = None,
    usage: UsageObservation | None = None,
) -> RunCostEstimate:
    """依据版本化价格证据与冻结 Contract Limits 估算一次 Run 成本。

    - 价格证据异常（缺失/过期/漂移/integrity failure/币种不一致/
      输出价格缺失）只记录证据缺口，不伪造精确费用；
    - ``WORST_CASE_TOKEN_BUDGET``：计量输入（缺失时回退上下文窗口
      并记录 ``INPUT_SIZE_UNAVAILABLE``）+ 预留输出，乘以 Variant
      冻结的 ``run_max_attempts`` 执行预算；
    - ``REPORTED_USAGE``：基于 usage 计量的证据性读数；usage 缺失
      或 provenance 与声明不符时记录缺口。
    """
    pricing = evidence.latest_pricing(variant.variant_id, variant.version)
    gaps: list[str] = []
    pricing_version: str | None = None
    contract_fingerprint: str | None = None
    input_price: Decimal | None = None
    output_price: Decimal | None = None

    if pricing is None:
        gaps.append(GAP_PRICING_MISSING)
    else:
        status = evaluate_pricing_snapshot(
            pricing, expected_variant=variant, as_of=as_of
        )
        pricing_version = pricing.version
        contract_fingerprint = pricing.contract_fingerprint
        if (
            status is SnapshotStatus.STALE
            or status is SnapshotStatus.NOT_EFFECTIVE
        ):
            gaps.append(GAP_PRICING_STALE)
        elif status is SnapshotStatus.SUBJECT_MISMATCH:
            gaps.append(GAP_PRICING_MISSING)
        elif status is SnapshotStatus.FINGERPRINT_DRIFT:
            gaps.append(GAP_PRICING_FINGERPRINT_DRIFT)
        elif status is SnapshotStatus.INTEGRITY_FAILURE:
            gaps.append(GAP_PRICING_INTEGRITY_FAILURE)
        else:
            if pricing.currency != policy.currency:
                gaps.append(GAP_CURRENCY_MISMATCH)
            input_price = pricing.input_price_per_mtok
            output_price = pricing.output_price_per_mtok
            if output_price is None:
                gaps.append(GAP_OUTPUT_PRICE_MISSING)

    def finish(
        *,
        worst_case: bool,
        upper: Decimal | None,
        lower: Decimal | None,
    ) -> RunCostEstimate:
        return RunCostEstimate(
            policy_id=policy.policy_id,
            policy_version=policy.version,
            currency=policy.currency,
            worst_case_upper_bound=worst_case,
            upper_bound=upper,
            lower_bound=lower,
            evidence_gaps=tuple(gaps),
            pricing_snapshot_version=pricing_version,
            contract_fingerprint=contract_fingerprint,
        )

    if gaps or input_price is None or output_price is None:
        # 证据缺口存在时不伪造任何精确读数。
        return finish(worst_case=True, upper=None, lower=None)

    if policy.formula is CostFormula.REPORTED_USAGE:
        if usage is None:
            gaps.append(GAP_USAGE_UNAVAILABLE)
            return finish(worst_case=False, upper=None, lower=None)
        if usage.provenance is not policy.usage_provenance:
            gaps.append(GAP_USAGE_PROVENANCE_MISMATCH)
            return finish(worst_case=False, upper=None, lower=None)
        spend = (
            (Decimal(usage.input_tokens) / Decimal(1_000_000)) * input_price
            + (Decimal(usage.output_tokens) / Decimal(1_000_000))
            * output_price
        )
        return finish(worst_case=False, upper=spend, lower=spend)

    # WORST_CASE_TOKEN_BUDGET：计量输入缺失时回退冻结上下文窗口。
    contract = variant.primary_contract()
    context_window = contract.limits.context_window_tokens
    reserved_output = contract.limits.max_output_tokens
    input_tokens: int
    if sized_input_tokens is None:
        input_tokens = context_window
        gaps.append(GAP_INPUT_SIZE_UNAVAILABLE)
    else:
        input_tokens = sized_input_tokens
    attempts = variant.model_execution_budget.run_max_attempts
    per_attempt = (
        (Decimal(input_tokens + reserved_output) / Decimal(1_000_000))
        * input_price
        + (Decimal(reserved_output) / Decimal(1_000_000)) * output_price
    )
    return finish(
        worst_case=True, upper=per_attempt * Decimal(attempts), lower=None
    )
