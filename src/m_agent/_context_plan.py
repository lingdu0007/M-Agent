"""Context Plan、Stage、Scope、Frame、Budget 与 Provenance。

ADR 0040：Context Pipeline 以预算关口和显式步骤管理上下文变换。
Agent Definition 冻结线性 Context Plan，每个 Stage 具有稳定 identity、
显式输入/输出、scope、transform type 与版本化配置。Runner 按 Plan 推进
可恢复 Stage invocation，在 Model Step dispatch 前强制完整请求硬预算
检查，超限以 ``CONTEXT_BUDGET_EXCEEDED`` 确定性失败且零 model dispatch。

ADR 0016：Context Item 带稳定 item_id、content、source 与 metadata，
运行时保留顺序与溯源关系。
ADR 0017：外部内容始终作为数据交付，不提升为 Agent Instruction。
"""

from __future__ import annotations

import enum
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ._context import (
    ContextItem,
    deserialize_context_items,
)
from ._history import ConversationMessage
from ._model import (
    ModelContract,
    ModelLimits,
    ModelRequest,
    StructuredOutputMode,
    UsageReportingMode,
)
from ._tools import ToolOutcome, ToolSpec


#: 受保护通道名称集合——这些通道不可被普通 selection/trimming stage
#: 透明丢弃或改写（ADR 0040 AC 6）。任何 Stage 的 ``output_channels``
#: 都不得声明受保护通道：Stage 输出只能进入 ``context_items`` 分区，
#: 受保护通道始终来自权威记录（Snapshot / Run input / History /
#: Tool Step checkpoint）。
PROTECTED_CHANNELS: frozenset[str] = frozenset({
    "instructions",
    "run_input",
    "history",
    "tool_outcomes",
})

#: 旧行为兼容：Definition 声明了 Context Provider 但未声明显式
#: RUN_INPUT Scope Stage 时，Runner 合成的隐式 PROVIDE Stage identity。
LEGACY_PROVIDER_STAGE_ID = "context-provider"


class _FrozenPlanValue(BaseModel, frozen=True):
    """Frozen value type for Context Plan structures (extra='forbid')."""

    model_config = ConfigDict(extra="forbid")


class ContextScope(str, enum.Enum):
    """Context Pipeline 结果在 Agent Run 生命周期中的适用范围。

    - ``RUN_INPUT``：整次 Run 共享的稳定上下文，Run 开始时准备一次；
    - ``TOOL_OUTCOME``：随工具执行结果重新准备的动态上下文；
    - ``MODEL_STEP``：每次 Model Step 前重新准备的当前步骤上下文。

    恢复时必须复用已完成 checkpoint，不能重新读取外部事实（ADR 0040）。
    """

    RUN_INPUT = "RUN_INPUT"
    TOOL_OUTCOME = "TOOL_OUTCOME"
    MODEL_STEP = "MODEL_STEP"


class ContextTransformType(str, enum.Enum):
    """声明 Stage 的变换类型，Core 不内置实现算法。

    - ``PROVIDE``：从外部源获取 Context Item（如 Context Provider）；
    - ``SELECT``：从输入项中选择子集（筛选、排序、去重）；
    - ``TRIM``：对输入项做机械裁剪（不丢弃 protected 通道）；
    - ``BUDGET_SELECT``：依据预算选择最终项集。

    具体算法由 Companion / Provider 实现，Core 只执行 Stage 边界。
    """

    PROVIDE = "PROVIDE"
    SELECT = "SELECT"
    TRIM = "TRIM"
    BUDGET_SELECT = "BUDGET_SELECT"


class ContextStageIdentity(_FrozenPlanValue):
    """Context Stage 的稳定身份标识。

    ``stage_id`` 在同一 Plan 内唯一且有序，恢复时据此定位 checkpoint。
    ``transform_type`` 声明该 Stage 的变换类型，``version`` 标记配置版本。
    """

    stage_id: str = Field(min_length=1)
    scope: ContextScope
    transform_type: ContextTransformType
    config_version: str = Field(default="1", min_length=1)

    @property
    def stable_key(self) -> str:
        """Return the stable checkpoint key for this Stage identity."""
        return f"{self.stage_id}:{self.scope.value}:{self.transform_type.value}"


