"""Semantic Compression 契约、结果与校验（ADR 0040）。

ADR 0040：任何调用模型的 Semantic Compression 都是
``purpose=CONTEXT_COMPRESSION`` 的独立、可追踪、可计费 Model Step，
使用版本化**有损** Compression Contract、保留原始 Item 与派生引用，
且不递归触发 Pipeline、业务 Tool 或 Output Repair。

- :class:`CompressionContract`：版本化契约，声明允许压缩的输入范围
  （``allowed_sources``）、保留/省略/派生信息类别与输出约束；契约
  恒为 lossy——绝不允许声明为无损变换。
- :class:`CompressionResult`：一次 compression Model Step 的结构化
  输出（Checkpoint 载荷），记录被消费的原始 item 引用、派生 Items
  及其 direct-derivation provenance 与计量。
- :func:`parse_compression_output` / :func:`validate_compression_result`：
  把 compression Model Response 解析并校验为 :class:`CompressionResult`；
  未知派生引用（tampered provenance）、无 provenance、item 冲突、
  超出输出约束或非压缩（扩张）输出都以
  :class:`CompressionContractViolationError` fail closed。
- :func:`apply_compression`：把 Compression Result 应用到聚合 Frame
  Items——被消费的原始 Item 被派生 Item 取代，其余 Item 原样保留；
  Result 引用不存在的 Item 时 fail closed（provenance drift）。

Compression 只处理契约允许的 Context Items。Conversation History、
instructions、run input、Tool Outcomes 与 protected evidence 永不
进入压缩输入（由 Runner 构造 compression ModelRequest 时强制）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ._context import ContextItem
from ._context_plan import ModelInputSizer
from ._errors import ModelContractViolationError


class CompressionContractViolationError(ModelContractViolationError):
    """压缩输出或压缩证据违反冻结的 Compression Contract。

    继承 :class:`ModelContractViolationError`（compression 输出是
    Model Response 的一部分），但携带独立的稳定错误标识
    ``COMPRESSION_CONTRACT_VIOLATION``，使失败路径机器可读且与
    一般 Model Contract 违约可区分。
    """

    code = "COMPRESSION_CONTRACT_VIOLATION"


class _FrozenCompressionValue(BaseModel, frozen=True):
    """Frozen value type for Compression structures (extra='forbid')."""

    model_config = ConfigDict(extra="forbid")


class CompressionContract(_FrozenCompressionValue):
    """版本化、显式有损的 Semantic Compression Contract（ADR 0040）。

    - ``contract_id`` / ``version``：契约身份；语义变化必须发布新版本，
      恢复时按 (id, version) 精确复用，不得用最新契约重算旧 Run。
    - ``instructions``：compression Model Step 的指令（受信输入）。
      它与业务 Agent Instruction 完全独立，绝不复用。
    - ``allowed_sources``：允许压缩的 Context Item ``source`` 精确集合。
      不在集合内的 Item（以及 instructions / run input / Conversation
      History / Tool Outcomes / protected evidence）永不被压缩，而是
      原样传递。
    - ``retained_categories`` / ``omitted_categories`` /
      ``derived_categories``：显式声明的保留 / 省略 / 派生信息类别——
      有损性必须被声明，不得默认无损。
    - ``max_output_items``：单次压缩输出 Item 数上限（None 为不限）。
    - ``lossy``：恒为 True。压缩是有损变换；声明无损在构造期即被拒绝。
    """

    contract_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    allowed_sources: tuple[str, ...] = Field(min_length=1)
    retained_categories: tuple[str, ...] = Field(min_length=1)
    omitted_categories: tuple[str, ...] = Field(min_length=1)
    derived_categories: tuple[str, ...] = Field(default_factory=tuple)
    max_output_items: int | None = Field(default=None, ge=1)
    lossy: bool = True

    @model_validator(mode="after")
    def _validate_explicit_lossy_declaration(self) -> "CompressionContract":
        for name, values in (
            ("allowed_sources", self.allowed_sources),
            ("retained_categories", self.retained_categories),
            ("omitted_categories", self.omitted_categories),
            ("derived_categories", self.derived_categories),
        ):
            if any(not value.strip() for value in values):
                raise ValueError(
                    f"CompressionContract {name} entries must be non-empty"
                )
        if len(set(self.allowed_sources)) != len(self.allowed_sources):
            raise ValueError("allowed_sources must be unique")
        for name in ("retained_categories", "omitted_categories", "derived_categories"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
        # 有损性是压缩契约的本质声明，不是可选项。
        if self.lossy is not True:
            raise ValueError(
                "CompressionContract must declare lossy=True; semantic "
                "compression is an explicitly lossy transform"
            )
        return self

    def allows_item(self, item: ContextItem) -> bool:
        """Return whether one Context Item is inside the declared input range."""
        return item.source in self.allowed_sources

    def derived_item_source(self) -> str:
        """Return the stable source stamped on every derived Item."""
        return f"compression:{self.contract_id}:{self.version}"


class CompressionProvenance(_FrozenCompressionValue):
    """一条派生 Context Item 的压缩溯源记录。

    - ``contract_id`` / ``contract_version``：产生本 Item 的契约身份；
    - ``source_item_ids``：直接派生出本 Item 的原始 item_id 列表
      （非空；必须能在被压缩输入集中解析——tampered provenance 在
      校验期即被拒绝）。
    """

    contract_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    source_item_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_source_references(self) -> "CompressionProvenance":
        if any(not value.strip() for value in self.source_item_ids):
            raise ValueError("source_item_ids entries must be non-empty")
        if len(set(self.source_item_ids)) != len(self.source_item_ids):
            raise ValueError("source_item_ids must be unique")
        return self


class CompressedContextItem(_FrozenCompressionValue):
    """带压缩溯源的派生 Context Item。"""

    item: ContextItem
    provenance: CompressionProvenance


class CompressionResult(_FrozenCompressionValue):
    """一次 compression Model Step 的结构化输出（Checkpoint 载荷）。

    - ``source_item_ids``：本次压缩消费的全部原始 item_id（有序、唯一）；
      原始 Item 本身保留在其来源 Stage checkpoint 中，绝不改写。
    - ``output_items``：派生 Items 及其 direct-derivation provenance。
    - ``omitted_categories``：契约声明的省略类别（显式有损标记）。
    - ``measurement``：输入 / 输出 token 估算（保守 Sizer）。
    """

    contract_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    source_item_ids: tuple[str, ...] = Field(default_factory=tuple)
    output_items: tuple[CompressedContextItem, ...] = Field(default_factory=tuple)
    omitted_categories: tuple[str, ...] = Field(default_factory=tuple)
    measurement: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_result_shape(self) -> "CompressionResult":
        if len(set(self.source_item_ids)) != len(self.source_item_ids):
            raise ValueError("source_item_ids must be unique")
        item_ids = [wrapped.item.item_id for wrapped in self.output_items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("output item ids must be unique")
        for wrapped in self.output_items:
            if (
                wrapped.provenance.contract_id != self.contract_id
                or wrapped.provenance.contract_version != self.contract_version
            ):
                raise ValueError(
                    "output item provenance must reference the result contract"
                )
            if wrapped.item.source != (
                f"compression:{self.contract_id}:{self.contract_version}"
            ):
                raise ValueError(
                    "output items must carry the compression derived source"
                )
        return self

    def serialize(self) -> str:
        """Serialize to JSON for Checkpoint persistence."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def deserialize(cls, payload: str) -> "CompressionResult":
        """Reconstruct from a Checkpoint payload."""
        try:
            return cls.model_validate_json(payload)
        except ValidationError as exc:
            raise CompressionContractViolationError(
                "compression checkpoint is not a valid CompressionResult"
            ) from exc


