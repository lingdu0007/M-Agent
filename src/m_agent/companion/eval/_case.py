"""Eval Case / Suite：冻结的评估声明与确定性展开（ADR 0029）。

Eval Case 冻结 input、Conversation History、Agent Variant、
content-addressed Fixture Bundle、Execution Protocol、Evaluator versions
与 tags；任何冻结字段缺失或非法都在构造时确定性失败。Eval Suite 把
Case × Variant × repetition 展开为身份稳定、互不共享状态的独立单元——
绝不在已启动 Run 内替换模型或配置。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..._history import ConversationMessage
from ..._model import ModelCapabilities
from ._errors import EvalSuiteError
from ._identity import canonical_json, digest_of, sha256_hex


class AgentVariant(BaseModel):
    """一个可独立注册、选择与比较的完整版本化 Agent Definition 身份。

    它指向注册表中的精确 definition id + version；不是已启动 Run 内
    可替换的模型参数（CONTEXT.md「Agent Variant」）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    variant_id: str = Field(min_length=1)
    definition_id: str = Field(min_length=1)
    definition_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_blank(self) -> "AgentVariant":
        if not self.variant_id.strip():
            raise ValueError("variant_id must be a non-blank identifier")
        if not self.definition_id.strip():
            raise ValueError("definition_id must be a non-blank identifier")
        if not self.definition_version.strip():
            raise ValueError("definition_version must be a non-blank string")
        return self


class FixtureFact(BaseModel):
    """Fixture Bundle 中一条内容冻结的外部事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    payload: str


class FixtureBundle(BaseModel):
    """content-addressed Fixture Bundle：外部事实与外部效果白名单。

    ``digest`` 由内容派生并在构造时复核：篡改或缺失即构造失败。
    ``declared_external_effects`` 是允许在 EXECUTE 中产生外部效果的
    工具名白名单——fixture 之外的访问 fail closed。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bundle_id: str = Field(min_length=1)
    facts: tuple[FixtureFact, ...] = ()
    declared_external_effects: tuple[str, ...] = ()
    expected_evidence_ids: tuple[str, ...] = ()
    digest: str = Field(min_length=64, max_length=64)

    @classmethod
    def build(
        cls,
        *,
        bundle_id: str,
        facts: tuple[FixtureFact, ...] = (),
        declared_external_effects: tuple[str, ...] = (),
        expected_evidence_ids: tuple[str, ...] = (),
    ) -> "FixtureBundle":
        """按内容计算 digest 并冻结为不可变 Bundle。"""
        return cls(
            bundle_id=bundle_id,
            facts=tuple(facts),
            declared_external_effects=tuple(declared_external_effects),
            expected_evidence_ids=tuple(expected_evidence_ids),
            digest=cls.content_digest(
                bundle_id,
                tuple(facts),
                tuple(declared_external_effects),
                tuple(expected_evidence_ids),
            ),
        )

    @staticmethod
    def content_digest(
        bundle_id: str,
        facts: tuple[FixtureFact, ...],
        declared_external_effects: tuple[str, ...],
        expected_evidence_ids: tuple[str, ...],
    ) -> str:
        payload = {
            "bundle_id": bundle_id,
            "facts": [
                {"fact_id": fact.fact_id, "payload": fact.payload}
                for fact in facts
            ],
            "declared_external_effects": sorted(declared_external_effects),
            "expected_evidence_ids": sorted(expected_evidence_ids),
        }
        return sha256_hex(canonical_json(payload))

    @model_validator(mode="after")
    def _verify_content_address(self) -> "FixtureBundle":
        expected = FixtureBundle.content_digest(
            self.bundle_id,
            self.facts,
            self.declared_external_effects,
            self.expected_evidence_ids,
        )
        if self.digest != expected:
            raise ValueError(
                "fixture bundle digest does not match its content; "
                "bundles are content-addressed and immutable"
            )
        return self


class ExecutionProtocol(BaseModel):
    """Case 的重复策略：确定性 Case 只跑一次，非确定性必须声明 seed。

    确定性 Case 强制 ``repetitions == 1``（PRD story 81：deterministic
    cases run once）；非确定性 Case 必须显式给出重复次数与 seed，使
    样本含义明确，不隐含 best-of-many。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    deterministic: bool = True
    repetitions: int = 1
    seed: str | None = None

    @model_validator(mode="after")
    def _validate_repetition_policy(self) -> "ExecutionProtocol":
        if self.repetitions < 1:
            raise ValueError("repetitions must be at least 1")
        if self.deterministic:
            if self.repetitions != 1:
                raise ValueError(
                    "deterministic cases run exactly once; declare "
                    "deterministic=False with a seed for repetition"
                )
        elif not (self.seed or "").strip():
            raise ValueError(
                "nondeterministic cases must declare a non-blank seed"
            )
        return self


class EvaluatorRef(BaseModel):
    """一个版本化 Evaluator 的精确引用（Case 冻结其版本）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluator_id: str = Field(min_length=1)
    version: str = Field(min_length=1)