class ContextStageConfig(_FrozenPlanValue):
    """版本化的 Stage 配置（纯数据，可持久化）。

    具体字段由 Companion / Provider 约定；Core 只保留和透传。
    ``protected_channels`` 声明该 Stage 不可丢弃的输入通道。
    """

    stage_id: str = Field(min_length=1)
    transform_type: ContextTransformType
    config_version: str = Field(default="1", min_length=1)
    protected_channels: tuple[str, ...] = Field(default_factory=tuple)
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_stage_id_match(self) -> "ContextStageConfig":
        if not self.stage_id.strip():
            raise ValueError("stage_id must be non-empty")
        return self


class ContextStage(_FrozenPlanValue):
    """Context Plan 中具有稳定身份的最小上下文处理单元。

    每个 Stage 声明其 scope（触发时机）、transform type（变换类型）、
    版本化配置和显式输入/输出通道声明。Core 不实现具体算法。
    """

    identity: ContextStageIdentity
    config: ContextStageConfig | None = None
    #: 该 Stage 声明的输入通道（``instructions`` / ``run_input`` /
    #: ``history`` / ``context_items`` / ``tool_outcomes``）。
    input_channels: tuple[str, ...] = Field(default_factory=tuple)
    #: 该 Stage 声明的输出通道。
    output_channels: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _validate_config_stage_id(self) -> "ContextStage":
        if (
            self.config is not None
            and self.config.stage_id != self.identity.stage_id
        ):
            raise ValueError(
                f"Stage config stage_id '{self.config.stage_id}' does not "
                f"match identity stage_id '{self.identity.stage_id}'"
            )
        if (
            self.config is not None
            and self.config.transform_type != self.identity.transform_type
        ):
            raise ValueError(
                f"Stage config transform_type '{self.config.transform_type}' "
                f"does not match identity transform_type "
                f"'{self.identity.transform_type}'"
            )
        return self


class ContextPlan(_FrozenPlanValue):
    """Agent Definition 声明并冻结的有序 Context Stage 序列。

    Stage 顺序固定且不可变（ADR 0040）。恢复时 Runner 按 Plan 顺序
    检查每个 Scope 的已完成 checkpoint，跳过已完成的 Stage，不重新
    读取外部事实。

    空的 Stage 序列表示该 Run 不使用 Context Pipeline（兼容 0.3 基线）。
    """

    stages: tuple[ContextStage, ...] = Field(default_factory=tuple)
    plan_version: str = Field(default="1", min_length=1)

    @model_validator(mode="after")
    def _validate_stage_uniqueness(self) -> "ContextPlan":
        seen: set[str] = set()
        for stage in self.stages:
            sid = stage.identity.stage_id
            if sid in seen:
                raise ValueError(
                    f"duplicate Context Stage id: {sid}"
                )
            seen.add(sid)
        # ADR 0040 AC 6（冻结期强制点）：任何 Stage（含普通
        # selection/trimming stage）都不得声明受保护通道为输出——
        # 受保护通道不可被 Stage 透明丢弃或改写，只能来自权威记录。
        for stage in self.stages:
            declared = set(stage.output_channels) & PROTECTED_CHANNELS
            if declared:
                raise ValueError(
                    f"Context Stage '{stage.identity.stage_id}' declares "
                    f"protected output channel(s) "
                    f"{sorted(declared)}; protected channels cannot be "
                    "dropped or rewritten by a Context Stage"
                )
        return self

    def stages_for_scope(self, scope: ContextScope) -> tuple[ContextStage, ...]:
        """Return the ordered Stages that fire at a given lifecycle scope."""
        return tuple(
            stage for stage in self.stages
            if stage.identity.scope is scope
        )

    def is_empty(self) -> bool:
        """Return True when this Plan declares no Stages."""
        return len(self.stages) == 0