def _require_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CompressionContractViolationError(
            f"compression output entry field {key!r} must be a non-empty string"
        )
    return value


def validate_compression_result(
    result: CompressionResult,
    *,
    contract: CompressionContract,
    source_items: Sequence[ContextItem],
) -> None:
    """Validate one Compression Result against its contract and sources.

    校验边界（全部 fail closed，抛
    :class:`CompressionContractViolationError`）：

    - Result 的契约身份必须与冻结契约一致；
    - ``source_item_ids`` 必须能在被压缩输入集中解析（tampered /
      drifted provenance 拒绝）；
    - 每个输出 Item 的 ``source_item_ids`` 非空且 ⊆ 被压缩输入集；
    - 输出 Item 不得与原始 Item 或彼此冲突；
    - 输出 Item 数不得超过 ``max_output_items``；
    - 输出不得扩张（保守估算的总输出不得大于总输入——压缩必须更紧凑）。
    """
    if (
        result.contract_id != contract.contract_id
        or result.contract_version != contract.version
    ):
        raise CompressionContractViolationError(
            "compression result contract identity does not match the "
            "frozen Compression Contract"
        )
    source_ids = {item.item_id for item in source_items}
    unknown = [
        item_id
        for item_id in result.source_item_ids
        if item_id not in source_ids
    ]
    if unknown:
        raise CompressionContractViolationError(
            "compression result references source items outside the "
            "compressed input set"
        )
    if contract.max_output_items is not None and (
        len(result.output_items) > contract.max_output_items
    ):
        raise CompressionContractViolationError(
            "compression output exceeds the declared max_output_items"
        )
    output_ids = {wrapped.item.item_id for wrapped in result.output_items}
    colliding = output_ids & source_ids
    if colliding:
        raise CompressionContractViolationError(
            "compression output item ids collide with source items"
        )
    for wrapped in result.output_items:
        unknown_refs = [
            item_id
            for item_id in wrapped.provenance.source_item_ids
            if item_id not in source_ids
        ]
        if unknown_refs:
            raise CompressionContractViolationError(
                "compression output provenance references items outside "
                "the compressed input set"
            )
    input_estimate = sum(
        ModelInputSizer.estimate_text(item.content) for item in source_items
    )
    output_estimate = sum(
        ModelInputSizer.estimate_text(wrapped.item.content)
        for wrapped in result.output_items
    )
    if output_estimate > input_estimate:
        raise CompressionContractViolationError(
            "compression output is not more compact than its input"
        )


