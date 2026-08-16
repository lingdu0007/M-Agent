"""Model Adapter 契约与确定性实现。

ADR 0030：Model Adapter 必须声明 streaming、tool calling、原生
structured output 与 usage reporting 等 Model Capabilities；
Definition 声明所需能力，注册时校验兼容性，禁止静默降级。

Live 与 deterministic（fake）Adapter 是明显不同的类型：
- :class:`ModelAdapter` 是 live provider 适配器的基类（deterministic=False）；
- :class:`DeterministicModelAdapter` 是测试与演示用的确定性 fake
  （deterministic=True），二者在任何场景都不会混淆。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
import enum
import hashlib
import json
from typing import Any, Self

from pydantic import BaseModel, Field, ValidationError, model_validator

from ._context import ContextItem
from ._errors import ModelContractViolationError
from ._tools import ToolCall, ToolOutcome, ToolSpec

class _CapabilityMode(str, enum.Enum):
    """Base type for explicit model capability modes."""


class StreamingMode(_CapabilityMode):
    NONE = "NONE"
    DELTA = "DELTA"


class ToolCallingMode(_CapabilityMode):
    NONE = "NONE"
    NATIVE = "NATIVE"


class StructuredOutputMode(_CapabilityMode):
    NONE = "NONE"
    JSON_OBJECT = "JSON_OBJECT"
    JSON_SCHEMA_STRICT = "JSON_SCHEMA_STRICT"


class UsageReportingMode(_CapabilityMode):
    NONE = "NONE"
    PROVIDER_REPORTED = "PROVIDER_REPORTED"


def _capability_mode_supports(
    field: str, available: _CapabilityMode, required: _CapabilityMode
) -> bool:
    """Return whether one typed capability mode satisfies another."""
    if required.value == "NONE":
        return True
    if field == "structured_output":
        return available is required or (
            available is StructuredOutputMode.JSON_SCHEMA_STRICT
            and required is StructuredOutputMode.JSON_OBJECT
        )
    return available is required


class RevisionStability(str, enum.Enum):
    PINNED = "PINNED"
    PROVIDER_ALIAS = "PROVIDER_ALIAS"


class ModelPurpose(str, enum.Enum):
    PRIMARY = "PRIMARY"
    CONTEXT_COMPRESSION = "CONTEXT_COMPRESSION"
    OUTPUT_REPAIR = "OUTPUT_REPAIR"


class UsageFieldGuarantee(str, enum.Enum):
    REQUIRED = "REQUIRED"
    OPTIONAL = "OPTIONAL"
    UNSUPPORTED = "UNSUPPORTED"


class UsageProvenance(str, enum.Enum):
    PROVIDER_REPORTED = "PROVIDER_REPORTED"
    RUNTIME_SIZED = "RUNTIME_SIZED"
    UNAVAILABLE = "UNAVAILABLE"


class ModelCapabilityCombination(BaseModel, frozen=True):
    """One explicitly supported concurrent protocol-mode combination."""

    streaming: StreamingMode = StreamingMode.NONE
    tool_calling: ToolCallingMode = ToolCallingMode.NONE
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    usage_reporting: UsageReportingMode = UsageReportingMode.NONE

    def supports(self, required: "ModelCapabilities") -> bool:
        """Whether this combination contains every requested non-NONE mode."""
        return all(
            _capability_mode_supports(
                field, getattr(self, field), getattr(required, field)
            )
            for field in (
                "streaming",
                "tool_calling",
                "structured_output",
                "usage_reporting",
            )
        )


class ModelRequirementReason(str, enum.Enum):
    SATISFIED = "SATISFIED"
    STREAMING_UNSUPPORTED = "STREAMING_UNSUPPORTED"
    TOOL_CALLING_UNSUPPORTED = "TOOL_CALLING_UNSUPPORTED"
    STRUCTURED_OUTPUT_UNSUPPORTED = "STRUCTURED_OUTPUT_UNSUPPORTED"
    USAGE_REPORTING_UNSUPPORTED = "USAGE_REPORTING_UNSUPPORTED"
    CAPABILITY_COMBINATION_UNSUPPORTED = "CAPABILITY_COMBINATION_UNSUPPORTED"
    CONTEXT_WINDOW_TOO_SMALL = "CONTEXT_WINDOW_TOO_SMALL"
    MAX_OUTPUT_TOO_SMALL = "MAX_OUTPUT_TOO_SMALL"


class ModelCapabilities(BaseModel, frozen=True):
    """Typed Model Contract capability modes.

    Stored contracts and Runner decisions always contain the explicit modes
    below. Boolean capability declarations are intentionally not accepted:
    the 0.3 contract is a one-time public API replacement.
    """

    streaming: StreamingMode = StreamingMode.NONE
    tool_calling: ToolCallingMode = ToolCallingMode.NONE
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    usage_reporting: UsageReportingMode = UsageReportingMode.NONE
    #: Concurrent protocol modes that this Contract expressly permits.  A
    #: multi-mode Contract without this list is invalid rather than implying
    #: that every Cartesian-product pairing is available.
    supported_combinations: tuple[ModelCapabilityCombination, ...] = Field(
        default_factory=tuple
    )

    @model_validator(mode="after")
    def _validate_supported_combinations(self) -> "ModelCapabilities":
        fields = (
            "streaming",
            "tool_calling",
            "structured_output",
            "usage_reporting",
        )
        active = [field for field in fields if getattr(self, field).value != "NONE"]
        if len(active) > 1 and not self.supported_combinations:
            raise ValueError(
                "multiple capability modes require supported_combinations"
            )
        seen: set[tuple[str, str, str, str]] = set()
        for combination in self.supported_combinations:
            key = tuple(getattr(combination, field).value for field in fields)
            if key in seen:
                raise ValueError("supported_combinations must be unique")
            seen.add(key)
            for field in fields:
                mode = getattr(combination, field)
                available = getattr(self, field)
                if not _capability_mode_supports(field, available, mode):
                    raise ValueError(
                        "supported combination declares a mode absent from "
                        f"capabilities: {field}={mode.value}"
                    )
        for field in active:
            if self.supported_combinations and not any(
                getattr(combination, field) == getattr(self, field)
                for combination in self.supported_combinations
            ):
                raise ValueError(
                    "supported_combinations must declare every enabled mode: "
                    f"{field}"
                )
        return self

    def _supports_combination(self, required: "ModelCapabilities") -> bool:
        requested = sum(
            getattr(required, field).value != "NONE"
            for field in (
                "streaming",
                "tool_calling",
                "structured_output",
                "usage_reporting",
            )
        )
        if requested <= 1:
            return True
        return any(
            combination.supports(required)
            for combination in self.supported_combinations
        )

    def supports(self, required: "ModelCapabilities") -> bool:
        """Return whether every requested non-NONE mode is available."""
        modes_match = all(
            _capability_mode_supports(
                field, getattr(self, field), getattr(required, field)
            )
            for field in (
                "streaming",
                "tool_calling",
                "structured_output",
                "usage_reporting",
            )
        )
        return modes_match and self._supports_combination(required)

    def merged_requirements(self, other: "ModelCapabilities") -> "ModelCapabilities":
        """Combine two minimum-capability declarations without weakening either."""
        values: dict[str, _CapabilityMode] = {}
        fields = (
            "streaming",
            "tool_calling",
            "structured_output",
            "usage_reporting",
        )
        for field in fields:
            left = getattr(self, field)
            right = getattr(other, field)
            if left.value == "NONE":
                values[field] = right
            elif right.value == "NONE":
                values[field] = left
            elif _capability_mode_supports(field, left, right):
                values[field] = left
            elif _capability_mode_supports(field, right, left):
                values[field] = right
            else:
                raise ValueError(f"conflicting requirements for {field}")
        active = sum(value.value != "NONE" for value in values.values())
        combinations = (
            (ModelCapabilityCombination(**values),) if active > 1 else ()
        )
        return ModelCapabilities(
            **values,
            supported_combinations=combinations,
        )


class ModelLimits(BaseModel, frozen=True):
    """Stable request-size limits declared by one Model Contract."""

    context_window_tokens: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)


class ModelUsageGuarantees(BaseModel, frozen=True):
    """Field-level Model Contract guarantees, never a single usage boolean."""

    input_tokens: UsageFieldGuarantee = UsageFieldGuarantee.OPTIONAL
    output_tokens: UsageFieldGuarantee = UsageFieldGuarantee.OPTIONAL
    cached_input_tokens: UsageFieldGuarantee = UsageFieldGuarantee.UNSUPPORTED
    reasoning_tokens: UsageFieldGuarantee = UsageFieldGuarantee.UNSUPPORTED


class ModelContract(BaseModel, frozen=True):
    """Versioned, non-secret model/deployment contract frozen into a Run."""

    contract_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    revision_stability: RevisionStability
    model_identity: str = Field(min_length=1)
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    limits: ModelLimits
    input_sizer_id: str = Field(min_length=1)
    serialization_id: str = Field(min_length=1)
    usage_guarantees: ModelUsageGuarantees = Field(
        default_factory=ModelUsageGuarantees
    )
    #: Canonical digest of this Contract's semantic fields. It excludes the
    #: deployment configuration digest so it identifies declared semantics.
    fingerprint: str | None = None
    #: Non-secret provider/deployment configuration identity. Live adapters
    #: compare it before dispatch; deterministic adapters have no such state.
    configuration_fingerprint: str | None = None

    @model_validator(mode="after")
    def _validate_usage_reporting(self) -> "ModelContract":
        if (
            self.capabilities.usage_reporting is UsageReportingMode.NONE
            and any(
                getattr(self.usage_guarantees, field)
                is UsageFieldGuarantee.REQUIRED
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "reasoning_tokens",
                )
            )
        ):
            raise ValueError(
                "usage_reporting=NONE cannot require usage fields"
            )
        expected_fingerprint = self.semantic_fingerprint()
        if self.fingerprint is None:
            object.__setattr__(self, "fingerprint", expected_fingerprint)
        elif self.fingerprint != expected_fingerprint:
            raise ValueError(
                "fingerprint must match the canonical ModelContract semantics"
            )
        return self

    def semantic_fingerprint(self) -> str:
        """Return the stable digest for this Contract's declared semantics."""
        encoded = json.dumps(
            self.model_dump(
                mode="json",
                exclude={"fingerprint", "configuration_fingerprint"},
            ),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Recompute the digest when a frozen Contract is deliberately revised."""
        semantic_fields = {
            "contract_id",
            "version",
            "revision_stability",
            "model_identity",
            "capabilities",
            "limits",
            "input_sizer_id",
            "serialization_id",
            "usage_guarantees",
        }
        if update and (semantic_fields.intersection(update) or "fingerprint" in update):
            values = self.model_dump()
            values.update(update)
            values["fingerprint"] = None
            return type(self).model_validate(values)
        return super().model_copy(update=update, deep=deep)


class ModelRequirementMatch(BaseModel, frozen=True):
    """Deterministic, inspectable result of matching requirements to a Contract."""

    compatible: bool
    reason: ModelRequirementReason


class ModelRequirements(BaseModel, frozen=True):
    """Minimum semantic and numeric requirements for one model binding."""

    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    min_context_window_tokens: int = Field(default=1, ge=1)
    min_output_tokens: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_capability_combination(self) -> "ModelRequirements":
        if not self.capabilities.supports(self.capabilities):
            raise ValueError(
                "multi-mode requirements must declare one supported combination"
            )
        return self

    def merged_with(self, other: "ModelRequirements") -> "ModelRequirements":
        """Return the stricter requirements required by both declarations."""
        return ModelRequirements(
            capabilities=self.capabilities.merged_requirements(other.capabilities),
            min_context_window_tokens=max(
                self.min_context_window_tokens, other.min_context_window_tokens
            ),
            min_output_tokens=max(
                self.min_output_tokens, other.min_output_tokens
            ),
        )

    def match(self, contract: ModelContract) -> ModelRequirementMatch:
        required = self.capabilities
        available = contract.capabilities
        for field, reason in (
            ("streaming", ModelRequirementReason.STREAMING_UNSUPPORTED),
            ("tool_calling", ModelRequirementReason.TOOL_CALLING_UNSUPPORTED),
            (
                "structured_output",
                ModelRequirementReason.STRUCTURED_OUTPUT_UNSUPPORTED,
            ),
            (
                "usage_reporting",
                ModelRequirementReason.USAGE_REPORTING_UNSUPPORTED,
            ),
        ):
            if (
                getattr(required, field).value != "NONE"
                and not _capability_mode_supports(
                    field, getattr(available, field), getattr(required, field)
                )
            ):
                return ModelRequirementMatch(compatible=False, reason=reason)
        if not available.supports(required):
            return ModelRequirementMatch(
                compatible=False,
                reason=ModelRequirementReason.CAPABILITY_COMBINATION_UNSUPPORTED,
            )
        if contract.limits.context_window_tokens < self.min_context_window_tokens:
            return ModelRequirementMatch(
                compatible=False,
                reason=ModelRequirementReason.CONTEXT_WINDOW_TOO_SMALL,
            )
        if contract.limits.max_output_tokens < self.min_output_tokens:
            return ModelRequirementMatch(
                compatible=False,
                reason=ModelRequirementReason.MAX_OUTPUT_TOO_SMALL,
            )
        return ModelRequirementMatch(
            compatible=True, reason=ModelRequirementReason.SATISFIED
        )


class ModelBinding(BaseModel, frozen=True):
    """One selected Contract and its requirements for a Model Step purpose."""

    purpose: ModelPurpose
    contract: ModelContract
    requirements: ModelRequirements = Field(default_factory=ModelRequirements)
    source_purpose: ModelPurpose | None = None

    @model_validator(mode="after")
    def _validate_source_purpose(self) -> "ModelBinding":
        if self.purpose is ModelPurpose.PRIMARY and self.source_purpose is not None:
            raise ValueError("PRIMARY binding cannot reuse another purpose")
        if (
            self.source_purpose is not None
            and self.source_purpose is not ModelPurpose.PRIMARY
        ):
            raise ValueError("only explicit PRIMARY reuse is supported")
        return self


class ModelBindingSet(BaseModel, frozen=True):
    """Frozen purpose-to-Contract selection with explicit primary reuse."""

    bindings: tuple[ModelBinding, ...]

    @model_validator(mode="after")
    def _validate_complete_bindings(self) -> "ModelBindingSet":
        purposes = [binding.purpose for binding in self.bindings]
        if set(purposes) != set(ModelPurpose) or len(purposes) != len(set(purposes)):
            raise ValueError(
                "model bindings must contain every ModelPurpose exactly once"
            )
        primary = next(
            binding
            for binding in self.bindings
            if binding.purpose is ModelPurpose.PRIMARY
        )
        for binding in self.bindings:
            if binding.source_purpose is ModelPurpose.PRIMARY and (
                binding.contract != primary.contract
                or binding.requirements != primary.requirements
            ):
                raise ValueError(
                    "PRIMARY reuse must preserve its Contract and Requirements"
                )
        return self

    def for_purpose(self, purpose: ModelPurpose) -> ModelBinding:
        for binding in self.bindings:
            if binding.purpose is purpose:
                return binding
        raise ValueError(f"no Model Binding for purpose {purpose.value}")

    def resolved(self) -> "ModelBindingSet":
        """Return the already-complete, explicitly selected binding set."""
        return self

    @classmethod
    def reuse_primary(
        cls,
        contract: ModelContract,
        requirements: ModelRequirements | None = None,
    ) -> "ModelBindingSet":
        """Explicitly select PRIMARY's Contract for every supported purpose."""
        primary = ModelBinding(
            purpose=ModelPurpose.PRIMARY,
            contract=contract,
            requirements=requirements or ModelRequirements(),
        )
        return cls(
            bindings=(
                primary,
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.CONTEXT_COMPRESSION,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
                primary.model_copy(
                    update={
                        "purpose": ModelPurpose.OUTPUT_REPAIR,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                ),
            )
        )


class ModelExecutionBudget(BaseModel, frozen=True):
    """Persisted upper bounds for all model dispatch attempts in one Run."""

    run_max_attempts: int = Field(default=8, ge=0)
    primary_max_attempts: int = Field(default=8, ge=0)
    context_compression_max_attempts: int = Field(default=8, ge=0)
    output_repair_max_attempts: int = Field(default=8, ge=0)

    def maximum_for(self, purpose: ModelPurpose) -> int:
        return {
            ModelPurpose.PRIMARY: self.primary_max_attempts,
            ModelPurpose.CONTEXT_COMPRESSION: self.context_compression_max_attempts,
            ModelPurpose.OUTPUT_REPAIR: self.output_repair_max_attempts,
        }[purpose]


class ModelUsage(BaseModel, frozen=True):
    """Normalized usage with provenance retained for every usage field."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    input_tokens_provenance: UsageProvenance | None = None
    output_tokens_provenance: UsageProvenance | None = None
    cached_input_tokens_provenance: UsageProvenance | None = None
    reasoning_tokens_provenance: UsageProvenance | None = None
    #: Unit as reported by the source system, e.g. ``tokens``.  Missing usage
    #: remains missing; Core never invents a unit.
    raw_unit: str | None = None
    #: Stable mapping/version that converted provider fields into this schema.
    normalization_source: str | None = None
    #: Compatibility summary only; field-level provenance is authoritative.
    provenance: UsageProvenance = UsageProvenance.PROVIDER_REPORTED

    @model_validator(mode="after")
    def _normalize_field_provenance(self) -> "ModelUsage":
        for field in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, field)
            source = getattr(self, f"{field}_provenance")
            if value is None and source not in (
                None,
                UsageProvenance.UNAVAILABLE,
            ):
                raise ValueError(
                    f"missing {field} must have unavailable provenance"
                )
            if value is not None and source is UsageProvenance.UNAVAILABLE:
                raise ValueError(
                    f"reported {field} cannot have unavailable provenance"
                )
            object.__setattr__(
                self,
                f"{field}_provenance",
                UsageProvenance.UNAVAILABLE
                if value is None
                else source or self.provenance
            )
        return self


class ModelRequest(BaseModel, frozen=True):
    """一次模型请求。

    - ``instructions``：来自 Definition 的 Agent Instruction（受信）；
    - ``context_items``：外部上下文，作为数据交付（ADR 0017）。
    - ``tools``：模型可调用的工具声明（含参数 Schema）；
    - ``tool_outcomes``：此前 Tool Step 的显式结果，作为外部数据
      交付（ADR 0017 / ADR 0024）。Tool Outcome 与 Context Item 一样
      永不写入或替换 ``instructions``，Model Adapter 必须保留这一
      边界。
    """

    input: str
    instructions: str
    context_items: tuple[ContextItem, ...] = Field(default_factory=tuple)
    tools: tuple[ToolSpec, ...] = Field(default_factory=tuple)
    tool_outcomes: tuple[ToolOutcome, ...] = Field(default_factory=tuple)
    #: Native structured-output mode selected by the frozen Model Binding.
    #: A configured provider schema is only sent when this request requires it.
    structured_output: StructuredOutputMode = StructuredOutputMode.NONE
    #: Usage reporting selected by the frozen Model Binding.
    usage_reporting: UsageReportingMode = UsageReportingMode.NONE


class ModelDelta(BaseModel, frozen=True):
    """一次模型流式输出增量（Ticket 08 / ADR 0011）。

    增量只作为带 ``attempt_id`` 的 Run Update 发布（:class:`RunUpdate`
    的 ``MODEL_DELTA``），**绝不写入 Run Store、不构成 checkpoint**；
    只有流结束产出的完整 :class:`ModelResponse` 才由 Runner checkpoint。
    失败重试时新的 Step Attempt 拥有新 ``attempt_id``，消费者据此替换
    废弃尝试的部分输出，而不是把不同尝试的文本拼接起来。
    """

    content: str


class ModelResponse(BaseModel, frozen=True):
    """一次模型请求的完整响应。

    - ``content``：文本内容；当响应只请求工具时可以为 None；
    - ``tool_calls``：模型请求的工具调用（0..N 个）；同一响应内的
      多个调用分别形成各自的 Tool Step，由 Runner 顺序执行
      （ADR 0004）。
    - ``usage``：用量（仅当 Adapter 声明 usage_reporting 时提供）。
    """

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = Field(default_factory=tuple)
    usage: ModelUsage | None = None
    #: Provider-reported model/revision observed for this specific dispatch.
    #: It is a Run fact, particularly relevant for PROVIDER_ALIAS Contracts.
    actual_revision: str | None = None


def normalize_model_response(
    contract: ModelContract,
    response: ModelResponse,
    *,
    request: ModelRequest,
    streaming: bool,
) -> ModelResponse:
    """Apply field guarantees without inventing missing provider usage."""
    if not isinstance(response, ModelResponse):
        raise ModelContractViolationError(
            "Model Adapter returned an unnormalizable response"
        )
    if (
        not isinstance(response.tool_calls, tuple)
        or not all(isinstance(call, ToolCall) for call in response.tool_calls)
        or (
            response.usage is not None
            and not isinstance(response.usage, ModelUsage)
        )
    ):
        raise ModelContractViolationError(
            "Model Adapter returned an unnormalizable response"
        )
    try:
        response = ModelResponse.model_validate(response.model_dump())
    except (TypeError, ValueError, ValidationError) as exc:
        raise ModelContractViolationError(
            "Model Adapter returned an unnormalizable response"
        ) from exc
    if (
        response.tool_calls
        and contract.capabilities.tool_calling is ToolCallingMode.NONE
    ):
        raise ModelContractViolationError(
            "Model Contract does not declare native tool calling"
        )
    values = {
        "streaming": StreamingMode.DELTA if streaming else StreamingMode.NONE,
        "tool_calling": (
            ToolCallingMode.NATIVE
            if response.tool_calls
            else ToolCallingMode.NONE
        ),
        "structured_output": request.structured_output,
        "usage_reporting": request.usage_reporting,
    }
    active = sum(mode.value != "NONE" for mode in values.values())
    response_modes = ModelCapabilities(
        **values,
        supported_combinations=(
            (ModelCapabilityCombination(**values),) if active > 1 else ()
        ),
    )
    if not contract.capabilities.supports(response_modes):
        raise ModelContractViolationError(
            "Model Contract does not declare the response capability combination"
        )
    if request.structured_output is StructuredOutputMode.JSON_SCHEMA_STRICT:
        try:
            structured = json.loads(response.content or "")
        except json.JSONDecodeError as exc:
            raise ModelContractViolationError(
                "strict structured response is not valid JSON"
            ) from exc
        if not isinstance(structured, dict):
            raise ModelContractViolationError(
                "strict structured response must be a JSON object"
            )
    usage = response.usage
    guarantees = contract.usage_guarantees
    fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
    )
    if usage is None:
        missing = [
            field
            for field in fields
            if getattr(guarantees, field) is UsageFieldGuarantee.REQUIRED
        ]
        if missing:
            raise ModelContractViolationError(
                "required usage fields missing: " + ", ".join(missing)
            )
        return response.model_copy(
            update={"usage": ModelUsage(provenance=UsageProvenance.UNAVAILABLE)}
        )
    provider_reported = any(
        getattr(usage, field) is not None
        and getattr(usage, f"{field}_provenance")
        is UsageProvenance.PROVIDER_REPORTED
        for field in fields
    )
    if (
        contract.capabilities.usage_reporting
        is UsageReportingMode.PROVIDER_REPORTED
        and provider_reported
        and (not usage.raw_unit or not usage.normalization_source)
    ):
        raise ModelContractViolationError(
            "provider-reported usage requires raw_unit and normalization_source"
        )
    for field in fields:
        guarantee = getattr(guarantees, field)
        value = getattr(usage, field)
        if guarantee is UsageFieldGuarantee.REQUIRED and value is None:
            raise ModelContractViolationError(f"required usage field missing: {field}")
        if guarantee is UsageFieldGuarantee.UNSUPPORTED and value is not None:
            raise ModelContractViolationError(
                f"unsupported usage field reported: {field}"
            )
    return response


def assert_model_request_compatible(
    contract: ModelContract,
    request: ModelRequest,
    *,
    streaming: bool = False,
) -> None:
    """Reject an undeclared request protocol combination before dispatch."""
    streaming_mode = StreamingMode.DELTA if streaming else StreamingMode.NONE
    tool_mode = (
        ToolCallingMode.NATIVE
        if request.tools or request.tool_outcomes
        else ToolCallingMode.NONE
    )
    values = {
        "streaming": streaming_mode,
        "tool_calling": tool_mode,
        "structured_output": request.structured_output,
        "usage_reporting": request.usage_reporting,
    }
    active = sum(mode.value != "NONE" for mode in values.values())
    required = ModelCapabilities(
        **values,
        supported_combinations=(
            (ModelCapabilityCombination(**values),) if active > 1 else ()
        ),
    )
    if not contract.capabilities.supports(required):
        raise ModelContractViolationError(
            "Model Contract does not declare the requested capability combination"
        )


class ModelAdapter(ABC):
    """Live provider Model Adapter 基类 / 扩展边界。

    `deterministic` 恒为 False：任何基于本类的实现都不是确定性 fake。
    """

    capabilities: ModelCapabilities

    #: 确定性 fake 的可见标记；live adapter 恒为 False。
    deterministic: bool = False

    @property
    def model_contract(self) -> ModelContract:
        """Return the explicitly declared Contract for this live instance."""
        raise ValueError(
            f"{type(self).__name__} must declare an instance ModelContract"
        )

    def definition_contract_fingerprint(self) -> str:
        """Return the current non-secret provider configuration identity.

        Live adapters must override this method with a stable, non-empty
        fingerprint. The empty default is reserved for deterministic test
        doubles, whose behavior is not a provider deployment contract.
        """
        return ""

    def validate_response(
        self, request: ModelRequest, response: ModelResponse
    ) -> ModelResponse:
        """Validate provider-specific response guarantees before normalization."""
        return response

    @abstractmethod
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """按统一模型契约生成一次完整响应。"""

    async def stream(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelDelta | ModelResponse]:
        """流式生成一次模型响应（Ticket 08 / ADR 0011）。

        只有声明 ``capabilities.streaming=DELTA`` 的 Adapter 才会被
        Runner 调用本方法（ADR 0030：不静默降级）。契约：

        - 逐个产出 :class:`ModelDelta`（文本增量）；
        - 流的**最后一个元素必须是完整的 :class:`ModelResponse`**
          （可携带 tool_calls / usage），表示本次调用成功结束；
        - 中途失败应抛出结构化 :class:`StepFailure`（如
          :class:`ModelFailure`），由 Runner 记录失败 Step Attempt 并按
          Retry Policy 决定是否以新 ``attempt_id`` 重试。

        未覆写（默认抛 NotImplementedError）意味着该 Adapter 不支持
        流式；Runner 只在 ``streaming=DELTA`` 时调用，未满足即显式失败。
        """
        raise NotImplementedError(
            f"{type(self).__name__} declares streaming=DELTA but does not "
            "implement stream()"
        )


class DeterministicModelAdapter(ModelAdapter):
    """确定性 fake Model Adapter，用于测试、演示与离线示例。

    与 live :class:`ModelAdapter` 类型和配置均明确区分：`deterministic`
    恒为 True，响应由构造参数决定，不访问任何网络或供应商。
    """

    deterministic: bool = True

    def __init__(
        self,
        responses: Sequence[str] = ("deterministic response",),
        capabilities: ModelCapabilities | None = None,
        model_contract: ModelContract | None = None,
    ) -> None:
        if not responses:
            raise ValueError("responses must contain at least one string")
        self._responses: tuple[str, ...] = tuple(responses)
        if model_contract is not None and (
            capabilities is not None
            and capabilities != model_contract.capabilities
        ):
            raise ValueError("capabilities must match model_contract")
        self._model_contract = model_contract
        self.capabilities: ModelCapabilities = (
            model_contract.capabilities
            if model_contract is not None
            else capabilities or ModelCapabilities()
        )
        self.call_count: int = 0
        self._last_request: ModelRequest | None = None

    @property
    def last_request(self) -> ModelRequest | None:
        """最近一次请求，供测试断言（如验证 create 之前无模型调用）。"""
        return self._last_request

    @property
    def model_contract(self) -> ModelContract:
        if self._model_contract is not None:
            return self._model_contract
        return ModelContract(
            contract_id="deterministic",
            version="1",
            revision_stability=RevisionStability.PINNED,
            model_identity="deterministic",
            capabilities=self.capabilities,
            limits=ModelLimits(
                context_window_tokens=1_000_000,
                max_output_tokens=1_000_000,
            ),
            input_sizer_id="deterministic-v1",
            serialization_id="deterministic-text-v1",
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self._last_request = request
        index = min(self.call_count - 1, len(self._responses) - 1)
        return ModelResponse(content=self._responses[index])


class DeterministicStreamingModelAdapter(DeterministicModelAdapter):
    """确定性流式 fake Model Adapter（Ticket 08 / ADR 0011）。

    与 live :class:`ModelAdapter` 和普通 :class:`DeterministicModelAdapter`
    明确区分：`deterministic` 恒为 True，`capabilities.streaming` 默认
    True，行为完全由构造参数决定，不访问任何网络或供应商。

    :meth:`stream` 按 ``chunks`` 逐个产出 :class:`ModelDelta`，最后产出
    携带 ``content="".join(chunks)`` 的完整 :class:`ModelResponse`；
    传入 ``tool_calls`` 时完整响应会请求这些工具（需要
    ``capabilities.tool_calling``，构造时自动补齐）。

    需要"部分输出后失败再成功"的测试通过子类覆写 :meth:`stream` 注入
    结构化失败（参考 tests/test_m_agent_stream_cancel.py）。
    """

    def __init__(
        self,
        chunks: Sequence[str] = ("Hel", "lo ", "world"),
        *,
        tool_calls: Sequence[ToolCall] = (),
        capabilities: ModelCapabilities | None = None,
    ) -> None:
        if not chunks:
            raise ValueError("chunks must contain at least one string")
        caps = capabilities or ModelCapabilities(streaming=StreamingMode.DELTA)
        if tool_calls and caps.tool_calling is ToolCallingMode.NONE:
            caps = ModelCapabilities(
                streaming=StreamingMode.DELTA,
                tool_calling=ToolCallingMode.NATIVE,
                structured_output=caps.structured_output,
                usage_reporting=caps.usage_reporting,
                supported_combinations=(
                    ModelCapabilityCombination(
                        streaming=StreamingMode.DELTA,
                        tool_calling=ToolCallingMode.NATIVE,
                        structured_output=caps.structured_output,
                        usage_reporting=caps.usage_reporting,
                    ),
                ),
            )
        super().__init__(responses=("",), capabilities=caps)
        self._chunks: tuple[str, ...] = tuple(chunks)
        self._tool_calls: tuple[ToolCall, ...] = tuple(tool_calls)
        self.requests: list[ModelRequest] = []

    async def stream(
        self, request: ModelRequest,
    ) -> AsyncIterator[ModelDelta | ModelResponse]:
        self.call_count += 1
        self._last_request = request
        self.requests.append(request)
        for chunk in self._chunks:
            yield ModelDelta(content=chunk)
        yield ModelResponse(
            content="".join(self._chunks),
            tool_calls=self._tool_calls,
        )


def serialize_model_response(response: ModelResponse) -> str:
    """把完整 ModelResponse 序列化为 Checkpoint 输出（含 tool_calls）。

    运行时内部使用（Runner 写入 Model Step Checkpoint）；恢复时经
    :func:`deserialize_model_response` 还原，以判断该响应是否请求了
    工具以及是否已产生最终内容（Ticket 05：checkpoint 必须携带完整
    响应，恢复才能精确重建执行位置）。
    """
    return response.model_dump_json()


def deserialize_model_response(payload: str) -> ModelResponse:
    """从 Model Step Checkpoint 输出还原完整 ModelResponse。"""
    return ModelResponse.model_validate_json(payload)