def legacy_provider_stage() -> ContextStage:
    """Return the implicit RUN_INPUT PROVIDE Stage for provider-only
    Definitions (0.3 兼容)：Definition 声明了 Context Provider 但
    Plan 未声明显式 RUN_INPUT Stage 时，Runner 按本 Stage 执行同一条
    Provider 路径，Checkpoint 格式与显式 Stage 完全一致。"""
    return ContextStage(
        identity=ContextStageIdentity(
            stage_id=LEGACY_PROVIDER_STAGE_ID,
            scope=ContextScope.RUN_INPUT,
            transform_type=ContextTransformType.PROVIDE,
        )
    )


def stage_invocation_step_id(
    stage: ContextStage, boundary: int
) -> str:
    """Return the deterministic Step id for one Stage invocation.

    同一 (stage identity, scope, boundary) 的 invocation 在崩溃重试/
    恢复时得到同一 step_id：已完成的 Checkpoint 据此被识别并复用，
    未完成的 invocation 以同一 identity 重新执行（at-least-once）。
    """
    return (
        f"ctx:{stage.identity.stage_id}:"
        f"{stage.identity.scope.value}:{boundary}"
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class ProvenanceSource(_FrozenPlanValue):
    """一条 Context Item 的来源/派生关系记录。

    - ``source_item_ids``：直接派生出本 Item 的输入 item_id 列表
      （``PROVIDE`` Stage 的原始 Item 没有派生来源，为空）；
    - ``stage_id``：产生本 Item 的 Stage identity；
    - ``transform_type``：产生本 Item 的变换类型。
    """

    stage_id: str = Field(min_length=1)
    transform_type: ContextTransformType
    source_item_ids: tuple[str, ...] = Field(default_factory=tuple)


class ContextItemWithProvenance(_FrozenPlanValue):
    """带溯源信息的 Context Item。

    Context Stage Result 中的每条输出都携带其直接派生来源，
    使 Checkpoint 保留完整的 provenance 链（ADR 0040 AC 3）。
    """

    item: ContextItem
    provenance: ProvenanceSource


# ---------------------------------------------------------------------------
# Context Stage Result & Checkpoint
# ---------------------------------------------------------------------------

class ContextStageResult(_FrozenPlanValue):
    """一次 Context Stage 执行的结构化输出（ADR 0040 / CONTEXT.md）。

    记录输入引用、输出 Context Items（带 provenance）、变换决策、
    计量与稳定变换类型，是该 Stage Checkpoint 的证据边界。
    """

    stage_id: str = Field(min_length=1)
    scope: ContextScope
    transform_type: ContextTransformType
    #: 输入引用：本次 Stage 消费的 item_id 列表（PROVIDE Stage 为空）。
    input_item_ids: tuple[str, ...] = Field(default_factory=tuple)
    #: 触发边界（ADR 0040）：RUN_INPUT 恒为 0（每次 Run 一次）；
    #: TOOL_OUTCOME 为触发时已完成的 Tool Outcome 数；MODEL_STEP
    #: 为触发时已完成的 Model checkpoint 数。同一 (stage, boundary)
    #: 的已完成 Checkpoint 在恢复时被原样复用，不重新读取外部事实。
    boundary: int = Field(default=0, ge=0)
    #: 输出 Items 及其 direct-derivation provenance。
    output_items: tuple[ContextItemWithProvenance, ...] = Field(
        default_factory=tuple
    )
    #: 变换决策摘要（纯数据，如选择的 item_id 列表、裁剪位置等）。
    decisions: dict[str, Any] = Field(default_factory=dict)
    #: 计量：本次 Stage 的输入/输出 token 估算（由 Sizer 提供）。
    measurement: dict[str, int] = Field(default_factory=dict)

    @property
    def context_items(self) -> tuple[ContextItem, ...]:
        """Return the bare Context Items (without provenance)."""
        return tuple(ip.item for ip in self.output_items)

    def serialize(self) -> str:
        """Serialize to JSON for Checkpoint persistence."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
        )

    @classmethod
    def deserialize(cls, payload: str) -> "ContextStageResult":
        """Reconstruct from Checkpoint payload."""
        return cls.model_validate_json(payload)


def parse_stage_result(payload: str) -> ContextStageResult | None:
    """Parse a Checkpoint payload into a Stage Result envelope.

    返回 None 表示载荷不是 Stage Result envelope（如 0.3 时代的裸
    Context Item 列表），调用方按 legacy 语义处理。
    """
    try:
        return ContextStageResult.deserialize(payload)
    except ValidationError:
        return None


def context_items_from_payload(payload: str) -> tuple[ContextItem, ...]:
    """Extract the bare Context Items from a Checkpoint payload.

    同时接受 Stage Result envelope（T14 起）与裸 Item 列表
    （0.3 legacy），恢复路径因此对两种历史载荷都能重建 Items。
    """
    parsed = parse_stage_result(payload)
    if parsed is not None:
        return parsed.context_items
    return tuple(deserialize_context_items(payload))


def aggregate_frame_items(
    payloads: Sequence[str],
    *,
    tool_boundary: int,
    model_boundary: int,
) -> tuple[ContextItem, ...]:
    """Aggregate Context Step Checkpoint payloads into one Frame item set.

    按 CONTEXT.md 的 Frame 语义聚合（ADR 0040）：

    - ``RUN_INPUT`` Stage 的 Items 是整次 Run 的基础项，始终包含；
    - ``TOOL_OUTCOME`` Stage 的 Items 是随 Tool Step 累积的增量项，
      触发边界 <= 当前 ``tool_boundary`` 的全部包含；
    - ``MODEL_STEP`` Stage 的 Items 只属于当前 Model Step，仅包含
      触发边界 == 当前 ``model_boundary`` 的结果；
    - legacy 裸 Item 列表载荷视为 RUN_INPUT 基础项。
    """
    items: list[ContextItem] = []
    for payload in payloads:
        parsed = parse_stage_result(payload)
        if parsed is None:
            items.extend(deserialize_context_items(payload))
        elif parsed.scope is ContextScope.RUN_INPUT:
            items.extend(parsed.context_items)
        elif (
            parsed.scope is ContextScope.TOOL_OUTCOME
            and parsed.boundary <= tool_boundary
        ):
            items.extend(parsed.context_items)
        elif (
            parsed.scope is ContextScope.MODEL_STEP
            and parsed.boundary == model_boundary
        ):
            items.extend(parsed.context_items)
    return tuple(items)


# ---------------------------------------------------------------------------
# Context Frame
# ---------------------------------------------------------------------------

class ContextFrame(_FrozenPlanValue):
    """一次 Model Step 使用的分区化上下文视图（ADR 0040 / CONTEXT.md）。

    明确隔离以下分区，Tool Outcome 不伪装成 Context Item：

    - ``instructions``：来自 Definition 的 Agent Instruction（受信）；
    - ``run_input``：Run 创建时冻结的输入；
    - ``history``：冻结的 Conversation History；
    - ``context_items``：Context Stage 产出的外部数据项；
    - ``tool_outcomes``：此前 Tool Step 的显式结果（外部数据）；
    - ``tools``：模型可调用的工具声明（含参数 Schema）；
    - ``structured_output`` / ``usage_reporting``：冻结的 Model Binding 选项。

    只有通过完整预算检查的 Frame 才能被 checkpoint 并 dispatch。
    """

    instructions: str
    run_input: str
    history: tuple[ConversationMessage, ...] = Field(default_factory=tuple)
    context_items: tuple[ContextItem, ...] = Field(default_factory=tuple)
    tool_outcomes: tuple[ToolOutcome, ...] = Field(default_factory=tuple)
    tools: tuple[ToolSpec, ...] = Field(default_factory=tuple)
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    usage_reporting: UsageReportingMode = UsageReportingMode.NONE

    def to_model_request(self, input_override: str | None = None) -> ModelRequest:
        """Convert this Frame into a ModelRequest for dispatch."""
        return ModelRequest(
            input=input_override if input_override is not None else self.run_input,
            instructions=self.instructions,
            history=self.history,
            context_items=self.context_items,
            tools=self.tools,
            tool_outcomes=self.tool_outcomes,
            structured_output=self.structured_output,
            usage_reporting=self.usage_reporting,
        )


# ---------------------------------------------------------------------------
# Context Budget & Sizer
# ---------------------------------------------------------------------------

class ContextBudget(_FrozenPlanValue):
    """一次 Model Step 完整模型请求的硬预算上限（ADR 0040）。

    覆盖所有输入 channel、协议/serialization 开销、Tool schema、
    Output Contract 与 reserved output。超限必须以
    ``CONTEXT_BUDGET_EXCEEDED`` 失败，不隐式裁剪或压缩。

    - ``total_token_limit``：包含 reserved output 的完整请求硬上限；
    - ``reserved_output_tokens``：为模型输出预留的 token 数。
    """

    total_token_limit: int = Field(ge=1)
    reserved_output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_reserved_not_exceeds_total(self) -> "ContextBudget":
        if self.reserved_output_tokens >= self.total_token_limit:
            raise ValueError(
                "reserved_output_tokens must be less than total_token_limit"
            )
        return self

    @property
    def input_token_limit(self) -> int:
        """The maximum input tokens allowed within the total budget."""
        return self.total_token_limit - self.reserved_output_tokens

    @classmethod
    def from_model_limits(
        cls, limits: ModelLimits, *, reserved_output_tokens: int | None = None
    ) -> "ContextBudget":
        """Create a Budget from frozen Model Limits.

        By default reserves the declared max_output_tokens, capped at
        half the context window to avoid degenerate budgets where the
        output limit is very large relative to the input window.
        """
        if reserved_output_tokens is not None:
            reserved = reserved_output_tokens
        else:
            reserved = min(
                limits.max_output_tokens,
                limits.context_window_tokens // 2,
            )
        return cls(
            total_token_limit=limits.context_window_tokens,
            reserved_output_tokens=min(reserved, limits.context_window_tokens - 1),
        )


class SizingMode(str, enum.Enum):
    """Model Input Sizer 的计量模式。

    - ``EXACT``：精确计量，与 Model Adapter wire contract 完全一致；
    - ``CONSERVATIVE``：保证不低于真实计量的保守估算。
    """

    EXACT = "EXACT"
    CONSERVATIVE = "CONSERVATIVE"


class FrameSizingResult(_FrozenPlanValue):
    """一次 Frame 计量的结果。

    ``input_tokens`` 是所有输入 channel + 协议开销的总估算；
    ``channel_breakdown`` 按通道记录分量，供 evidence 和 debug。
    """

    input_tokens: int = Field(ge=0)
    reserved_output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    channel_breakdown: dict[str, int] = Field(default_factory=dict)
    sizing_mode: SizingMode = SizingMode.CONSERVATIVE

    @property
    def within_budget(self) -> bool:
        """True when total_tokens fits within the Budget."""
        return self.total_tokens <= (
            self.input_tokens + self.reserved_output_tokens
        )  # total_tokens is pre-computed; caller checks against Budget


class ModelInputSizer:
    """Model Input Sizer 扩展边界（ADR 0040 / CONTEXT.md）。

    按照冻结的 Model Contract serialization 行为，对完整 Model Request
    及其组成部分给出精确或保证不低估的计量。Core 不内置供应商
    tokenizer，而是通过此抽象边界委托给 Adapter 实现。
    """

    def __init__(
        self,
        *,
        sizer_id: str,
        mode: SizingMode = SizingMode.CONSERVATIVE,
    ) -> None:
        if not sizer_id.strip():
            raise ValueError("sizer_id must be non-empty")
        self._sizer_id = sizer_id
        self._mode = mode

    @property
    def sizer_id(self) -> str:
        return self._sizer_id

    @property
    def mode(self) -> SizingMode:
        return self._mode

    def size_frame(self, frame: ContextFrame) -> FrameSizingResult:
        """Size a complete Frame for budget checking.

        Default implementation uses conservative character-based estimation
        (4 chars ≈ 1 token). Official adapters override with precise
        tokenizers matching their wire contract.
        """
        breakdown: dict[str, int] = {}

        instructions_tokens = self.estimate_text(frame.instructions)
        breakdown["instructions"] = instructions_tokens

        run_input_tokens = self.estimate_text(frame.run_input)
        breakdown["run_input"] = run_input_tokens

        history_tokens = sum(
            self.estimate_text(msg.content) for msg in frame.history
        )
        breakdown["history"] = history_tokens

        context_tokens = sum(
            self.estimate_text(item.content) for item in frame.context_items
        )
        breakdown["context_items"] = context_tokens

        tool_outcome_tokens = sum(
            self.estimate_text(outcome.result) for outcome in frame.tool_outcomes
        )
        breakdown["tool_outcomes"] = tool_outcome_tokens

        tool_schema_tokens = sum(
            self.estimate_text(json.dumps(tool.model_dump(mode="json")))
            for tool in frame.tools
        )
        breakdown["tools"] = tool_schema_tokens

        # Protocol overhead: conservative per-item and per-message overhead.
        protocol_overhead = (
            len(frame.context_items) * 4
            + len(frame.history) * 4
            + len(frame.tool_outcomes) * 4
            + len(frame.tools) * 4
            + 10  # base request structure
        )
        breakdown["protocol_overhead"] = protocol_overhead

        input_tokens = sum(breakdown.values())
        total_tokens = input_tokens  # reserved output added by caller

        return FrameSizingResult(
            input_tokens=input_tokens,
            reserved_output_tokens=0,
            total_tokens=total_tokens,
            channel_breakdown=breakdown,
            sizing_mode=self._mode,
        )

    @staticmethod
    def estimate_text(text: str) -> int:
        """Conservative token estimate: 4 chars per token, minimum 1."""
        if not text:
            return 0
        return max(1, len(text) // 4)

    @classmethod
    def for_contract(
        cls, contract: ModelContract
    ) -> "ModelInputSizer":
        """Create a Sizer matching a frozen Model Contract's sizer identity."""
        mode = SizingMode.EXACT
        # Deterministic and legacy contracts use conservative estimation.
        if contract.input_sizer_id.startswith(("legacy-", "deterministic-")):
            mode = SizingMode.CONSERVATIVE
        return cls(sizer_id=contract.input_sizer_id, mode=mode)


def check_frame_budget(
    frame: ContextFrame,
    budget: ContextBudget,
    sizer: ModelInputSizer,
) -> FrameSizingResult:
    """Check a complete Frame against the hard Budget.

    Returns a FrameSizingResult with ``within_budget`` indicating pass/fail.
    The caller must verify ``result.total_tokens <= budget.total_token_limit``
    before allowing dispatch; if it exceeds, the Runner must fail with
    ``CONTEXT_BUDGET_EXCEEDED`` and zero model dispatch (ADR 0040 AC 7).
    """
    result = sizer.size_frame(frame)
    # Add reserved output tokens to the total.
    total = result.input_tokens + budget.reserved_output_tokens
    return result.model_copy(
        update={
            "reserved_output_tokens": budget.reserved_output_tokens,
            "total_tokens": total,
        }
    )
