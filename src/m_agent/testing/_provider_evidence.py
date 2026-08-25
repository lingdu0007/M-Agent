"""Honest freshness labeling for PROVIDER evidence (Ticket 16 / ADR 0042).

PROVIDER 层证据是显式授权、凭据门控的 live endpoint 合同记录。本模块只做
**离线的元数据标注**：按 Model Contract fingerprint 与 30 天规则把一条既有
证据标注为 ``VALID``、``STALE`` 或 ``NOT_RUN``。它绝不发起 provider 请求，
也绝不把 CONTRACT/HOST 结果升级为 PROVIDER 结论——缺失证据只能如实标注
``NOT_RUN``。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Mapping


PROVIDER_EVIDENCE_MAX_AGE_DAYS = 30


class ProviderEvidenceStatus(str, Enum):
    """Freshness labels for recorded PROVIDER evidence."""

    VALID = "VALID"
    STALE = "STALE"
    NOT_RUN = "NOT_RUN"


def provider_evidence_status(
    *,
    contract_fingerprint: str,
    evidence: Mapping[str, object] | None,
    now: datetime,
    max_age_days: int = PROVIDER_EVIDENCE_MAX_AGE_DAYS,
) -> ProviderEvidenceStatus:
    """Label one recorded PROVIDER evidence record for a Model Contract.

    ``evidence`` 是一条已归档的 live 验证记录，必须携带
    ``contract_fingerprint``（验证时冻结的 Model Contract 指纹）与
    ``verified_at``（tz-aware 时间戳）。规则（ADR 0042）：

    - 没有记录 → ``NOT_RUN``（缺失不能解释为通过）；
    - 指纹不匹配 → ``STALE``（记录存在但不证明这个契约修订）；
    - 时间戳晚于 ``now`` 或超过 ``max_age_days`` 天 → ``STALE``；
    - 其余（指纹匹配且不超过窗口）→ ``VALID``。

    该函数只读取归档元数据，绝不触发网络请求；naive 时间戳因无法与
    ``now`` 可靠比较而 fail closed 抛 ``ValueError``。
    """
    if not contract_fingerprint:
        raise ValueError("contract_fingerprint must not be empty")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if max_age_days < 0:
        raise ValueError("max_age_days must not be negative")
    if evidence is None:
        return ProviderEvidenceStatus.NOT_RUN
    verified_at = evidence.get("verified_at")
    recorded_fingerprint = evidence.get("contract_fingerprint")
    if not isinstance(verified_at, datetime):
        return ProviderEvidenceStatus.NOT_RUN
    if not isinstance(recorded_fingerprint, str) or not recorded_fingerprint:
        return ProviderEvidenceStatus.NOT_RUN
    if verified_at.tzinfo is None:
        raise ValueError("provider evidence verified_at must be timezone-aware")
    if recorded_fingerprint != contract_fingerprint:
        return ProviderEvidenceStatus.STALE
    if verified_at > now:
        return ProviderEvidenceStatus.STALE
    if verified_at < now - timedelta(days=max_age_days):
        return ProviderEvidenceStatus.STALE
    return ProviderEvidenceStatus.VALID
