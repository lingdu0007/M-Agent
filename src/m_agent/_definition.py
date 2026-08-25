"""Agent Definition、Definition Snapshot 与 Definition Registry。

ADR 0022：Agent Definition 不可变且显式版本化，具有稳定
`definition_id` 和不可变 `version`。
ADR 0023：Run Store 只保存 Snapshot 与能力标识，从不序列化 Python
callable；Registry 按 `definition_id + version` 精确提供可执行实现。
ADR 0030：注册时校验 required Model Capabilities 被 Adapter 声明。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ._context import ContextProvider
from ._compression import CompressionContract
from ._context_plan import ContextPlan
from ._errors import (
    DefinitionConflictError,
    DefinitionNotFoundError,
    ModelCapabilityError,
)
from ._model import (
    ModelAdapter,
    ModelBindingSet,
    ModelCapabilities,
    ModelContract,
    ModelExecutionBudget,
    ModelLimits,
    ModelPurpose,
    ModelRequirements,
    RevisionStability,
    ToolCallingMode,
)
from ._output import OutputContract
from ._policy import AllowAllRunPolicy, PolicyIdentity, RunPolicy
from ._tools import Tool, ToolDeclaration, ToolEffect


class RetryPolicy(BaseModel, frozen=True):
    """Definition Snapshot 中针对 Run Step 声明的有界重试规则（ADR 0025）。

    - ``max_attempts``：单次推进路径内 Step 的**总尝试次数上限**
      （含首次执行），必须 >= 1；到达上限后 Runner 停止自动重试并
      产生确定性终态或 WAITING。这是重试的显式、有限上界，绝不
      无限重试。
    - ``delay``：两次尝试之间的固定等待（确定性退避，非随机）。
      默认 0 表示立即重试；测试与示例应使用默认值以保持确定性。
      若配置非零 ``delay``，其值应小于 Run Lease TTL
      （:data:`DEFAULT_LEASE_TTL`），否则等待期间租约可能过期，
      后续尝试写入会被 RunStore 以 :class:`LeaseNotHeldError` 拒绝。

    未配置 Retry Policy 时 Runner 不做任何自动重试（fail-closed）。
    策略在 Run 启动时冻结进 Definition Snapshot，运行中修改 Agent
    Definition 不能改变已有 Run 的重试行为（Ticket 06 AC 8）。
    """

    max_attempts: int = Field(ge=1)
    delay: timedelta = Field(default_factory=lambda: timedelta(seconds=0))


class DefinitionSnapshot(BaseModel, frozen=True):
    """Agent Run 首次开始时冻结的不可变定义视图（纯数据，可持久化）。"""

    definition_id: str
    version: str
    instructions: str
    #: 完整、已解析的用途绑定；PRIMARY reuse 也以明确的 source_purpose
    #: 持久化，恢复无需重新选择模型。
    model_bindings: ModelBindingSet | None = None
    #: Temporary 0.2 snapshot fields. A snapshot without bindings remains a
    #: legacy snapshot and is verified using exactly its persisted evidence.
    required_capabilities: ModelCapabilities | None = None
    adapter_capabilities: ModelCapabilities | None = None
    adapter_contract_fingerprint: str = ""
    #: Run 级与用途级模型 dispatch 尝试硬上限。None 保留 0.2 快照未声明
    #: 该新契约时的不设限语义；新 typed helper 总会写入显式预算。
    model_execution_budget: ModelExecutionBudget | None = None
    #: 能力标识：该 Run 声明了 Context Provider（ADR 0014）。只记录
    #: 是否声明，不序列化 provider 本身（ADR 0023：不持久化 callable）。
    has_context_provider: bool = False
    #: 冻结的工具能力声明（ADR 0022/0023）：只记录 name 与 effect，
    #: 不序列化工具实现。恢复行为由启动时冻结的声明决定；后续重试 /
    #: WAITING 决策依赖 Tool Effect（ADR 0007）。
    tool_declarations: tuple[ToolDeclaration, ...] = Field(default_factory=tuple)
    #: 冻结的 Retry Policy（ADR 0025 / Ticket 06）：Run 的重试决策只
    #: 依据本快照中的策略，运行中修改 Agent Definition 不影响已有 Run
    #: （ADR 0022/0023）。None 表示该 Run 不自动重试。
    retry_policy: RetryPolicy | None = None
    #: Deterministic policy identity and the versioned final output semantics
    #: are persisted with the Run; recovery never consults a later Definition.
    policy_identity: PolicyIdentity = Field(
        default_factory=lambda: AllowAllRunPolicy().identity
    )
    output_contract: OutputContract | None = None
    #: 冻结的 Context Plan（ADR 0040 / Ticket 14）：有序 Stage 序列及其
    #: 作用域。空 Plan 表示不使用 Context Pipeline。恢复时 Runner 按
    #: Plan 顺序复用已完成 checkpoint，不重新读取外部事实。
    context_plan: ContextPlan = Field(default_factory=ContextPlan)
    #: 冻结的 Compression Contract（ADR 0040 / Ticket 15）：显式、有损的
    #: Semantic Compression 声明。None 表示该 Run 不执行压缩；非空时
    #: Runner 在 RUN_INPUT Stage 之后、业务 Model Step 之前执行一次
    #: ``purpose=CONTEXT_COMPRESSION`` 的独立 Model Step。
    compression_contract: CompressionContract | None = None

    @model_validator(mode="after")
    def _validate_model_snapshot_shape(self) -> "DefinitionSnapshot":
        if self.model_bindings is None and (
            self.required_capabilities is None
            or self.adapter_capabilities is None
        ):
            raise ValueError(
                "legacy DefinitionSnapshot requires persisted Model Capabilities"
            )
        return self


class AgentDefinition(BaseModel, frozen=True):
    """不可变、带版本的 Agent 行为与能力声明。

    `model_adapter` 是运行时扩展对象，从不随 Definition 序列化
    （ADR 0023：Python callable 不进入 Run Store）。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    definition_id: str
    version: str
    instructions: str
    model_requirements: ModelRequirements = Field(default_factory=ModelRequirements)
    #: Temporary 0.2 alias. Ticket 11 removes this at the explicit 0.3
    #: contract transition; typed requirements remain the internal source.
    required_capabilities: ModelCapabilities = Field(
        default_factory=ModelCapabilities
    )
    #: Each purpose is persisted explicitly. During the 0.2 expand window an
    #: omitted value is normalized to PRIMARY reuse; new callers can make that
    #: choice visible with :meth:`for_adapter`.
    model_bindings: ModelBindingSet
    #: Direct 0.2 construction without this field remains unbounded during
    #: the documented expand window. New typed construction uses for_adapter,
    #: which freezes the default budget explicitly.
    model_execution_budget: ModelExecutionBudget | None = None
    model_adapter: ModelAdapter = Field(exclude=True)
    #: Direct non-primary bindings must retain the Adapter instance that owns
    #: their selected Contract. PRIMARY reuse deliberately resolves through
    #: ``model_adapter`` and does not need a duplicate entry here.
    model_adapters: Mapping[ModelPurpose, ModelAdapter] = Field(
        default_factory=dict, exclude=True
    )
    #: 应用选择的、在依赖的 Model Step 之前确定性执行的 Context
    #: Provider（ADR 0014）。None 表示该 Run 不注入外部上下文。
    #: 与 model_adapter 一样从不随 Definition 序列化（ADR 0023）。
    context_provider: ContextProvider | None = Field(default=None, exclude=True)
    #: 模型可调用的工具（ADR 0004 / Ticket 05）。每个工具声明
    #: Tool Effect（ADR 0007，未声明默认 NON_IDEMPOTENT）；工具实现
    #: 与 model_adapter / context_provider 一样从不随 Definition
    #: 序列化（ADR 0023：不持久化 callable），快照只记录能力标识。
    tools: tuple[Tool, ...] = Field(default_factory=tuple, exclude=True)
    #: Run Step 的有界重试规则（ADR 0025 / Ticket 06）。None 表示不
    #: 自动重试。与 model_adapter 不同，Retry Policy 是纯数据声明，
    #: 随 Snapshot 一起冻结、持久化（不 exclude），恢复决策只读快照。
    retry_policy: RetryPolicy | None = None
    #: Deterministic executable policy is an application-supplied port, never
    #: serialized. Its immutable identity is persisted in the Snapshot.
    run_policy: RunPolicy = Field(default_factory=AllowAllRunPolicy, exclude=True)
    #: Versioned final-output semantics, frozen into every created Run.
    output_contract: OutputContract | None = None
    #: Agent Definition 声明并冻结的有序 Context Plan（ADR 0040 /
    #: Ticket 14）。None 或空 Plan 表示不使用 Context Pipeline；
    #: 非空 Plan 不随 Snapshot 序列化 Stage 实现对象（ADR 0023：
    #: 不持久化 callable），只冻结纯数据 Plan。
    context_plan: ContextPlan = Field(default_factory=ContextPlan)
    #: 版本化的 Compression Contract（ADR 0040 / Ticket 15）：纯数据，
    #: 随 Snapshot 冻结。恢复时按 (contract_id, version) 精确复用已
    #: 完成 compression checkpoint，不用最新契约重算旧 Run。
    compression_contract: CompressionContract | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_model_definition(cls, value: object) -> object:
        """Accept 0.2 construction while retaining typed internal bindings."""
        if not isinstance(value, Mapping):
            return value
        values = dict(value)
        required_capabilities = ModelCapabilities.model_validate(
            values.get("required_capabilities", ModelCapabilities())
        )
        requirements = ModelRequirements.model_validate(
            values.get("model_requirements", ModelRequirements())
        ).merged_with(
            ModelRequirements(capabilities=required_capabilities)
        )
        values["required_capabilities"] = required_capabilities
        values["model_requirements"] = requirements
        uses_legacy_primary_reuse = values.get("model_bindings") is None
        if uses_legacy_primary_reuse:
            adapter = values.get("model_adapter")
            if isinstance(adapter, ModelAdapter):
                values["model_bindings"] = ModelBindingSet.reuse_primary(
                    cls._model_contract_for(adapter), requirements
                )
        elif values.get("model_execution_budget") is None:
            # Supplying the complete Binding Set is the new typed construction
            # path. It must freeze a hard budget even while the omitted-binding
            # 0.2 construction path remains compatible and unbounded.
            values["model_execution_budget"] = ModelExecutionBudget()
        return values

    @classmethod
    def for_adapter(cls, **values: Any) -> "AgentDefinition":
        """Build a Definition with an explicit complete PRIMARY-reuse set.

        This compact construction helper makes PRIMARY reuse visible at the
        call site while preserving a complete frozen snapshot. Explicit
        ``model_bindings`` always take precedence.
        """
        if values.get("model_bindings") is None:
            adapter = values.get("model_adapter")
            if not isinstance(adapter, ModelAdapter):
                raise ValueError("for_adapter requires a ModelAdapter")
            requirements = ModelRequirements.model_validate(
                values.get("model_requirements", ModelRequirements())
            )
            values["model_bindings"] = ModelBindingSet.reuse_primary(
                adapter.model_contract, requirements
            )
        if "model_execution_budget" not in values:
            values["model_execution_budget"] = ModelExecutionBudget()
        return cls(**values)

    def effective_model_requirements(self) -> ModelRequirements:
        """Return the complete typed requirements frozen for this Run."""
        if not self.tools:
            return self.model_requirements
        return self.model_requirements.merged_with(
            ModelRequirements(
                capabilities=ModelCapabilities(
                    tool_calling=ToolCallingMode.NATIVE
                )
            )
        )

    @staticmethod
    def _model_contract_for(adapter: ModelAdapter) -> ModelContract:
        """Resolve a typed Contract or synthesize the 0.2 direct-adapter view."""
        try:
            return adapter.model_contract
        except ValueError:
            if (
                getattr(type(adapter), "model_contract", None)
                is not ModelAdapter.model_contract
            ):
                raise
        identity = f"{type(adapter).__module__}.{type(adapter).__qualname__}"
        contract_identity = hashlib.sha256(identity.encode()).hexdigest()[:16]
        configuration_fingerprint = adapter.definition_contract_fingerprint()
        return ModelContract(
            contract_id=f"legacy-{contract_identity}",
            version="0.2",
            revision_stability=RevisionStability.PROVIDER_ALIAS,
            model_identity=f"legacy:{identity}",
            capabilities=adapter.capabilities,
            limits=ModelLimits(
                context_window_tokens=1_000_000,
                max_output_tokens=1_000_000,
            ),
            input_sizer_id="legacy-0.2",
            serialization_id="legacy-0.2",
            configuration_fingerprint=configuration_fingerprint or None,
        )

    @staticmethod
    def _adapter_configuration_fingerprint(
        adapter: ModelAdapter, contract: ModelContract
    ) -> str:
        if AgentDefinition._model_contract_for(adapter) != contract:
            raise ModelCapabilityError(
                "Model Binding Adapter owner does not match its Model Contract"
            )
        if not adapter.deterministic and not contract.configuration_fingerprint:
            raise ValueError(
                f"live adapter {type(adapter).__name__} must declare a non-empty "
                "definition contract configuration fingerprint"
            )
        fingerprint = adapter.definition_contract_fingerprint()
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            adapter_kind = "deterministic" if adapter.deterministic else "live"
            raise ValueError(
                f"{adapter_kind} adapter {type(adapter).__name__} must provide "
                "a non-empty current configuration fingerprint"
            )
        if (
            not adapter.deterministic
            and fingerprint != contract.configuration_fingerprint
        ):
            raise ValueError(
                f"adapter {type(adapter).__name__} configuration fingerprint does "
                "not match its ModelContract fingerprint"
            )
        return fingerprint

    def effective_model_bindings(self) -> ModelBindingSet:
        requirements = self.effective_model_requirements()
        output_requirements = ModelRequirements()
        if (
            self.output_contract is not None
            and self.output_contract.structured_output.value != "NONE"
        ):
            output_requirements = ModelRequirements(
                capabilities=ModelCapabilities(
                    structured_output=self.output_contract.structured_output
                )
            )
        contract = self._model_contract_for(self.model_adapter)
        bindings = self.model_bindings.resolved()
        primary = bindings.for_purpose(ModelPurpose.PRIMARY)
        if primary.contract != contract:
            raise ModelCapabilityError(
                "PRIMARY Model Binding contract does not match the adapter "
                "instance contract"
            )
        effective_primary = primary.model_copy(
            update={
                "adapter_configuration_fingerprint": (
                    self._adapter_configuration_fingerprint(
                        self.model_adapter, primary.contract
                    )
                ),
                "requirements": primary.requirements.merged_with(
                    requirements
                ).merged_with(
                    output_requirements
                ).effective_for(primary.contract)
            }
        )
        return ModelBindingSet(
            bindings=tuple(
                effective_primary
                if binding.purpose is ModelPurpose.PRIMARY
                else effective_primary.model_copy(
                    update={
                        "purpose": binding.purpose,
                        "source_purpose": ModelPurpose.PRIMARY,
                    }
                )
                if binding.source_purpose is ModelPurpose.PRIMARY
                else binding.model_copy(
                    update={
                        "adapter_configuration_fingerprint": (
                            self._adapter_configuration_fingerprint(
                                self.model_adapter_for(binding.purpose),
                                binding.contract,
                            )
                        ),
                        "requirements": (
                            binding.requirements.merged_with(output_requirements)
                            if binding.purpose is ModelPurpose.OUTPUT_REPAIR
                            else binding.requirements
                        ).effective_for(binding.contract)
                    }
                )
                for binding in bindings.bindings
            )
        )

    def model_adapter_for(self, purpose: ModelPurpose) -> ModelAdapter:
        """Return the executable Adapter selected for one frozen purpose."""
        binding = self.model_bindings.resolved().for_purpose(purpose)
        if (
            purpose is ModelPurpose.PRIMARY
            or binding.source_purpose is ModelPurpose.PRIMARY
        ):
            return self.model_adapter
        try:
            return self.model_adapters[purpose]
        except KeyError:
            raise ModelCapabilityError(
                f"Model Binding {purpose.value} has no Adapter owner"
            ) from None

    def frozen_snapshot(self) -> DefinitionSnapshot:
        """冻结当前版本为 Run 使用的不可变 Definition Snapshot。"""
        return DefinitionSnapshot(
            definition_id=self.definition_id,
            version=self.version,
            instructions=self.instructions,
            model_bindings=self.effective_model_bindings(),
            required_capabilities=self.required_capabilities,
            adapter_capabilities=self.model_adapter.capabilities,
            adapter_contract_fingerprint=(
                self.model_adapter.definition_contract_fingerprint()
            ),
            model_execution_budget=self.model_execution_budget,
            has_context_provider=self.context_provider is not None,
            tool_declarations=tuple(
                ToolDeclaration(name=tool.name, effect=tool.effect)
                for tool in self.tools
            ),
            retry_policy=self.retry_policy,
            policy_identity=self.run_policy.identity,
            output_contract=self.output_contract,
            context_plan=self.context_plan,
            compression_contract=self.compression_contract,
        )


