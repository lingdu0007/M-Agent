"""冻结的 Agent Variant 与 Model Catalog（ADR 0041）。

Agent Variant 是同一评估或业务目标下可独立注册、选择和比较的完整
版本化声明：冻结 Definition 身份、完整 Model Binding Set（含各用途的
Requirements、Contract、Sizer、Limits 与预算）及所需 Routing Policy
身份。Model Catalog 是组织已注册 Agent Variant 及其静态 Model
Contract 身份与非敏感 deployment 属性的选择视图：不持有模型凭证、
不通过探测 provider 发现能力，也不参与 Runner 执行循环。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..._model import (
    ModelBindingSet,
    ModelContract,
    ModelExecutionBudget,
    ModelPurpose,
)
from ._policy import RoutingPolicyIdentity, canonical_digest


class _FrozenCatalogValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeploymentAttributes(_FrozenCatalogValue):
    """一个 Catalog 实例的非敏感 deployment 属性。

    只描述 provider、region、endpoint class 与数据保留声明版本；
    凭据、tenant ID、敏感 endpoint 与数据正文不进入 Catalog。未声明
    （``None``）的属性在该维度被 hard 约束时 fail closed。
    """

    provider: str | None = None
    region: str | None = None
    endpoint_class: str | None = None
    retention_evidence_version: str | None = None


class AgentVariant(_FrozenCatalogValue):
    """冻结的完整 Agent Variant 声明。

    - ``variant_id + version`` 是稳定身份与最终 tie-break 键；
    - ``model_bindings`` 是完整 Model Binding Set：每个用途显式绑定
      Model Requirements、Model Contract（含 Sizer/Limits/指纹）；
    - ``model_execution_budget`` 是随 Variant 冻结的 dispatch 尝试上限；
    - ``policy_identities`` 是该 Variant 注册参与选择的 Routing Policy
      精确身份（显式发布新 Policy 版本需要显式重新注册）。
    """

    variant_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)
    model_bindings: ModelBindingSet
    model_execution_budget: ModelExecutionBudget
    policy_identities: tuple[RoutingPolicyIdentity, ...]

    @model_validator(mode="after")
    def _validate_policy_identities(self) -> "AgentVariant":
        if not self.policy_identities:
            raise ValueError(
                "agent variant must declare at least one routing policy identity"
            )
        seen: set[tuple[str, str]] = set()
        for identity in self.policy_identities:
            key = (identity.policy_id, identity.version)
            if key in seen:
                raise ValueError("routing policy identities must be unique")
            seen.add(key)
        return self

    def variant_digest(self) -> str:
        """Variant 冻结内容的规范化摘要。"""
        return canonical_digest(self.model_dump(mode="json"))

    def primary_contract(self) -> ModelContract:
        """PRIMARY 用途绑定的 Model Contract。"""
        return self.model_bindings.for_purpose(ModelPurpose.PRIMARY).contract

    @property
    def identity(self) -> tuple[str, str]:
        """稳定身份键： ``(variant_id, version)``。"""
        return (self.variant_id, self.version)


class ModelCatalogEntry(_FrozenCatalogValue):
    """Catalog 中一个可实例：冻结 Variant 与非敏感 deployment 属性。"""

    variant: AgentVariant
    deployment: DeploymentAttributes = Field(
        default_factory=DeploymentAttributes
    )


class ModelCatalog(_FrozenCatalogValue):
    """不可变的版本化 Model Catalog。

    Catalog 是选择视图而不是可变注册服务：身份冲突与 Contract 指纹
    冲突由 Router 在路由时确定性检出（``CATALOG_CONFLICT``），同
    ``variant_id + version`` 且内容一致的重复条目按稳定身份去重。
    """

    catalog_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    entries: tuple[ModelCatalogEntry, ...] = Field(default_factory=tuple)

    def catalog_digest(self) -> str:
        """按声明顺序对全部条目计算规范化摘要。"""
        return canonical_digest(self.model_dump(mode="json"))

    def canonical_entries(self) -> tuple[ModelCatalogEntry, ...]:
        """按稳定 ``(variant_id, version)`` 排序并去重的候选视图。"""
        by_identity: dict[tuple[str, str], ModelCatalogEntry] = {}
        for entry in sorted(
            self.entries, key=lambda item: item.variant.identity
        ):
            by_identity.setdefault(entry.variant.identity, entry)
        return tuple(by_identity.values())