class EvalCase(BaseModel):
    """一项不可变评估声明：回归比较的最小场景单位。

    冻结字段缺失或非法（空白 id、空 Evaluator 集合、被篡改的
    Fixture Bundle、非法重复策略）在构造时确定性失败。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str = Field(min_length=1)
    input: str
    history: tuple[ConversationMessage, ...] = ()
    variant: AgentVariant
    fixture_bundle: FixtureBundle
    execution_protocol: ExecutionProtocol = Field(
        default_factory=ExecutionProtocol
    )
    evaluators: tuple[EvaluatorRef, ...] = Field(min_length=1)
    tags: tuple[str, ...] = ()
    #: 可选静态能力门槛：缺失即 UNSUPPORTED、零 provider 请求。
    required_capabilities: ModelCapabilities | None = None

    @model_validator(mode="after")
    def _reject_blank_identity(self) -> "EvalCase":
        if not self.case_id.strip():
            raise ValueError("case_id must be a non-blank identifier")
        return self


class EvalSuiteItem(BaseModel):
    """Suite 展开出的一个独立执行单元（对应一次 subject Run）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    suite_id: str = Field(min_length=1)
    suite_version: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    variant: AgentVariant
    repetition_index: int = Field(ge=0)


class EvalSuite(BaseModel):
    """把 Case、Variant 与重复策略组合成可展开评估计划的边界。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    cases: tuple[EvalCase, ...] = Field(min_length=1)
    variants: tuple[AgentVariant, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_duplicates(self) -> "EvalSuite":
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("suite cases must have unique case_id values")
        variant_ids = [v.variant_id for v in self.variants]
        if len(set(variant_ids)) != len(variant_ids):
            raise ValueError("suite variants must have unique variant_id values")
        return self

    def expand(self) -> tuple[EvalSuiteItem, ...]:
        """按 case -> variant -> repetition 顺序确定性展开。

        Case 冻结的 Variant 必须在 Suite 变体集合中注册，否则在任何
        subject Run 创建之前抛 :class:`EvalSuiteError`（fail-closed）。
        item_id 由 Suite 身份派生：同 Suite 恒得到同一批 item identity。
        """
        items: list[EvalSuiteItem] = []
        for case in self.cases:
            if case.variant not in self.variants:
                raise EvalSuiteError(
                    f"case {case.case_id!r} freezes variant "
                    f"{case.variant.variant_id!r} which is not registered "
                    f"in suite {self.suite_id!r}"
                )
            for variant in self.variants:
                for repetition in range(case.execution_protocol.repetitions):
                    items.append(
                        EvalSuiteItem(
                            item_id=self._item_identity(
                                case, variant, repetition
                            ),
                            suite_id=self.suite_id,
                            suite_version=self.version,
                            case_id=case.case_id,
                            variant=variant,
                            repetition_index=repetition,
                        )
                    )
        return tuple(items)

    def content_digest(self) -> str:
        """Suite 冻结内容（含 Case/Variant/Bundle 声明）的稳定摘要。"""
        return sha256_hex(
            canonical_json(
                {
                    "suite_id": self.suite_id,
                    "version": self.version,
                    "cases": [
                        case.model_dump(mode="json") for case in self.cases
                    ],
                    "variants": [
                        variant.model_dump(mode="json")
                        for variant in self.variants
                    ],
                }
            )
        )

    def case_by_id(self, case_id: str) -> EvalCase:
        """按 id 取冻结 Case；不存在抛 :class:`EvalSuiteError`。"""
        for case in self.cases:
            if case.case_id == case_id:
                return case
        raise EvalSuiteError(
            f"suite {self.suite_id!r} has no case {case_id!r}"
        )

    def _item_identity(
        self, case: EvalCase, variant: AgentVariant, repetition: int
    ) -> str:
        return digest_of(
            "eval-suite-item",
            self.suite_id,
            self.version,
            case.case_id,
            variant.variant_id,
            str(repetition),
        )