class DefinitionRegistry:
    """按 `definition_id + version` 精确解析可执行 Definition 的注册表。

    只保留运行时对象引用，不持久化、不反序列化代码。
    """

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], AgentDefinition] = {}
        self._contract_fingerprints: dict[tuple[str, str], str] = {}

    def register(self, definition: AgentDefinition) -> None:
        """注册一个不可变 Definition，并在注册时校验能力兼容性。

        同一 id + version 重复注册抛 :class:`DefinitionConflictError`
        （不可变定义禁止覆盖）；能力校验失败抛
        :class:`ModelCapabilityError`（无静默降级）。
        """
        key = (definition.definition_id, definition.version)
        if key in self._definitions:
            raise DefinitionConflictError(
                f"definition {definition.definition_id}@{definition.version} "
                "is already registered and immutable"
            )
        try:
            bindings = definition.effective_model_bindings()
        except ModelCapabilityError:
            raise
        contract_fingerprints = dict(self._contract_fingerprints)
        declared_bindings = definition.model_bindings.resolved()
        adapter_contracts: dict[int, ModelContract] = {}
        for binding in bindings.bindings:
            adapter = definition.model_adapter_for(binding.purpose)
            adapter_key = id(adapter)
            adapter_contract = adapter_contracts.get(adapter_key)
            if adapter_contract is None:
                adapter_contract = definition._model_contract_for(adapter)
                ceiling_match = adapter.capabilities.capability_ceiling_match(
                    adapter_contract.capabilities
                )
                if not ceiling_match.compatible:
                    raise ModelCapabilityError(
                        f"adapter {type(adapter).__name__} class capability "
                        "ceiling is incompatible with its Model Contract: "
                        f"reason_code={ceiling_match.reason.value}"
                    )
                definition._adapter_configuration_fingerprint(
                    adapter, adapter_contract
                )
                adapter_contracts[adapter_key] = adapter_contract
            declared_binding = declared_bindings.for_purpose(binding.purpose)
            if adapter_contract != declared_binding.contract:
                raise ModelCapabilityError(
                    f"Model Binding {binding.purpose.value} Adapter owner "
                    "does not match its Model Contract"
                )
            match = binding.requirements.match(binding.contract)
            if not match.compatible:
                missing = match.reason.value
                raise ModelCapabilityError(
                    f"definition {definition.definition_id}@{definition.version} "
                    f"is incompatible with {binding.purpose.value} Model Contract: "
                    f"reason_code={missing}"
                )
            output_contract = definition.output_contract
            if (
                output_contract is not None
                and binding.purpose
                in (ModelPurpose.PRIMARY, ModelPurpose.OUTPUT_REPAIR)
            ):
                adapter.assert_output_contract_schema(
                    output_contract.structured_output,
                    output_contract.schema_definition,
                )
            contract_key = (
                binding.contract.contract_id,
                binding.contract.version,
            )
            fingerprint = binding.contract.fingerprint
            assert fingerprint is not None
            existing = contract_fingerprints.get(contract_key)
            if existing is not None and existing != fingerprint:
                raise DefinitionConflictError(
                    "Model Contract "
                    f"{binding.contract.contract_id}@{binding.contract.version} "
                    "is already registered with different immutable semantics"
                )
            contract_fingerprints[contract_key] = fingerprint
        self._definitions[key] = definition
        self._contract_fingerprints = contract_fingerprints

    def resolve(self, definition_id: str, version: str) -> AgentDefinition:
        """按精确 id + version 解析；不匹配抛 DefinitionNotFoundError。"""
        try:
            return self._definitions[(definition_id, version)]
        except KeyError:
            raise DefinitionNotFoundError(
                f"no registered definition for "
                f"{definition_id}@{version}"
            ) from None

    def is_registered(self, definition_id: str, version: str) -> bool:
        return (definition_id, version) in self._definitions

    def registered_definitions(self) -> tuple[AgentDefinition, ...]:
        """已注册定义（测试与检查用；顺序不保证）。"""
        return tuple(self._definitions.values())
