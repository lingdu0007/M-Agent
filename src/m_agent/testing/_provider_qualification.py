"""Qualify live PROVIDER evidence for one release candidate (Ticket 21).

Release Owner 通过独立、显式授权的命令验证当前 RC 受影响的官方
endpoint，并以**可审计的已决议状态**区分未授权、凭据、配额、
provider、contract 与 harness 问题。本模块是纯离线的编排与记录
表面：

- 不读取原始 prompt、不持久化凭证、不回显 provider 错误正文；
- 未显式授权（环境 opt-in + 命令 ``--allow-live``）时绝不发起任何
  provider 请求，引擎在未授权路径上强制 request counter 为零；
- 结果绑定当前 RC 的 artifact digest、Model Contract 指纹、adapter
  配置指纹、endpoint alias、capability case 与最小环境身份；其他
  wheel 的结果无法附加到当前 RC；
- PROVIDER Report revision 只追加、不改写正式 Pack Execution；
  修复后的 release acceptance 必须创建新 RC 重跑 required profile。

真实的 live dispatch 由调用方（``m_agent.testing verify-provider`` 命令
或显式授权的 Release Owner 流程）通过注入的 ``dispatch`` 回调提供；
本模块对 dispatch 的任何结局都归入八种已决议状态之一，绝不把
PROVIDER 缺失改写为 CONTRACT/HOST 失败。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from ..runtime import (
    ModelCapabilityError,
    ModelContractViolationError,
    ModelFailure,
)
from ._pack import AcceptanceManifest, PackExecution
from ._provider_evidence import (
    PROVIDER_EVIDENCE_MAX_AGE_DAYS,
    ProviderEvidenceStatus,
    provider_evidence_status,
)


PROVIDER_QUALIFICATION_SCHEMA_VERSION = "1"

#: 与官方 live adapter（``m_agent.adapters.provider``）共用的授权环境
#: 表面：仅当显式 opt-in 且嵌入环境有凭证时，live 验证才可能被授权。
PROVIDER_LIVE_OPT_IN_ENV = "M_AGENT_RUN_LIVE_TESTS"
PROVIDER_CREDENTIAL_ENV_NAMES = ("M_AGENT_OPENAI_API_KEY", "OPENAI_API_KEY")

#: 受影响的官方 endpoint alias（0.5：两个 OpenAI 兼容端点）。
PROVIDER_ENDPOINT_ALIASES = frozenset(
    {"openai-chat-completions", "openai-responses"}
)

#: 每个 endpoint 必须资格化的 capability case：官方 Chat/Responses
#: 行为、structured mode、streaming、usage normalization 与声明 Limits。
PROVIDER_CAPABILITY_CASES = frozenset(
    {
        "text_behavior",
        "structured_mode",
        "streaming",
        "usage_normalization",
        "declared_limits",
    }
)


class ProviderVerificationStatus(StrEnum):
    """八种已决议的 provider 验证状态（可审计、可区分）。"""

    OPTED_OUT = "OPTED_OUT"
    CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    CONTRACT_FAILURE = "CONTRACT_FAILURE"
    HARNESS_ERROR = "HARNESS_ERROR"
    PASS = "PASS"
    STALE = "STALE"


PROVIDER_VERIFICATION_STATUSES: tuple[ProviderVerificationStatus, ...] = tuple(
    ProviderVerificationStatus
)

REASON_OPTED_OUT = "opted_out"
REASON_CREDENTIALS_MISSING = "credentials_missing"
REASON_QUOTA_BLOCKED = "quota_blocked"
REASON_PROVIDER_ERROR = "provider_error"
REASON_CONTRACT_FAILURE = "contract_failure"
REASON_CASE_ASSERTION_FAILED = "case_assertion_failed"
REASON_HARNESS_ERROR = "harness_error"
REASON_DISPATCH_VERIFIED = "dispatch_verified"
REASON_REUSED_VALID_EVIDENCE = "reused_valid_evidence"
REASON_STALE_CONTRACT_FINGERPRINT_DRIFT = "stale_contract_fingerprint_drift"
REASON_STALE_ADAPTER_CONFIGURATION_DRIFT = (
    "stale_adapter_configuration_drift"
)
REASON_STALE_EVIDENCE_EXPIRED = "stale_evidence_expired"

_SHA256_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_STABLE_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_HTTP_STATUS_IN_MESSAGE = re.compile(r"HTTP (\d{3})")
_ENDPOINT_USERINFO = re.compile(r"://[^/@\s]+@")
_SENSITIVE_KEY_FRAGMENTS = (
    "credential",
    "key",
    "password",
    "secret",
    "token",
    "authorization",
)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_digest(value: str, name: str) -> None:
    if not _SHA256_DIGEST.fullmatch(value):
        raise ValueError(f"{name} must be an sha256: digest")


class _FrozenQualificationValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# 授权入口
# ---------------------------------------------------------------------------


class ProviderVerificationAuthorization(_FrozenQualificationValue):
    """一次 provider 验证运行的显式授权范围。

    ``authorized`` 只有在环境显式 opt-in **且** 凭证存在时才为真；
    未授权时 ``blocked_status`` 说明阻塞原因（OPTED_OUT /
    CREDENTIALS_MISSING），且任何 dispatch 都不得发生。本对象只记录
    凭证**是否存在**，绝不记录或回显凭证值。
    """

    authorized: bool
    blocked_status: ProviderVerificationStatus | None
    endpoint_aliases: frozenset[str]

    @model_validator(mode="after")
    def _validate_authorization_state(self) -> Self:
        if not self.endpoint_aliases or not self.endpoint_aliases <= frozenset(
            PROVIDER_ENDPOINT_ALIASES
        ):
            raise ValueError(
                "authorization endpoint aliases must be a nonempty subset of "
                "the known official endpoints"
            )
        if self.authorized:
            if self.blocked_status is not None:
                raise ValueError(
                    "an authorized verification cannot carry a blocked status"
                )
        elif self.blocked_status not in (
            ProviderVerificationStatus.OPTED_OUT,
            ProviderVerificationStatus.CREDENTIALS_MISSING,
        ):
            raise ValueError(
                "an unauthorized verification must record OPTED_OUT or "
                "CREDENTIALS_MISSING"
            )
        return self

    @classmethod
    def opted_out(
        cls, endpoint_aliases: frozenset[str]
    ) -> "ProviderVerificationAuthorization":
        """命令行未显式 ``--allow-live`` 时的强制退出状态。"""
        return cls(
            authorized=False,
            blocked_status=ProviderVerificationStatus.OPTED_OUT,
            endpoint_aliases=endpoint_aliases,
        )


def provider_verification_authorization(
    environ: Mapping[str, str],
    *,
    endpoint_aliases: frozenset[str],
) -> ProviderVerificationAuthorization:
    """只根据显式 opt-in 与凭证存在性解析授权（不读取凭证值）。"""
    if (
        not endpoint_aliases
        or not endpoint_aliases <= frozenset(PROVIDER_ENDPOINT_ALIASES)
    ):
        raise ValueError(
            "endpoint aliases must be a nonempty subset of the known "
            "official endpoints"
        )
    if environ.get(PROVIDER_LIVE_OPT_IN_ENV) != "1":
        return ProviderVerificationAuthorization.opted_out(endpoint_aliases)
    if not any(
        environ.get(name) for name in PROVIDER_CREDENTIAL_ENV_NAMES
    ):
        return ProviderVerificationAuthorization(
            authorized=False,
            blocked_status=ProviderVerificationStatus.CREDENTIALS_MISSING,
            endpoint_aliases=endpoint_aliases,
        )
    return ProviderVerificationAuthorization(
        authorized=True,
        blocked_status=None,
        endpoint_aliases=endpoint_aliases,
    )


# ---------------------------------------------------------------------------
# endpoint × capability matrix
# ---------------------------------------------------------------------------


def provider_capability_matrix(
    endpoint_aliases: frozenset[str] = PROVIDER_ENDPOINT_ALIASES,
) -> tuple[tuple[str, str], ...]:
    """受影响 endpoint 必须资格化的全部 (alias, capability case) 组合。"""
    if (
        not endpoint_aliases
        or not endpoint_aliases <= frozenset(PROVIDER_ENDPOINT_ALIASES)
    ):
        raise ValueError(
            "endpoint aliases must be a nonempty subset of the known "
            "official endpoints"
        )
    return tuple(
        (alias, case)
        for alias in sorted(endpoint_aliases)
        for case in sorted(PROVIDER_CAPABILITY_CASES)
    )


# ---------------------------------------------------------------------------
# RC 绑定与记录
# ---------------------------------------------------------------------------


class ProviderCaseBinding(_FrozenQualificationValue):
    """一条验证结果绑定的最小、非敏感身份。

    绑定当前 RC artifact digest、Model Contract 指纹、adapter 配置
    指纹、endpoint alias、capability case 与最小环境身份（provider 家族
    与 model identity）。凭证、原始响应与 endpoint secret 不属于绑定。
    """

    artifact_digest: str
    contract_fingerprint: str
    adapter_configuration_fingerprint: str
    endpoint_alias: str
    capability_case: str
    provider: str
    model_identity: str

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        _require_digest(self.artifact_digest, "artifact_digest")
        if not _FINGERPRINT.fullmatch(self.contract_fingerprint):
            raise ValueError("contract_fingerprint must be a sha256 hex digest")
        if not _FINGERPRINT.fullmatch(self.adapter_configuration_fingerprint):
            raise ValueError(
                "adapter_configuration_fingerprint must be a sha256 hex digest"
            )
        if self.endpoint_alias not in PROVIDER_ENDPOINT_ALIASES:
            raise ValueError("endpoint_alias must be a known official endpoint")
        if self.capability_case not in PROVIDER_CAPABILITY_CASES:
            raise ValueError("capability_case must be a known capability case")
        if not self.provider.strip() or not self.model_identity.strip():
            raise ValueError(
                "provider and model_identity must be nonempty"
            )
        return self


def provider_case_evidence_digest(
    *,
    binding: ProviderCaseBinding,
    status: ProviderVerificationStatus,
    request_count: int,
    reason_code: str,
    verified_at: datetime,
) -> str:
    """对一条记录的最小结构化证据计算规范摘要（不含任何原始数据）。"""
    payload = {
        "schema_version": PROVIDER_QUALIFICATION_SCHEMA_VERSION,
        "binding": binding.model_dump(mode="json"),
        "status": status.value,
        "request_count": request_count,
        "reason_code": reason_code,
        "verified_at": verified_at.isoformat(),
    }
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


class ProviderCaseRecord(_FrozenQualificationValue):
    """一个 (endpoint, capability case) 的已决议验证结果。"""

    binding: ProviderCaseBinding
    status: ProviderVerificationStatus
    request_count: int = Field(ge=0)
    verified_at: datetime
    reason_code: str
    evidence_digest: str

    @model_validator(mode="after")
    def _validate_record(self) -> Self:
        _require_aware(self.verified_at, "verified_at")
        if not _STABLE_REASON.fullmatch(self.reason_code):
            raise ValueError("reason_code must be a stable identifier")
        _require_digest(self.evidence_digest, "evidence_digest")
        if self.status is ProviderVerificationStatus.OPTED_OUT and (
            self.request_count != 0
        ):
            raise ValueError("OPTED_OUT records cannot report requests")
        if self.status is ProviderVerificationStatus.STALE and (
            self.request_count != 0
        ):
            raise ValueError("STALE records are evaluations and cannot report requests")
        if (
            self.status is ProviderVerificationStatus.PASS
            and self.request_count == 0
            and self.reason_code != REASON_REUSED_VALID_EVIDENCE
        ):
            raise ValueError(
                "PASS without dispatch must reuse recorded valid evidence"
            )
        expected = provider_case_evidence_digest(
            binding=self.binding,
            status=self.status,
            request_count=self.request_count,
            reason_code=self.reason_code,
            verified_at=self.verified_at,
        )
        if self.evidence_digest != expected:
            raise ValueError("evidence_digest does not match the record content")
        return self


# ---------------------------------------------------------------------------
# 验证引擎：dispatch 结局 → 八种状态
# ---------------------------------------------------------------------------


def provider_failure_http_status(failure: ModelFailure) -> int | None:
    """从结构化 provider 失败中提取稳定 HTTP 状态码（若无则 None）。"""
    match = _HTTP_STATUS_IN_MESSAGE.search(failure.message)
    return int(match.group(1)) if match else None


def _record(
    binding: ProviderCaseBinding,
    status: ProviderVerificationStatus,
    *,
    request_count: int,
    reason_code: str,
    verified_at: datetime,
) -> ProviderCaseRecord:
    return ProviderCaseRecord(
        binding=binding,
        status=status,
        request_count=request_count,
        verified_at=verified_at,
        reason_code=reason_code,
        evidence_digest=provider_case_evidence_digest(
            binding=binding,
            status=status,
            request_count=request_count,
            reason_code=reason_code,
            verified_at=verified_at,
        ),
    )


async def verify_provider_capability_case(
    *,
    authorization: ProviderVerificationAuthorization,
    binding: ProviderCaseBinding,
    dispatch: Callable[[], Awaitable[object]],
    request_counter: Callable[[], int],
    assertion: Callable[[object], None] | None = None,
    now: datetime,
) -> ProviderCaseRecord:
    """以显式授权运行一个 capability case 并归入已决议状态。

    未授权时绝不调用 ``dispatch``，并强制 ``request_counter`` 读数为
    零（出现任何请求都是对授权边界的违反）。授权后的 dispatch 结局
    映射为：

    - :class:`ModelFailure` ``provider_credentials_missing`` →
      ``CREDENTIALS_MISSING``；
    - HTTP 429 → ``QUOTA_BLOCKED``；
    - 其余 :class:`ModelFailure` → ``PROVIDER_ERROR``；
    - :class:`ModelContractViolationError` /
      :class:`ModelCapabilityError` / 断言失败 → ``CONTRACT_FAILURE``；
    - 其他异常 → ``HARNESS_ERROR``；
    - 成功且断言通过 → ``PASS``。

    异常消息与响应正文绝不进入记录；只保留稳定原因码。
    """
    _require_aware(now, "now")
    if not authorization.authorized:
        observed = request_counter()
        if observed != 0:
            raise ValueError(
                "a live provider request was made without explicit "
                "authorization"
            )
        assert authorization.blocked_status is not None
        reason = (
            REASON_OPTED_OUT
            if authorization.blocked_status
            is ProviderVerificationStatus.OPTED_OUT
            else REASON_CREDENTIALS_MISSING
        )
        return _record(
            binding,
            authorization.blocked_status,
            request_count=0,
            reason_code=reason,
            verified_at=now,
        )
    try:
        observation = await dispatch()
    except ModelFailure as failure:
        if failure.code == "provider_credentials_missing":
            status, reason = (
                ProviderVerificationStatus.CREDENTIALS_MISSING,
                REASON_CREDENTIALS_MISSING,
            )
        elif provider_failure_http_status(failure) == 429:
            status, reason = (
                ProviderVerificationStatus.QUOTA_BLOCKED,
                REASON_QUOTA_BLOCKED,
            )
        else:
            status, reason = (
                ProviderVerificationStatus.PROVIDER_ERROR,
                REASON_PROVIDER_ERROR,
            )
        return _record(
            binding,
            status,
            request_count=request_counter(),
            reason_code=reason,
            verified_at=now,
        )
    except (ModelContractViolationError, ModelCapabilityError):
        return _record(
            binding,
            ProviderVerificationStatus.CONTRACT_FAILURE,
            request_count=request_counter(),
            reason_code=REASON_CONTRACT_FAILURE,
            verified_at=now,
        )
    except AssertionError:
        return _record(
            binding,
            ProviderVerificationStatus.CONTRACT_FAILURE,
            request_count=request_counter(),
            reason_code=REASON_CASE_ASSERTION_FAILED,
            verified_at=now,
        )
    except Exception:
        return _record(
            binding,
            ProviderVerificationStatus.HARNESS_ERROR,
            request_count=request_counter(),
            reason_code=REASON_HARNESS_ERROR,
            verified_at=now,
        )
    if assertion is not None:
        try:
            assertion(observation)
        except AssertionError:
            return _record(
                binding,
                ProviderVerificationStatus.CONTRACT_FAILURE,
                request_count=request_counter(),
                reason_code=REASON_CASE_ASSERTION_FAILED,
                verified_at=now,
            )
        except (ModelContractViolationError, ModelCapabilityError):
            return _record(
                binding,
                ProviderVerificationStatus.CONTRACT_FAILURE,
                request_count=request_counter(),
                reason_code=REASON_CONTRACT_FAILURE,
                verified_at=now,
            )
    observed = request_counter()
    if observed < 1:
        raise ValueError(
            "request counter did not observe the dispatched provider request"
        )
    return _record(
        binding,
        ProviderVerificationStatus.PASS,
        request_count=observed,
        reason_code=REASON_DISPATCH_VERIFIED,
        verified_at=now,
    )


# ---------------------------------------------------------------------------
# 证据复用：fingerprint 一致 + 30 天窗口
# ---------------------------------------------------------------------------


def reuse_archived_provider_evidence(
    archived: ProviderCaseRecord,
    *,
    endpoint_alias: str,
    capability_case: str,
    contract_fingerprint: str,
    adapter_configuration_fingerprint: str,
    artifact_digest: str,
    provider: str,
    model_identity: str,
    now: datetime,
    max_age_days: int = PROVIDER_EVIDENCE_MAX_AGE_DAYS,
) -> ProviderCaseRecord | None:
    """评估一条已归档记录能否为当前 RC 的这个 case 提供证据。

    - 别名 / case / 环境身份不匹配，或归档记录不是 PASS（失败是结局
      而非证据）→ ``None``（无可复用证据，需要重验）；
    - adapter 配置或 Contract 指纹漂移 → 绑定当前 RC 的 ``STALE``
      记录（必须在当前 RC 重验）；
    - 超过 ``max_age_days`` 窗口 → ``STALE``；
    - 指纹一致且新鲜 → 绑定当前 RC artifact 的 ``PASS`` 复用记录
      （request_count=0，verified_at 保留原始验证时间）。
    """
    _require_aware(now, "now")
    if (
        archived.binding.endpoint_alias != endpoint_alias
        or archived.binding.capability_case != capability_case
        or archived.binding.provider != provider
        or archived.binding.model_identity != model_identity
    ):
        return None
    if archived.status is not ProviderVerificationStatus.PASS:
        return None
    binding = ProviderCaseBinding(
        artifact_digest=artifact_digest,
        contract_fingerprint=contract_fingerprint,
        adapter_configuration_fingerprint=adapter_configuration_fingerprint,
        endpoint_alias=endpoint_alias,
        capability_case=capability_case,
        provider=provider,
        model_identity=model_identity,
    )
    if (
        archived.binding.adapter_configuration_fingerprint
        != adapter_configuration_fingerprint
    ):
        return _record(
            binding,
            ProviderVerificationStatus.STALE,
            request_count=0,
            reason_code=REASON_STALE_ADAPTER_CONFIGURATION_DRIFT,
            verified_at=now,
        )
    if archived.binding.contract_fingerprint != contract_fingerprint:
        return _record(
            binding,
            ProviderVerificationStatus.STALE,
            request_count=0,
            reason_code=REASON_STALE_CONTRACT_FINGERPRINT_DRIFT,
            verified_at=now,
        )
    freshness = provider_evidence_status(
        contract_fingerprint=contract_fingerprint,
        evidence={
            "contract_fingerprint": archived.binding.contract_fingerprint,
            "verified_at": archived.verified_at,
        },
        now=now,
        max_age_days=max_age_days,
    )
    if freshness is ProviderEvidenceStatus.NOT_RUN:
        return None
    if freshness is ProviderEvidenceStatus.STALE:
        return _record(
            binding,
            ProviderVerificationStatus.STALE,
            request_count=0,
            reason_code=REASON_STALE_EVIDENCE_EXPIRED,
            verified_at=now,
        )
    return _record(
        binding,
        ProviderVerificationStatus.PASS,
        request_count=0,
        reason_code=REASON_REUSED_VALID_EVIDENCE,
        verified_at=archived.verified_at,
    )


# ---------------------------------------------------------------------------
# PROVIDER Report：append-only revision，绝不改写 Pack Execution
# ---------------------------------------------------------------------------


class ProviderReportRewriteError(ValueError):
    """A PROVIDER report cannot rewrite the formal Pack Execution."""


class ProviderReportRevision(_FrozenQualificationValue):
    """一次授权（或未授权）运行追加的记录集合。"""

    revision: int = Field(ge=1)
    created_at: datetime
    endpoint_scope: frozenset[str]
    request_count: int = Field(ge=0)
    records: tuple[ProviderCaseRecord, ...]
    redaction_verified: bool

    @field_serializer("endpoint_scope")
    def _serialize_endpoint_scope(self, value: frozenset[str]) -> list[str]:
        # The report content digest is computed over this serialization, so it
        # must never depend on per-process set iteration order (PYTHONHASHSEED
        # reshuffles it; a rebuilt frozenset can iterate differently from the
        # list it was parsed from, making the digest unverifiable).
        return sorted(value)

    @model_validator(mode="after")
    def _validate_revision(self) -> Self:
        _require_aware(self.created_at, "created_at")
        if not self.records:
            raise ValueError("a report revision requires at least one record")
        if not self.endpoint_scope or not self.endpoint_scope <= frozenset(
            PROVIDER_ENDPOINT_ALIASES
        ):
            raise ValueError(
                "revision endpoint scope must be a nonempty subset of "
                "the known official endpoints"
            )
        if not self.redaction_verified:
            raise ValueError(
                "a report revision can only be archived after the redaction "
                "check passed"
            )
        seen: set[tuple[str, str]] = set()
        for record in self.records:
            key = (
                record.binding.endpoint_alias,
                record.binding.capability_case,
            )
            if key in seen:
                raise ValueError(
                    "revision records must cover distinct endpoint/case pairs"
                )
            seen.add(key)
            if record.binding.endpoint_alias not in self.endpoint_scope:
                raise ValueError(
                    "revision records must stay inside the endpoint scope"
                )
        if self.request_count != sum(
            record.request_count for record in self.records
        ):
            raise ValueError(
                "revision request_count must equal the sum of its records"
            )
        return self


class ProviderVerificationReport(_FrozenQualificationValue):
    """绑定一个 RC（Manifest digest + artifact digest + 正式执行）的
    PROVIDER 验证报告。

    revision 只能追加：后续授权的结果以更高 revision 号附加，早先
    revision 原样保留；报告绝不改写正式 Pack Execution（见
    :func:`attach_provider_report`），修复后的 release acceptance 必须
    创建新 RC 并重跑 required profile（见
    :func:`require_new_rc_release_acceptance`）。
    """

    report_id: str
    manifest_digest: str
    artifact_digest: str
    execution_id: str
    endpoint_aliases: frozenset[str]
    revisions: tuple[ProviderReportRevision, ...] = ()
    content_digest: str

    @field_serializer("endpoint_aliases")
    def _serialize_endpoint_aliases(self, value: frozenset[str]) -> list[str]:
        # Same rationale as ProviderReportRevision.endpoint_scope: the
        # content digest must be stable across processes and hash seeds.
        return sorted(value)

    @model_validator(mode="after")
    def _validate_report(self) -> Self:
        if not self.report_id.strip() or not self.execution_id.strip():
            raise ValueError("report_id and execution_id must be nonempty")
        _require_digest(self.manifest_digest, "manifest_digest")
        _require_digest(self.artifact_digest, "artifact_digest")
        if (
            not self.endpoint_aliases
            or not self.endpoint_aliases <= frozenset(PROVIDER_ENDPOINT_ALIASES)
        ):
            raise ValueError(
                "report endpoint aliases must be a nonempty subset of the "
                "known official endpoints"
            )
        for index, revision in enumerate(self.revisions, start=1):
            if revision.revision != index:
                raise ValueError(
                    "report revisions must be contiguous and append-only"
                )
            for record in revision.records:
                if record.binding.artifact_digest != self.artifact_digest:
                    raise ValueError(
                        "report records must be bound to the report's "
                        "release candidate artifact"
                    )
                if record.binding.endpoint_alias not in self.endpoint_aliases:
                    raise ValueError(
                        "report records must stay inside the report scope"
                    )
        return self

    @classmethod
    def create(
        cls,
        *,
        report_id: str,
        manifest_digest: str,
        artifact_digest: str,
        execution_id: str,
        endpoint_aliases: frozenset[str],
    ) -> "ProviderVerificationReport":
        """为当前 RC 创建一个还没有 revision 的空报告。"""
        return cls(
            report_id=report_id,
            manifest_digest=manifest_digest,
            artifact_digest=artifact_digest,
            execution_id=execution_id,
            endpoint_aliases=endpoint_aliases,
            revisions=(),
            content_digest="",
        )._with_content_digest()

    def _with_content_digest(self) -> "ProviderVerificationReport":
        payload = self.model_dump(mode="json", exclude={"content_digest"})
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        values = self.model_dump(mode="json")
        values["content_digest"] = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        return type(self).model_validate(values)

    def append_revision(
        self,
        *,
        records: Sequence[ProviderCaseRecord],
        created_at: datetime,
        authorization: ProviderVerificationAuthorization,
        redaction_verified: bool,
    ) -> "ProviderVerificationReport":
        """追加一次运行的记录；其他 wheel 的结果在此被拒绝。"""
        _require_aware(created_at, "created_at")
        if not redaction_verified:
            raise ValueError(
                "a revision can only be appended after the redaction check "
                "passed"
            )
        records = tuple(records)
        if not records:
            raise ValueError("a report revision requires at least one record")
        for record in records:
            if record.binding.artifact_digest != self.artifact_digest:
                raise ValueError(
                    "records from another wheel cannot be attached to this "
                    "release candidate"
                )
            if record.binding.endpoint_alias not in self.endpoint_aliases:
                raise ValueError(
                    "records must stay inside the report's endpoint scope"
                )
        record_aliases = {
            record.binding.endpoint_alias for record in records
        }
        if authorization.authorized:
            if not record_aliases <= set(authorization.endpoint_aliases):
                raise ValueError(
                    "authorized records must stay inside the authorized "
                    "endpoint scope"
                )
        else:
            assert authorization.blocked_status is not None
            for record in records:
                if record.status is not authorization.blocked_status:
                    raise ValueError(
                        "an unauthorized revision can only record the "
                        "authorization's blocked status"
                    )
        next_revision = (
            self.revisions[-1].revision + 1 if self.revisions else 1
        )
        revision = ProviderReportRevision(
            revision=next_revision,
            created_at=created_at,
            endpoint_scope=frozenset(record_aliases),
            request_count=sum(record.request_count for record in records),
            records=records,
            redaction_verified=True,
        )
        values = self.model_dump(mode="json")
        values["revisions"] = [
            *values["revisions"],
            revision.model_dump(mode="json"),
        ]
        values["content_digest"] = ""
        return type(self).model_validate(values)._with_content_digest()

    def latest_case_records(
        self,
    ) -> dict[tuple[str, str], ProviderCaseRecord]:
        """每个 (alias, case) 的最新记录（后追加的 revision 胜出）。"""
        latest: dict[tuple[str, str], ProviderCaseRecord] = {}
        for revision in self.revisions:
            for record in revision.records:
                latest[
                    (
                        record.binding.endpoint_alias,
                        record.binding.capability_case,
                    )
                ] = record
        return latest

    def missing_cases(self) -> tuple[tuple[str, str], ...]:
        """报告范围内还没有任何记录的 (alias, case) 组合。"""
        covered = set(self.latest_case_records())
        return tuple(
            pair
            for pair in provider_capability_matrix(self.endpoint_aliases)
            if pair not in covered
        )

    def verify(self) -> None:
        """重算内容摘要并校验 append-only 结构。"""
        expected = self._with_content_digest()
        if self.content_digest != expected.content_digest:
            raise ValueError(
                "provider verification report content digest does not match"
            )


def attach_provider_report(
    report: ProviderVerificationReport,
    execution: PackExecution,
) -> PackExecution:
    """把报告附到正式 Pack Execution 上：只做身份校验，原样返回。

    PROVIDER revision 可以来后追加，但它们**绝不改写**正式 Pack
    Execution —— 本函数是唯一的附加入口，且不产生任何新的执行状态。
    """
    if (
        report.manifest_digest != execution.manifest_digest
        or report.execution_id != execution.execution_id
    ):
        raise ProviderReportRewriteError(
            "provider report does not belong to this Pack Execution"
        )
    return execution


def require_new_rc_release_acceptance(
    execution: PackExecution,
    manifest: AcceptanceManifest,
    *,
    execution_id: str,
) -> PackExecution:
    """修复后的 release acceptance 必须创建新 RC 并重跑 required profile。

    同一 Manifest（同一 RC）不允许重新开一个执行来"修复"结论；只有
    新的 RC（新 Manifest digest）才能开启新的 release acceptance，且
    新执行从 ``CREATED`` 开始完整重跑。
    """
    if manifest.digest == execution.manifest_digest:
        raise ValueError(
            "fixed release acceptance requires a new release candidate "
            "Manifest and a full rerun of the required profile"
        )
    return PackExecution.create(manifest, execution_id=execution_id)


# ---------------------------------------------------------------------------
# Catalog verified 门禁：阻塞 verified 状态，但不跨层误报
# ---------------------------------------------------------------------------


class VerifiedRecommendationStatus(StrEnum):
    """默认推荐 Catalog 项的 PROVIDER 资格状态。

    只有 PROVIDER 域的结论：证据缺失/漂移阻塞 ``VERIFIED``，但绝不
    冒充 CONTRACT/HOST 失败——那些层级的证据由各自的 Pack 与 Router
    独立判定。
    """

    VERIFIED = "VERIFIED"
    BLOCKED_STALE = "BLOCKED_STALE"
    BLOCKED_MISSING = "BLOCKED_MISSING"


def verified_recommendation_status(
    *,
    endpoint_alias: str,
    contract_fingerprint: str,
    adapter_configuration_fingerprint: str,
    records: Sequence[ProviderCaseRecord],
    now: datetime,
    max_age_days: int = PROVIDER_EVIDENCE_MAX_AGE_DAYS,
) -> VerifiedRecommendationStatus:
    """评估默认推荐项的 PROVIDER 证据是否维持 verified 推荐状态。

    每个 capability case 都需要最新记录为 PASS、指纹一致且在
    ``max_age_days`` 窗口内。证据过期、alias/fingerprint 漂移或验证
    失败 → ``BLOCKED_STALE``；完全缺失 → ``BLOCKED_MISSING``。序列顺
    序即 revision 顺序，后出现的记录胜出。
    """
    _require_aware(now, "now")
    if not contract_fingerprint or not adapter_configuration_fingerprint:
        raise ValueError("fingerprints must be nonempty")
    blocked: VerifiedRecommendationStatus | None = None
    for case in sorted(PROVIDER_CAPABILITY_CASES):
        current = [
            record
            for record in records
            if record.binding.endpoint_alias == endpoint_alias
            and record.binding.capability_case == case
        ]
        if current:
            record = current[-1]
            if record.status is not ProviderVerificationStatus.PASS:
                blocked = VerifiedRecommendationStatus.BLOCKED_STALE
                continue
            if (
                record.binding.contract_fingerprint != contract_fingerprint
                or record.binding.adapter_configuration_fingerprint
                != adapter_configuration_fingerprint
            ):
                blocked = VerifiedRecommendationStatus.BLOCKED_STALE
                continue
            freshness = provider_evidence_status(
                contract_fingerprint=contract_fingerprint,
                evidence={
                    "contract_fingerprint": (
                        record.binding.contract_fingerprint
                    ),
                    "verified_at": record.verified_at,
                },
                now=now,
                max_age_days=max_age_days,
            )
            if freshness is not ProviderEvidenceStatus.VALID:
                blocked = VerifiedRecommendationStatus.BLOCKED_STALE
            continue
        drifted_alias = any(
            record.binding.capability_case == case
            and record.binding.endpoint_alias != endpoint_alias
            for record in records
        )
        if drifted_alias:
            blocked = VerifiedRecommendationStatus.BLOCKED_STALE
        else:
            blocked = VerifiedRecommendationStatus.BLOCKED_MISSING
    if blocked is not None:
        return blocked
    return VerifiedRecommendationStatus.VERIFIED


# ---------------------------------------------------------------------------
# 脱敏扫描
# ---------------------------------------------------------------------------


def _is_sensitive_key_name(name: str) -> bool:
    lowered = name.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redaction_findings(
    payload: object,
    *,
    canary_values: Sequence[str],
) -> list[str]:
    """扫描结构化证据，返回稳定的问题码列表（空列表即干净）。

    检测三类泄漏：credential canary 值、敏感字段名（credential/key/
    password/secret/token/authorization）与携带 userinfo 的 endpoint
    URL。发现的描述只含稳定问题码，绝不回显泄漏值本身。
    """
    findings: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str) and _is_sensitive_key_name(key):
                    findings.add("sensitive_field_present")
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            for canary in canary_values:
                if canary and canary in value:
                    findings.add("credential_canary_present")
            if _ENDPOINT_USERINFO.search(value):
                findings.add("endpoint_secret_present")

    walk(payload)
    return sorted(findings)


def assert_provider_evidence_sanitized(
    payload: object,
    *,
    canary_values: Sequence[str],
) -> None:
    """归档前强制脱敏检查；有任何发现即抛出稳定错误码。"""
    findings = redaction_findings(payload, canary_values=canary_values)
    if findings:
        raise ValueError(
            "provider evidence failed redaction: " + ",".join(findings)
        )


__all__ = [
    "PROVIDER_CAPABILITY_CASES",
    "PROVIDER_CREDENTIAL_ENV_NAMES",
    "PROVIDER_ENDPOINT_ALIASES",
    "PROVIDER_LIVE_OPT_IN_ENV",
    "PROVIDER_QUALIFICATION_SCHEMA_VERSION",
    "PROVIDER_VERIFICATION_STATUSES",
    "ProviderCaseBinding",
    "ProviderCaseRecord",
    "ProviderReportRevision",
    "ProviderReportRewriteError",
    "ProviderVerificationAuthorization",
    "ProviderVerificationReport",
    "ProviderVerificationStatus",
    "REASON_CASE_ASSERTION_FAILED",
    "REASON_CONTRACT_FAILURE",
    "REASON_CREDENTIALS_MISSING",
    "REASON_DISPATCH_VERIFIED",
    "REASON_HARNESS_ERROR",
    "REASON_OPTED_OUT",
    "REASON_PROVIDER_ERROR",
    "REASON_QUOTA_BLOCKED",
    "REASON_REUSED_VALID_EVIDENCE",
    "REASON_STALE_ADAPTER_CONFIGURATION_DRIFT",
    "REASON_STALE_CONTRACT_FINGERPRINT_DRIFT",
    "REASON_STALE_EVIDENCE_EXPIRED",
    "VerifiedRecommendationStatus",
    "assert_provider_evidence_sanitized",
    "attach_provider_report",
    "provider_capability_matrix",
    "provider_case_evidence_digest",
    "provider_failure_http_status",
    "provider_verification_authorization",
    "redaction_findings",
    "require_new_rc_release_acceptance",
    "reuse_archived_provider_evidence",
    "verified_recommendation_status",
    "verify_provider_capability_case",
]