def parse_compression_output(
    content: str | None,
    *,
    contract: CompressionContract,
    source_items: Sequence[ContextItem],
) -> CompressionResult:
    """Parse and validate a compression Model Response content.

    模型输出必须是 JSON 对象 ``{"items": [...]}``；每个条目声明
    ``item_id``、``content``、``source_item_ids``（可选 ``metadata``）。
    派生 Item 的 ``source`` 由运行时统一加盖（模型不得伪造来源），
    provenance 携带冻结契约身份。任何违反都以
    :class:`CompressionContractViolationError` fail closed。
    """
    if content is None:
        raise CompressionContractViolationError(
            "compression response must carry content"
        )
    try:
        payload = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise CompressionContractViolationError(
            "compression response is not valid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CompressionContractViolationError(
            "compression response must be a JSON object with an 'items' array"
        )
    source_ids = {item.item_id for item in source_items}
    derived_source = contract.derived_item_source()
    output_items: list[CompressedContextItem] = []
    for entry in payload["items"]:
        if not isinstance(entry, dict):
            raise CompressionContractViolationError(
                "compression output items must be JSON objects"
            )
        item_id = _require_str(entry, "item_id")
        item_content = _require_str(entry, "content")
        raw_refs = entry.get("source_item_ids")
        if (
            not isinstance(raw_refs, list)
            or not raw_refs
            or any(
                not isinstance(ref, str) or not ref.strip() for ref in raw_refs
            )
        ):
            raise CompressionContractViolationError(
                "compression output item must declare non-empty source_item_ids"
            )
        metadata = entry.get("metadata", {})
        if not isinstance(metadata, dict):
            raise CompressionContractViolationError(
                "compression output item metadata must be a JSON object"
            )
        unknown_refs = [ref for ref in raw_refs if ref not in source_ids]
        if unknown_refs:
            raise CompressionContractViolationError(
                "compression output provenance references unknown source items"
            )
        if item_id in source_ids:
            raise CompressionContractViolationError(
                "compression output item id collides with a source item"
            )
        output_items.append(
            CompressedContextItem(
                item=ContextItem(
                    item_id=item_id,
                    content=item_content,
                    source=derived_source,
                    metadata=dict(metadata),
                ),
                provenance=CompressionProvenance(
                    contract_id=contract.contract_id,
                    contract_version=contract.version,
                    source_item_ids=tuple(raw_refs),
                ),
            )
        )
    result = CompressionResult(
        contract_id=contract.contract_id,
        contract_version=contract.version,
        source_item_ids=tuple(item.item_id for item in source_items),
        output_items=tuple(output_items),
        omitted_categories=contract.omitted_categories,
        measurement={
            "input_tokens_estimate": sum(
                ModelInputSizer.estimate_text(item.content)
                for item in source_items
            ),
            "output_tokens_estimate": sum(
                ModelInputSizer.estimate_text(wrapped.item.content)
                for wrapped in output_items
            ),
            "source_item_count": len(source_items),
            "output_item_count": len(output_items),
        },
    )
    validate_compression_result(
        result, contract=contract, source_items=source_items
    )
    return result


def apply_compression(
    items: Sequence[ContextItem],
    result: CompressionResult,
) -> tuple[ContextItem, ...]:
    """Apply one validated Compression Result to aggregated Frame Items.

    被消费的原始 Item 被派生 Item 取代；未消费的 Item（含 protected
    source 与其他 scope 的 Item）原样保留。Result 引用当前 Frame 中
    不存在的 Item 时 fail closed（provenance drift / tamper）。
    """
    current_ids = {item.item_id for item in items}
    drifted = [
        item_id
        for item_id in result.source_item_ids
        if item_id not in current_ids
    ]
    if drifted:
        raise CompressionContractViolationError(
            "compression result consumes items absent from the current frame"
        )
    consumed = set(result.source_item_ids)
    kept = [item for item in items if item.item_id not in consumed]
    return (*kept, *(wrapped.item for wrapped in result.output_items))


def compression_step_id(contract: CompressionContract) -> str:
    """Return the deterministic Step id for one compression invocation.

    同一 Run 内该 identity 稳定：已完成 checkpoint 据此复用，未完成的
    invocation 以同一 identity 重放（at-least-once）。
    """
    return f"compression:{contract.contract_id}:{contract.version}"


def compression_task_input(contract: CompressionContract) -> str:
    """Return the deterministic compression ModelRequest input."""
    return json.dumps(
        {
            "task": "semantic-compression",
            "contract_id": contract.contract_id,
            "contract_version": contract.version,
            "retained_categories": list(contract.retained_categories),
            "omitted_categories": list(contract.omitted_categories),
            "derived_categories": list(contract.derived_categories),
            "max_output_items": contract.max_output_items,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
