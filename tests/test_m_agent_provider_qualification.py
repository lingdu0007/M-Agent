"""Ticket 21：为 Release Candidate 资格化 live provider 证据。

这些契约测试只用公共 seam（``m_agent.testing`` 的 provider qualification
表面、``m_agent.companion.routing``、``m_agent.runtime``）冻结：

- **零 live request**：未显式授权时，授权入口、验证引擎、Router
  discovery、import 与 ``verify-provider`` 命令都不发任何 provider
  请求（引擎在未授权路径上强制 request counter 为零）；
- **八种已决议状态**：OPTED_OUT、CREDENTIALS_MISSING、QUOTA_BLOCKED、
  PROVIDER_ERROR、CONTRACT_FAILURE、HARNESS_ERROR、PASS 与 STALE 全部
  可区分且原因码稳定；
- **RC 绑定**：结果绑定 artifact digest、Contract/adapter 指纹、
  endpoint alias、capability case 与最小环境身份；其他 wheel 的结果
  不能附加到当前 RC；
- **复用规则**：fingerprint 一致且不超过 30 天的证据可复用为 PASS，
  Contract / adapter 配置漂移或过期判 STALE 必须在当前 RC 重验；
- **Catalog verified 门禁**：PROVIDER 证据缺失/漂移阻塞 verified 推荐
  状态，但绝不把 PROVIDER 缺失误写成 CONTRACT/HOST 失败；
- **脱敏**：credential canary、敏感字段与 endpoint secret 不进入任何
  保存的 provider 证据；
- **Report revision**：授权结果可后来追加，但绝不改写正式 Pack
  Execution；修复后的 release acceptance 必须创建新 RC 重跑。

所有测试离线运行（fake dispatch / MockTransport），默认 CI 无凭证、
无网络。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from m_agent.runtime import (
    ModelCapabilityError,
    ModelContractViolationError,
    ModelFailure,
    ModelResponse,
)
from m_agent.testing import (
    AcceptanceCheck,
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    AcceptanceManifest,
    EvidenceLevel,
    EXIT_INCOMPLETE,
    PackExecution,
    PackExecutionStatus,
    PROVIDER_CAPABILITY_CASES,
    PROVIDER_CREDENTIAL_ENV_NAMES,
    PROVIDER_ENDPOINT_ALIASES,
    PROVIDER_LIVE_OPT_IN_ENV,
    PROVIDER_VERIFICATION_STATUSES,
    ProviderCaseBinding,
    ProviderCaseRecord,
    ProviderReportRewriteError,
    ProviderVerificationAuthorization,
    ProviderVerificationReport,
    ProviderVerificationStatus,
    VerifiedRecommendationStatus,
    assert_provider_evidence_sanitized,
    attach_provider_report,
    provider_capability_matrix,
    provider_case_evidence_digest,
    provider_verification_authorization,
    redaction_findings,
    require_new_rc_release_acceptance,
    reuse_archived_provider_evidence,
    verified_recommendation_status,
    verify_provider_capability_case,
)

from m_agent.companion.routing import ModelRouter

from routing_fixtures import (
    make_catalog,
    make_contract,
    make_evidence,
    make_entry,
    make_policy,
    make_variant,
)


NOW = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
ARTIFACT_DIGEST = "sha256:" + "a" * 64
OTHER_ARTIFACT_DIGEST = "sha256:" + "b" * 64
CONTRACT_FINGERPRINT = "c" * 64
OTHER_CONTRACT_FINGERPRINT = "d" * 64
CONFIGURATION_FINGERPRINT = "e" * 64
OTHER_CONFIGURATION_FINGERPRINT = "f" * 64
CREDENTIAL_CANARY = "sk-provider-qualification-canary-7d61b4e2"

CHAT_ALIAS = "openai-chat-completions"
RESPONSES_ALIAS = "openai-responses"


def make_binding(
    *,
    alias: str = CHAT_ALIAS,
    capability_case: str = "text_behavior",
    artifact_digest: str = ARTIFACT_DIGEST,
    contract_fingerprint: str = CONTRACT_FINGERPRINT,
    adapter_configuration_fingerprint: str = CONFIGURATION_FINGERPRINT,
) -> ProviderCaseBinding:
    return ProviderCaseBinding(
        artifact_digest=artifact_digest,
        contract_fingerprint=contract_fingerprint,
        adapter_configuration_fingerprint=adapter_configuration_fingerprint,
        endpoint_alias=alias,
        capability_case=capability_case,
        provider="openai-compatible",
        model_identity="gpt-verification-model",
    )


def make_record(
    *,
    status: ProviderVerificationStatus = ProviderVerificationStatus.PASS,
    binding: ProviderCaseBinding | None = None,
    request_count: int = 1,
    verified_at: datetime = NOW,
    reason_code: str = "dispatch_verified",
) -> ProviderCaseRecord:
    binding = binding or make_binding()
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


def make_report(
    *,
    artifact_digest: str = ARTIFACT_DIGEST,
    manifest_digest: str = "sha256:" + "1" * 64,
    execution_id: str = "execution-1",
    endpoint_aliases: frozenset[str] | None = None,
    records: tuple[ProviderCaseRecord, ...] = (),
    authorization: ProviderVerificationAuthorization | None = None,
) -> ProviderVerificationReport:
    aliases = endpoint_aliases or frozenset({CHAT_ALIAS})
    report = ProviderVerificationReport.create(
        report_id="report-1",
        manifest_digest=manifest_digest,
        artifact_digest=artifact_digest,
        execution_id=execution_id,
        endpoint_aliases=aliases,
    )
    if records:
        report = report.append_revision(
            records=records,
            created_at=NOW,
            authorization=authorization
            or ProviderVerificationAuthorization(
                authorized=True, blocked_status=None, endpoint_aliases=aliases
            ),
            redaction_verified=True,
        )
    return report


def authorization(
    *, authorized: bool, aliases: frozenset[str] | None = None
) -> ProviderVerificationAuthorization:
    return ProviderVerificationAuthorization(
        authorized=authorized,
        blocked_status=None
        if authorized
        else ProviderVerificationStatus.OPTED_OUT,
        endpoint_aliases=aliases or frozenset({CHAT_ALIAS}),
    )


# ---------------------------------------------------------------------------
# 授权入口：显式 allow-live 才可能触发 live 请求
# ---------------------------------------------------------------------------


class ProviderAuthorizationTests(unittest.TestCase):
    """未显式 opt-in 时，环境里的凭证永远不足以授权 live 验证。"""

    def test_missing_opt_in_is_opted_out_even_with_ambient_credential(self) -> None:
        environ = {
            PROVIDER_LIVE_OPT_IN_ENV: "0",
            "M_AGENT_OPENAI_API_KEY": CREDENTIAL_CANARY,
            "OPENAI_API_KEY": CREDENTIAL_CANARY,
        }
        granted = provider_verification_authorization(
            environ, endpoint_aliases=frozenset({CHAT_ALIAS})
        )
        self.assertFalse(granted.authorized)
        assert granted.blocked_status is not None
        self.assertIs(granted.blocked_status, ProviderVerificationStatus.OPTED_OUT)

    def test_opt_in_without_credentials_reports_missing(self) -> None:
        granted = provider_verification_authorization(
            {PROVIDER_LIVE_OPT_IN_ENV: "1"},
            endpoint_aliases=frozenset({CHAT_ALIAS}),
        )
        self.assertFalse(granted.authorized)
        assert granted.blocked_status is not None
        self.assertIs(
            granted.blocked_status, ProviderVerificationStatus.CREDENTIALS_MISSING
        )

    def test_opt_in_with_credentials_authorizes(self) -> None:
        granted = provider_verification_authorization(
            {PROVIDER_LIVE_OPT_IN_ENV: "1", "M_AGENT_OPENAI_API_KEY": "present"},
            endpoint_aliases=frozenset({RESPONSES_ALIAS}),
        )
        self.assertTrue(granted.authorized)
        self.assertIsNone(granted.blocked_status)
        self.assertEqual(granted.endpoint_aliases, frozenset({RESPONSES_ALIAS}))

    def test_authorization_surface_is_closed(self) -> None:
        with self.assertRaises(ValueError):
            ProviderVerificationAuthorization(
                authorized=True,
                blocked_status=ProviderVerificationStatus.OPTED_OUT,
                endpoint_aliases=frozenset({CHAT_ALIAS}),
            )
        with self.assertRaises(ValueError):
            ProviderVerificationAuthorization(
                authorized=False,
                blocked_status=ProviderVerificationStatus.PASS,
                endpoint_aliases=frozenset({CHAT_ALIAS}),
            )
        with self.assertRaises(ValueError):
            ProviderVerificationAuthorization(
                authorized=False,
                blocked_status=None,
                endpoint_aliases=frozenset({CHAT_ALIAS}),
            )
        with self.assertRaises(ValueError):
            ProviderVerificationAuthorization(
                authorized=False,
                blocked_status=ProviderVerificationStatus.OPTED_OUT,
                endpoint_aliases=frozenset(),
            )

    def test_unknown_endpoint_alias_rejected(self) -> None:
        with self.assertRaises(ValueError):
            provider_verification_authorization(
                {},
                endpoint_aliases=frozenset({"unknown-endpoint"}),
            )

    def test_authorization_env_names_match_live_adapter_constants(self) -> None:
        """与官方 live adapter 的授权环境保持同一表面，防止漂移。"""
        try:
            from m_agent.adapters.provider import (
                LIVE_OPT_IN_ENV,
                live_contract_preflight,
            )
        except ImportError:
            self.skipTest("m-agent provider extra not installed")
        self.assertEqual(PROVIDER_LIVE_OPT_IN_ENV, LIVE_OPT_IN_ENV)
        # 与既有 preflight 的语义一致：仅凭环境凭证绝不授权。
        self.assertIsNotNone(live_contract_preflight({}))


# ---------------------------------------------------------------------------
# endpoint × capability matrix
# ---------------------------------------------------------------------------


class ProviderCapabilityMatrixTests(unittest.TestCase):
    def test_matrix_covers_every_endpoint_and_case(self) -> None:
        matrix = provider_capability_matrix()
        self.assertEqual(len(matrix), len(PROVIDER_ENDPOINT_ALIASES) * 5)
        for alias in PROVIDER_ENDPOINT_ALIASES:
            for case in PROVIDER_CAPABILITY_CASES:
                self.assertIn((alias, case), matrix)
        self.assertEqual(
            PROVIDER_CAPABILITY_CASES,
            frozenset(
                {
                    "text_behavior",
                    "structured_mode",
                    "streaming",
                    "usage_normalization",
                    "declared_limits",
                }
            ),
        )

    def test_matrix_scopes_to_requested_aliases(self) -> None:
        matrix = provider_capability_matrix(frozenset({RESPONSES_ALIAS}))
        self.assertEqual(
            set(matrix),
            {(RESPONSES_ALIAS, case) for case in PROVIDER_CAPABILITY_CASES},
        )

    def test_matrix_rejects_unknown_alias(self) -> None:
        with self.assertRaises(ValueError):
            provider_capability_matrix(frozenset({"unknown-endpoint"}))


# ---------------------------------------------------------------------------
# 验证引擎：八种已决议状态 + 零 live request 守卫
# ---------------------------------------------------------------------------


class ProviderCaseVerificationTests(unittest.IsolatedAsyncioTestCase):
    """每个 dispatch 结局必须落入且只落入一种已决议状态。"""

    def _counter(self, initial: int = 0) -> tuple[list[int], Any]:
        state = [initial]

        def read() -> int:
            return state[0]

        return state, read

    def _dispatch(self, observation: object = None, error: BaseException | None = None):
        calls: list[int] = []

        async def dispatch() -> object:
            calls.append(1)
            if error is not None:
                raise error
            return observation

        return dispatch, calls

    async def test_unauthorized_never_dispatches(self) -> None:
        dispatch, calls = self._dispatch()
        state, counter = self._counter(0)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=False),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.OPTED_OUT)
        self.assertEqual(record.request_count, 0)
        self.assertEqual(record.reason_code, "opted_out")
        self.assertEqual(calls, [])
        self.assertEqual(state, [0])

    async def test_unauthorized_counter_guard_rejects_any_request(self) -> None:
        dispatch, calls = self._dispatch()
        _, counter = self._counter(1)
        with self.assertRaisesRegex(ValueError, "authorization"):
            await verify_provider_capability_case(
                authorization=authorization(authorized=False),
                binding=make_binding(),
                dispatch=dispatch,
                request_counter=counter,
                now=NOW,
            )
        self.assertEqual(calls, [])

    async def test_credentials_missing_without_dispatch(self) -> None:
        dispatch, calls = self._dispatch()
        _, counter = self._counter(0)
        granted = ProviderVerificationAuthorization(
            authorized=False,
            blocked_status=ProviderVerificationStatus.CREDENTIALS_MISSING,
            endpoint_aliases=frozenset({CHAT_ALIAS}),
        )
        record = await verify_provider_capability_case(
            authorization=granted,
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.CREDENTIALS_MISSING)
        self.assertEqual(record.request_count, 0)
        self.assertEqual(calls, [])

    async def test_dispatch_success_records_pass(self) -> None:
        response = ModelResponse(content="pong")
        dispatch, calls = self._dispatch(response)
        _, counter = self._counter(2)

        def assert_text(observation: object) -> None:
            assert isinstance(observation, ModelResponse)
            if not observation.content or not observation.content.strip():
                raise AssertionError("empty content")

        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            assertion=assert_text,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.PASS)
        self.assertEqual(record.request_count, 2)
        self.assertEqual(record.reason_code, "dispatch_verified")
        self.assertEqual(record.verified_at, NOW)
        self.assertEqual(calls, [1])
        self.assertTrue(record.evidence_digest.startswith("sha256:"))

    async def test_dispatch_success_requires_observed_request(self) -> None:
        dispatch, _ = self._dispatch(ModelResponse(content="pong"))
        _, counter = self._counter(0)
        with self.assertRaisesRegex(ValueError, "request counter"):
            await verify_provider_capability_case(
                authorization=authorization(authorized=True),
                binding=make_binding(),
                dispatch=dispatch,
                request_counter=counter,
                now=NOW,
            )

    async def test_dispatch_credentials_failure_maps_to_credentials_missing(
        self,
    ) -> None:
        failure = ModelFailure(
            "PERMANENT",
            "provider_credentials_missing",
            f"no API key configured; {CREDENTIAL_CANARY}",
        )
        dispatch, _ = self._dispatch(error=failure)
        _, counter = self._counter(1)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.CREDENTIALS_MISSING)
        self.assertEqual(record.reason_code, "credentials_missing")
        self.assertEqual(record.request_count, 1)
        # 错误文本里的 canary 绝不进入保存的记录。
        self.assertNotIn(CREDENTIAL_CANARY, record.model_dump_json())

    async def test_dispatch_quota_429_maps_to_quota_blocked(self) -> None:
        failure = ModelFailure(
            "TRANSIENT",
            "provider_unavailable",
            "provider request failed with HTTP 429",
        )
        dispatch, _ = self._dispatch(error=failure)
        _, counter = self._counter(1)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.QUOTA_BLOCKED)
        self.assertEqual(record.reason_code, "quota_blocked")

    async def test_dispatch_provider_500_maps_to_provider_error(self) -> None:
        failure = ModelFailure(
            "TRANSIENT",
            "provider_unavailable",
            "provider request failed with HTTP 503",
        )
        dispatch, _ = self._dispatch(error=failure)
        _, counter = self._counter(1)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.PROVIDER_ERROR)
        self.assertEqual(record.reason_code, "provider_error")

    async def test_dispatch_transport_error_maps_to_provider_error(self) -> None:
        failure = ModelFailure(
            "TRANSIENT",
            "provider_transport_error",
            "provider request failed at the transport layer: ConnectError",
        )
        dispatch, _ = self._dispatch(error=failure)
        _, counter = self._counter(0)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.PROVIDER_ERROR)
        self.assertEqual(record.request_count, 0)

    async def test_dispatch_contract_violation_maps_to_contract_failure(self) -> None:
        dispatch, _ = self._dispatch(error=ModelContractViolationError("boom"))
        _, counter = self._counter(1)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.CONTRACT_FAILURE)
        self.assertEqual(record.reason_code, "contract_failure")

    async def test_dispatch_capability_error_maps_to_contract_failure(self) -> None:
        dispatch, _ = self._dispatch(error=ModelCapabilityError("undeclared"))
        _, counter = self._counter(0)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.CONTRACT_FAILURE)

    async def test_assertion_failure_maps_to_contract_failure(self) -> None:
        dispatch, _ = self._dispatch(ModelResponse(content=""))

        def assert_non_empty(observation: object) -> None:
            assert isinstance(observation, ModelResponse)
            if not observation.content:
                raise AssertionError("empty model content")

        _, counter = self._counter(1)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            assertion=assert_non_empty,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.CONTRACT_FAILURE)
        self.assertEqual(record.reason_code, "case_assertion_failed")

    async def test_unexpected_error_maps_to_harness_error(self) -> None:
        dispatch, _ = self._dispatch(error=RuntimeError("harness bug"))
        _, counter = self._counter(0)
        record = await verify_provider_capability_case(
            authorization=authorization(authorized=True),
            binding=make_binding(),
            dispatch=dispatch,
            request_counter=counter,
            now=NOW,
        )
        self.assertIs(record.status, ProviderVerificationStatus.HARNESS_ERROR)
        self.assertEqual(record.reason_code, "harness_error")

    async def test_naive_now_rejected(self) -> None:
        dispatch, _ = self._dispatch(ModelResponse(content="pong"))
        _, counter = self._counter(1)
        with self.assertRaises(ValueError):
            await verify_provider_capability_case(
                authorization=authorization(authorized=True),
                binding=make_binding(),
                dispatch=dispatch,
                request_counter=counter,
                now=datetime(2026, 8, 27, 12, 0, 0),
            )

    async def test_all_eight_statuses_are_distinguishable(self) -> None:
        """八种已决议状态都能通过公共引擎/复用 seam 观察到。"""

        async def run_case(
            error: BaseException | None,
            observation: object = ModelResponse(content="pong"),
            granted: bool = True,
            counter_initial: int = 1,
            blocked_status: "ProviderVerificationStatus | None" = None,
        ) -> ProviderVerificationStatus:
            dispatch, _ = self._dispatch(observation, error)
            _, counter = self._counter(counter_initial)
            granted_authorization = (
                authorization(authorized=True)
                if granted
                else ProviderVerificationAuthorization(
                    authorized=False,
                    blocked_status=blocked_status
                    or ProviderVerificationStatus.OPTED_OUT,
                    endpoint_aliases=frozenset({CHAT_ALIAS}),
                )
            )
            return (
                await verify_provider_capability_case(
                    authorization=granted_authorization,
                    binding=make_binding(),
                    dispatch=dispatch,
                    request_counter=counter,
                    now=NOW,
                )
            ).status

        observed = {
            await run_case(None, granted=False, counter_initial=0),
            await run_case(
                None,
                granted=False,
                counter_initial=0,
                blocked_status=ProviderVerificationStatus.CREDENTIALS_MISSING,
            ),
            await run_case(
                ModelFailure(
                    "TRANSIENT",
                    "provider_unavailable",
                    "provider request failed with HTTP 429",
                )
            ),
            await run_case(
                ModelFailure(
                    "PERMANENT",
                    "provider_request_failed",
                    "provider request failed with HTTP 401",
                )
            ),
            await run_case(ModelContractViolationError("violation")),
            await run_case(RuntimeError("harness")),
            await run_case(None),
        }
        # STALE 来自复用判定（见 ReuseTests）。
        stale = reuse_archived_provider_evidence(
            make_record(
                binding=make_binding(capability_case="streaming"),
                verified_at=NOW - timedelta(days=45),
            ),
            endpoint_alias=CHAT_ALIAS,
            capability_case="streaming",
            contract_fingerprint=CONTRACT_FINGERPRINT,
            adapter_configuration_fingerprint=CONFIGURATION_FINGERPRINT,
            artifact_digest=ARTIFACT_DIGEST,
            provider="openai-compatible",
            model_identity="gpt-verification-model",
            now=NOW,
        )
        assert stale is not None
        observed.add(stale.status)

        self.assertEqual(len(observed), 8)
        self.assertEqual(
            frozenset(observed),
            frozenset(PROVIDER_VERIFICATION_STATUSES),
        )
        self.assertEqual(len(PROVIDER_VERIFICATION_STATUSES), 8)


# ---------------------------------------------------------------------------
# 记录不变量：状态 ↔ 请求计数 ↔ 原因码
# ---------------------------------------------------------------------------


class ProviderCaseRecordInvariantTests(unittest.TestCase):
    def test_opted_out_and_stale_require_zero_requests(self) -> None:
        for status, reason in (
            (ProviderVerificationStatus.OPTED_OUT, "opted_out"),
            (ProviderVerificationStatus.STALE, "stale_evidence_expired"),
        ):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    make_record(status=status, request_count=1, reason_code=reason)

    def test_failure_states_allow_zero_or_more_requests(self) -> None:
        for status in (
            ProviderVerificationStatus.CREDENTIALS_MISSING,
            ProviderVerificationStatus.QUOTA_BLOCKED,
            ProviderVerificationStatus.PROVIDER_ERROR,
            ProviderVerificationStatus.CONTRACT_FAILURE,
            ProviderVerificationStatus.HARNESS_ERROR,
        ):
            with self.subTest(status=status):
                make_record(status=status, request_count=0)
                make_record(status=status, request_count=3)

    def test_pass_with_zero_requests_requires_reuse_reason(self) -> None:
        with self.assertRaises(ValueError):
            make_record(
                status=ProviderVerificationStatus.PASS,
                request_count=0,
                reason_code="dispatch_verified",
            )
        make_record(
            status=ProviderVerificationStatus.PASS,
            request_count=0,
            reason_code="reused_valid_evidence",
        )

    def test_reason_code_must_be_stable_identifier(self) -> None:
        with self.assertRaises(ValueError):
            make_record(reason_code="free text detail!")

    def test_naive_verified_at_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_record(verified_at=datetime(2026, 8, 27, 12, 0, 0))

    def test_binding_rejects_foreign_identity_shapes(self) -> None:
        with self.assertRaises(ValueError):
            make_binding(artifact_digest="not-a-digest")
        with self.assertRaises(ValueError):
            make_binding(contract_fingerprint="short")
        with self.assertRaises(ValueError):
            make_binding(adapter_configuration_fingerprint="")
        with self.assertRaises(ValueError):
            make_binding(alias="unknown-endpoint")
        with self.assertRaises(ValueError):
            make_binding(capability_case="unknown-case")


# ---------------------------------------------------------------------------
# 复用规则：fingerprint 一致 + 30 天窗口
# ---------------------------------------------------------------------------


class ProviderEvidenceReuseTests(unittest.TestCase):
    def _reuse(
        self,
        archived: ProviderCaseRecord,
        *,
        capability_case: str = "text_behavior",
        alias: str = CHAT_ALIAS,
        contract_fingerprint: str = CONTRACT_FINGERPRINT,
        adapter_configuration_fingerprint: str = CONFIGURATION_FINGERPRINT,
        now: datetime = NOW,
    ) -> ProviderCaseRecord | None:
        return reuse_archived_provider_evidence(
            archived,
            endpoint_alias=alias,
            capability_case=capability_case,
            contract_fingerprint=contract_fingerprint,
            adapter_configuration_fingerprint=adapter_configuration_fingerprint,
            artifact_digest=ARTIFACT_DIGEST,
            provider="openai-compatible",
            model_identity="gpt-verification-model",
            now=now,
        )

    def test_fresh_matching_evidence_is_reused_as_pass(self) -> None:
        archived = make_record(verified_at=NOW - timedelta(days=3))
        reused = self._reuse(archived)
        assert reused is not None
        self.assertIs(reused.status, ProviderVerificationStatus.PASS)
        self.assertEqual(reused.request_count, 0)
        self.assertEqual(reused.reason_code, "reused_valid_evidence")
        # 复用记录绑定当前 RC artifact，但保留原始验证时间。
        self.assertEqual(reused.binding.artifact_digest, ARTIFACT_DIGEST)
        self.assertEqual(reused.verified_at, archived.verified_at)

    def test_contract_fingerprint_drift_is_stale(self) -> None:
        archived = make_record(verified_at=NOW - timedelta(days=3))
        reused = self._reuse(archived, contract_fingerprint=OTHER_CONTRACT_FINGERPRINT)
        assert reused is not None
        self.assertIs(reused.status, ProviderVerificationStatus.STALE)
        self.assertEqual(
            reused.reason_code, "stale_contract_fingerprint_drift"
        )

    def test_adapter_configuration_drift_is_stale(self) -> None:
        archived = make_record(verified_at=NOW - timedelta(days=3))
        reused = self._reuse(
            archived,
            adapter_configuration_fingerprint=OTHER_CONFIGURATION_FINGERPRINT,
        )
        assert reused is not None
        self.assertIs(reused.status, ProviderVerificationStatus.STALE)
        self.assertEqual(
            reused.reason_code, "stale_adapter_configuration_drift"
        )

    def test_expired_evidence_is_stale(self) -> None:
        archived = make_record(verified_at=NOW - timedelta(days=31))
        reused = self._reuse(archived)
        assert reused is not None
        self.assertIs(reused.status, ProviderVerificationStatus.STALE)
        self.assertEqual(reused.reason_code, "stale_evidence_expired")

    def test_thirty_day_boundary_is_reusable(self) -> None:
        archived = make_record(verified_at=NOW - timedelta(days=30))
        reused = self._reuse(archived)
        assert reused is not None
        self.assertIs(reused.status, ProviderVerificationStatus.PASS)

    def test_other_alias_or_case_is_not_applicable(self) -> None:
        archived = make_record()
        self.assertIsNone(self._reuse(archived, alias=RESPONSES_ALIAS))
        self.assertIsNone(self._reuse(archived, capability_case="streaming"))

    def test_non_pass_archived_record_is_not_reusable(self) -> None:
        archived = make_record(
            status=ProviderVerificationStatus.PROVIDER_ERROR,
            reason_code="provider_error",
        )
        self.assertIsNone(self._reuse(archived))

    def test_reuse_rejects_naive_now(self) -> None:
        archived = make_record()
        with self.assertRaises(ValueError):
            self._reuse(archived, now=datetime(2026, 8, 27, 12, 0, 0))


# ---------------------------------------------------------------------------
# Report：RC 绑定、跨 wheel 拒绝、append-only revision
# ---------------------------------------------------------------------------


class ProviderVerificationReportTests(unittest.TestCase):
    def test_append_revision_rejects_other_wheel_results(self) -> None:
        foreign = make_record(
            binding=make_binding(artifact_digest=OTHER_ARTIFACT_DIGEST)
        )
        report = ProviderVerificationReport.create(
            report_id="report-1",
            manifest_digest="sha256:" + "1" * 64,
            artifact_digest=ARTIFACT_DIGEST,
            execution_id="execution-1",
            endpoint_aliases=frozenset({CHAT_ALIAS}),
        )
        with self.assertRaisesRegex(ValueError, "wheel"):
            report.append_revision(
                records=(foreign,),
                created_at=NOW,
                authorization=authorization(authorized=True),
                redaction_verified=True,
            )

    def test_append_revision_rejects_out_of_scope_alias(self) -> None:
        out_of_scope = make_record(
            binding=make_binding(alias=RESPONSES_ALIAS)
        )
        report = ProviderVerificationReport.create(
            report_id="report-1",
            manifest_digest="sha256:" + "1" * 64,
            artifact_digest=ARTIFACT_DIGEST,
            execution_id="execution-1",
            endpoint_aliases=frozenset({CHAT_ALIAS}),
        )
        with self.assertRaisesRegex(ValueError, "scope"):
            report.append_revision(
                records=(out_of_scope,),
                created_at=NOW,
                authorization=authorization(
                    authorized=True, aliases=frozenset({RESPONSES_ALIAS})
                ),
                redaction_verified=True,
            )

    def test_append_revision_rejects_duplicate_case(self) -> None:
        first = make_record()
        duplicate = make_record(
            binding=make_binding(capability_case="text_behavior"),
            request_count=2,
        )
        report = make_report()
        with self.assertRaisesRegex(ValueError, "distinct"):
            report.append_revision(
                records=(first, duplicate),
                created_at=NOW,
                authorization=authorization(authorized=True),
                redaction_verified=True,
            )

    def test_append_revision_requires_redaction_verification(self) -> None:
        report = make_report()
        with self.assertRaises(ValueError):
            report.append_revision(
                records=(make_record(),),
                created_at=NOW,
                authorization=authorization(authorized=True),
                redaction_verified=False,
            )

    def test_unauthorized_revision_only_records_blocked_statuses(self) -> None:
        report = make_report()
        with self.assertRaisesRegex(ValueError, "authorization"):
            report.append_revision(
                records=(make_record(),),
                created_at=NOW,
                authorization=authorization(authorized=False),
                redaction_verified=True,
            )
        opted_out = make_record(
            status=ProviderVerificationStatus.OPTED_OUT,
            request_count=0,
            reason_code="opted_out",
        )
        appended = report.append_revision(
            records=(opted_out,),
            created_at=NOW,
            authorization=authorization(authorized=False),
            redaction_verified=True,
        )
        self.assertEqual(appended.revisions[-1].records[0].status, opted_out.status)

    def test_authorized_revision_requires_authorized_scope(self) -> None:
        report = make_report(endpoint_aliases=frozenset(PROVIDER_ENDPOINT_ALIASES))
        record = make_record()
        with self.assertRaisesRegex(ValueError, "authorized"):
            report.append_revision(
                records=(record,),
                created_at=NOW,
                authorization=authorization(
                    authorized=True, aliases=frozenset({RESPONSES_ALIAS})
                ),
                redaction_verified=True,
            )

    def test_later_revision_appends_authorized_results(self) -> None:
        """先 OPTED_OUT、后取得授权：结果可追加且不覆盖既有 revision。"""
        report = make_report()
        opted_out = make_record(
            status=ProviderVerificationStatus.OPTED_OUT,
            request_count=0,
            reason_code="opted_out",
        )
        first = report.append_revision(
            records=(opted_out,),
            created_at=NOW,
            authorization=authorization(authorized=False),
            redaction_verified=True,
        )
        verified = make_record()
        second = first.append_revision(
            records=(verified,),
            created_at=NOW + timedelta(hours=1),
            authorization=authorization(authorized=True),
            redaction_verified=True,
        )
        self.assertEqual(
            [revision.revision for revision in second.revisions], [1, 2]
        )
        # append-only：第一个 revision 原样保留。
        self.assertEqual(first.revisions[-1].records[0].status, opted_out.status)
        latest = second.latest_case_records()
        self.assertEqual(
            latest[(CHAT_ALIAS, "text_behavior")].status,
            ProviderVerificationStatus.PASS,
        )

    def test_latest_case_records_and_missing_cases(self) -> None:
        report = make_report(
            endpoint_aliases=frozenset(PROVIDER_ENDPOINT_ALIASES),
            records=(make_record(),),
        )
        self.assertEqual(
            report.missing_cases(),
            tuple(
                pair
                for pair in provider_capability_matrix(
                    frozenset(PROVIDER_ENDPOINT_ALIASES)
                )
                if pair != (CHAT_ALIAS, "text_behavior")
            ),
        )

    def test_content_digest_verifies_and_detects_tampering(self) -> None:
        report = make_report(records=(make_record(),))
        report.verify()
        tampered = report.model_copy(
            update={"artifact_digest": OTHER_ARTIFACT_DIGEST}
        )
        with self.assertRaises(ValueError):
            tampered.verify()

    def test_report_rejects_non_contiguous_revisions(self) -> None:
        record = make_record()
        revision = report_revision_with(records=(record,), revision=3)
        with self.assertRaises(ValueError):
            ProviderVerificationReport(
                report_id="report-1",
                manifest_digest="sha256:" + "1" * 64,
                artifact_digest=ARTIFACT_DIGEST,
                execution_id="execution-1",
                endpoint_aliases=frozenset({CHAT_ALIAS}),
                revisions=(revision,),
                content_digest="sha256:" + "2" * 64,
            )


def report_revision_with(
    *, records: tuple[ProviderCaseRecord, ...], revision: int
):
    """直接构造带指定 revision 号的 revision（绕过 append API 做校验测试）。"""
    from m_agent.testing import ProviderReportRevision

    return ProviderReportRevision(
        revision=revision,
        created_at=NOW,
        endpoint_scope=frozenset({CHAT_ALIAS}),
        request_count=sum(record.request_count for record in records),
        records=records,
        redaction_verified=True,
    )


# ---------------------------------------------------------------------------
# Pack Execution 语义：append-only，不改写正式执行
# ---------------------------------------------------------------------------


def make_rc_manifest(
    *,
    artifact_digest: str = ARTIFACT_DIGEST,
    evidence_level: EvidenceLevel = EvidenceLevel.CONTRACT,
) -> AcceptanceManifest:
    check = AcceptanceCheck(
        check_id="provider.qualification",
        scenario="provider-qualification",
        public_seam="m_agent.testing verify-provider",
        owner="Release Owner",
        positive_check="authorized provider matrix verified",
        negative_check="unauthorized run records zero requests",
        authoritative_evidence="provider_qualification_authoritative",
        independent_evidence="provider_qualification_independent",
        milestone="0.5",
        non_claim="not a production provider guarantee",
        evidence_level=evidence_level,
    )
    return AcceptanceManifest(
        pack_version="provider-qualification-v1",
        profile="provider-qualification-0-5",
        source_commit="a" * 40,
        artifact_digest=artifact_digest,
        sdist_digest="sha256:" + "3" * 64,
        fixture_digest="sha256:" + "4" * 64,
        environment={"distribution": "m-agent", "version": "0.5.0"},
        scenarios=("provider-qualification",),
        required_checks=(check,),
    )


class PackExecutionSemanticsTests(unittest.TestCase):
    def test_attach_returns_execution_unchanged(self) -> None:
        manifest = make_rc_manifest()
        execution = (
            PackExecution.create(manifest, execution_id="execution-1")
            .start(manifest)
        )
        report = make_report(
            manifest_digest=manifest.digest,
            execution_id="execution-1",
            records=(make_record(),),
        )
        appended = report.append_revision(
            records=(make_record(binding=make_binding(capability_case="streaming")),),
            created_at=NOW,
            authorization=authorization(authorized=True),
            redaction_verified=True,
        )
        attached = attach_provider_report(appended, execution)
        self.assertEqual(attached, execution)
        self.assertIs(attached.status, PackExecutionStatus.RUNNING)

    def test_attach_rejects_identity_mismatch(self) -> None:
        manifest = make_rc_manifest()
        execution = (
            PackExecution.create(manifest, execution_id="execution-1")
            .start(manifest)
        )
        # 同 execution id，但报告绑定的是另一个 Manifest。
        foreign_manifest_report = make_report(records=(make_record(),))
        with self.assertRaises(ProviderReportRewriteError):
            attach_provider_report(foreign_manifest_report, execution)
        # 同 Manifest，但报告属于另一个 execution。
        other_execution = (
            PackExecution.create(manifest, execution_id="execution-2")
            .start(manifest)
        )
        foreign_execution_report = make_report(
            manifest_digest=manifest.digest,
            execution_id="execution-1",
            records=(make_record(),),
        )
        with self.assertRaises(ProviderReportRewriteError):
            attach_provider_report(foreign_execution_report, other_execution)

    def test_provider_check_level_never_yields_passed_execution(self) -> None:
        """PROVIDER 证据级别只能得到诚实的 INCOMPLETE，绝不能伪造 PASS。"""
        manifest = make_rc_manifest(evidence_level=EvidenceLevel.PROVIDER)
        execution = (
            PackExecution.create(manifest, execution_id="execution-1")
            .start(manifest)
        )
        result = AcceptanceCheckResult(
            check_id="provider.qualification",
            status=AcceptanceCheckStatus.PASS,
            evidence_level=EvidenceLevel.PROVIDER,
            reason_code="provider_verified",
            evidence_digest="sha256:" + "5" * 64,
        )
        completed = execution.complete(manifest, (result,))
        self.assertIs(completed.status, PackExecutionStatus.INCOMPLETE)
        self.assertEqual(completed.exit_code, EXIT_INCOMPLETE)

    def test_new_rc_required_for_fixed_release_acceptance(self) -> None:
        manifest = make_rc_manifest()
        execution = (
            PackExecution.create(manifest, execution_id="execution-1")
            .start(manifest)
            .complete(
                manifest,
                (
                    AcceptanceCheckResult(
                        check_id="provider.qualification",
                        status=AcceptanceCheckStatus.FAIL,
                        evidence_level=EvidenceLevel.CONTRACT,
                        reason_code="subject_failed",
                        evidence_digest="sha256:" + "6" * 64,
                    ),
                ),
            )
        )
        with self.assertRaisesRegex(ValueError, "new release candidate"):
            require_new_rc_release_acceptance(
                execution, manifest, execution_id="execution-2"
            )
        fixed_manifest = make_rc_manifest(
            artifact_digest="sha256:" + "c" * 64
        )
        fresh = require_new_rc_release_acceptance(
            execution, fixed_manifest, execution_id="execution-2"
        )
        self.assertIs(fresh.status, PackExecutionStatus.CREATED)
        self.assertIsNone(fresh.exit_code)
        # 新 RC 必须完整重跑 required profile：空结果只能得到诚实的
        # INCOMPLETE，而不是继承或伪造任何结论。
        rerun = fresh.complete(fixed_manifest, ())
        self.assertIs(rerun.status, PackExecutionStatus.INCOMPLETE)
        self.assertEqual(rerun.exit_code, EXIT_INCOMPLETE)

    def test_terminal_execution_cannot_be_rewritten(self) -> None:
        manifest = make_rc_manifest()
        execution = (
            PackExecution.create(manifest, execution_id="execution-1")
            .start(manifest)
            .complete(
                manifest,
                (
                    AcceptanceCheckResult(
                        check_id="provider.qualification",
                        status=AcceptanceCheckStatus.PASS,
                        evidence_level=EvidenceLevel.CONTRACT,
                        reason_code="verified",
                        evidence_digest="sha256:" + "7" * 64,
                    ),
                ),
            )
        )
        with self.assertRaises(ValueError):
            execution.complete(
                manifest,
                (
                    AcceptanceCheckResult(
                        check_id="provider.qualification",
                        status=AcceptanceCheckStatus.PASS,
                        evidence_level=EvidenceLevel.CONTRACT,
                        reason_code="verified",
                        evidence_digest="sha256:" + "7" * 64,
                    ),
                ),
            )


# ---------------------------------------------------------------------------
# Catalog verified 门禁：阻塞但不跨层误报
# ---------------------------------------------------------------------------


class VerifiedRecommendationGateTests(unittest.TestCase):
    def _full_matrix_records(
        self,
        *,
        alias: str = CHAT_ALIAS,
        verified_at: datetime = NOW - timedelta(days=2),
        contract_fingerprint: str = CONTRACT_FINGERPRINT,
        adapter_configuration_fingerprint: str = CONFIGURATION_FINGERPRINT,
    ) -> tuple[ProviderCaseRecord, ...]:
        return tuple(
            make_record(
                binding=make_binding(
                    alias=alias,
                    capability_case=case,
                    contract_fingerprint=contract_fingerprint,
                    adapter_configuration_fingerprint=(
                        adapter_configuration_fingerprint
                    ),
                ),
                verified_at=verified_at,
            )
            for case in PROVIDER_CAPABILITY_CASES
        )

    def _gate(
        self,
        records: tuple[ProviderCaseRecord, ...],
        *,
        alias: str = CHAT_ALIAS,
        contract_fingerprint: str = CONTRACT_FINGERPRINT,
        adapter_configuration_fingerprint: str = CONFIGURATION_FINGERPRINT,
        now: datetime = NOW,
    ) -> VerifiedRecommendationStatus:
        return verified_recommendation_status(
            endpoint_alias=alias,
            contract_fingerprint=contract_fingerprint,
            adapter_configuration_fingerprint=adapter_configuration_fingerprint,
            records=records,
            now=now,
        )

    def test_missing_provider_evidence_blocks_verified_state(self) -> None:
        self.assertIs(
            self._gate(()), VerifiedRecommendationStatus.BLOCKED_MISSING
        )
        partial = self._full_matrix_records()[:3]
        self.assertIs(self._gate(partial), VerifiedRecommendationStatus.BLOCKED_MISSING)

    def test_stale_provider_evidence_blocks_verified_state(self) -> None:
        expired = self._full_matrix_records(verified_at=NOW - timedelta(days=45))
        self.assertIs(self._gate(expired), VerifiedRecommendationStatus.BLOCKED_STALE)

    def test_fingerprint_drift_blocks_verified_state(self) -> None:
        drifted = self._full_matrix_records(
            contract_fingerprint=OTHER_CONTRACT_FINGERPRINT
        )
        self.assertIs(self._gate(drifted), VerifiedRecommendationStatus.BLOCKED_STALE)

    def test_alias_drift_blocks_verified_state(self) -> None:
        drifted_alias = self._full_matrix_records(alias=RESPONSES_ALIAS)
        self.assertIs(
            self._gate(drifted_alias), VerifiedRecommendationStatus.BLOCKED_STALE
        )

    def test_non_pass_records_block_verified_state(self) -> None:
        failed = tuple(
            make_record(
                binding=make_binding(capability_case=case),
                status=ProviderVerificationStatus.PROVIDER_ERROR,
                request_count=1,
                reason_code="provider_error",
            )
            for case in PROVIDER_CAPABILITY_CASES
        )
        self.assertIs(self._gate(failed), VerifiedRecommendationStatus.BLOCKED_STALE)

    def test_fresh_pass_matrix_verifies(self) -> None:
        records = self._full_matrix_records()
        self.assertIs(self._gate(records), VerifiedRecommendationStatus.VERIFIED)

    def test_gate_never_reports_contract_or_host_failure(self) -> None:
        """PROVIDER 门禁只有 PROVIDER 域状态，绝不冒充 CONTRACT/HOST 结论。"""
        allowed = {
            VerifiedRecommendationStatus.VERIFIED,
            VerifiedRecommendationStatus.BLOCKED_STALE,
            VerifiedRecommendationStatus.BLOCKED_MISSING,
        }
        for records in (
            (),
            self._full_matrix_records(),
            self._full_matrix_records(verified_at=NOW - timedelta(days=99)),
            self._full_matrix_records(contract_fingerprint=OTHER_CONTRACT_FINGERPRINT),
            self._full_matrix_records(alias=RESPONSES_ALIAS),
        ):
            with self.subTest(records=len(records)):
                self.assertIn(self._gate(records), allowed)

    def test_missing_provider_evidence_does_not_break_offline_routing(self) -> None:
        """PROVIDER 缺失阻塞 verified 状态，但离线路由照常 SELECTED。"""
        variant = make_variant("variant-verified", make_contract("contract-v"))
        catalog = make_catalog(make_entry(variant))
        router_result = ModelRouter().select(
            catalog=catalog,
            policy=make_policy(),
            evidence=make_evidence((variant,)),
            as_of=NOW,
        )
        self.assertIsNotNone(router_result.decision)
        gate = verified_recommendation_status(
            endpoint_alias=CHAT_ALIAS,
            contract_fingerprint=variant.primary_contract().fingerprint,
            adapter_configuration_fingerprint=CONFIGURATION_FINGERPRINT,
            records=(),
            now=NOW,
        )
        self.assertIs(gate, VerifiedRecommendationStatus.BLOCKED_MISSING)
        # Router 的原因码里没有任何 PROVIDER 误报。
        self.assertNotIn("PROVIDER", router_result.reason_code or "")
        for warning in router_result.warnings:
            self.assertNotIn("PROVIDER", warning.code)


# ---------------------------------------------------------------------------
# 脱敏：credential canary / 敏感字段 / endpoint secret
# ---------------------------------------------------------------------------


class ProviderEvidenceRedactionTests(unittest.TestCase):
    def test_failed_dispatch_error_text_never_enters_report(self) -> None:
        failure = ModelFailure(
            "PERMANENT",
            "provider_request_failed",
            f"provider request failed with HTTP 401 ({CREDENTIAL_CANARY})",
        )

        async def dispatch() -> object:
            raise failure

        record = asyncio.run(
            verify_provider_capability_case(
                authorization=authorization(authorized=True),
                binding=make_binding(),
                dispatch=dispatch,
                request_counter=lambda: 1,
                now=NOW,
            )
        )
        report = make_report(records=(record,))
        serialized = report.model_dump_json()
        self.assertNotIn(CREDENTIAL_CANARY, serialized)
        assert_provider_evidence_sanitized(
            json.loads(serialized), canary_values=(CREDENTIAL_CANARY,)
        )

    def test_redaction_findings_detect_canary_value(self) -> None:
        findings = redaction_findings(
            {"note": f"leaked {CREDENTIAL_CANARY}"},
            canary_values=(CREDENTIAL_CANARY,),
        )
        self.assertIn("credential_canary_present", findings)

    def test_redaction_findings_detect_sensitive_field_name(self) -> None:
        findings = redaction_findings(
            {"authorization": "Bearer abc"}, canary_values=()
        )
        self.assertIn("sensitive_field_present", findings)

    def test_redaction_findings_detect_endpoint_secret(self) -> None:
        findings = redaction_findings(
            {"endpoint": "https://user:secret@example.com/v1"}, canary_values=()
        )
        self.assertIn("endpoint_secret_present", findings)

    def test_clean_report_passes_scan(self) -> None:
        report = make_report(records=(make_record(),))
        payload = json.loads(report.model_dump_json())
        self.assertEqual(
            redaction_findings(payload, canary_values=(CREDENTIAL_CANARY,)), []
        )
        assert_provider_evidence_sanitized(
            payload, canary_values=(CREDENTIAL_CANARY,)
        )

    def test_assert_sanitized_raises_on_findings(self) -> None:
        with self.assertRaises(ValueError):
            assert_provider_evidence_sanitized(
                {"api_key": CREDENTIAL_CANARY},
                canary_values=(CREDENTIAL_CANARY,),
            )


# ---------------------------------------------------------------------------
# 零 live request：import / Router discovery / 命令入口
# ---------------------------------------------------------------------------


class ZeroLiveRequestTests(unittest.TestCase):
    def test_import_with_ambient_credentials_makes_no_requests(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import m_agent.testing, m_agent.companion.routing",
            ],
            env={
                "HOME": os.environ.get("HOME", "/tmp"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                PROVIDER_LIVE_OPT_IN_ENV: "0",
                "M_AGENT_OPENAI_API_KEY": CREDENTIAL_CANARY,
                "OPENAI_API_KEY": CREDENTIAL_CANARY,
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 0)

    def test_router_discovery_makes_zero_transport_requests(self) -> None:
        try:
            httpx = __import__("httpx")
        except ImportError:
            self.skipTest("m-agent provider extra not installed")
        from m_agent.adapters.provider import (
            CHAT_COMPLETIONS_CAPABILITIES,
            ChatCompletionsModelAdapter,
        )
        from m_agent.runtime import ModelLimits, RevisionStability

        calls: list[int] = []

        def counting_transport(request):  # noqa: ANN001
            calls.append(1)
            return httpx.Response(200, json={"choices": []})

        adapter = ChatCompletionsModelAdapter(
            base_url="https://discovery.invalid/v1",
        )
        adapter._transport = httpx.MockTransport(counting_transport)
        contract = make_contract("contract-discovery").model_copy(
            update={
                "capabilities": CHAT_COMPLETIONS_CAPABILITIES,
                "revision_stability": RevisionStability.PROVIDER_ALIAS,
                "limits": ModelLimits(
                    context_window_tokens=128_000, max_output_tokens=16_000
                ),
            }
        )
        variant = make_variant("variant-discovery", contract)
        router_result = ModelRouter().select(
            catalog=make_catalog(make_entry(variant)),
            policy=make_policy(),
            evidence=make_evidence((variant,)),
            as_of=NOW,
        )
        self.assertIsNotNone(router_result.decision)
        self.assertEqual(calls, [])
        self.assertEqual(adapter.requests, [])
        asyncio.run(adapter.aclose())


# ---------------------------------------------------------------------------
# verify-provider 命令：独立入口、默认零 live request
# ---------------------------------------------------------------------------


class VerifyProviderCommandTests(unittest.TestCase):
    def _arguments(self, output_dir: Path, manifest_path: Path, *extra: str):
        from m_agent.testing import __main__ as testing_cli

        parser = testing_cli._parser()
        return parser.parse_args(
            [
                "verify-provider",
                "--manifest",
                str(manifest_path),
                "--wheel",
                str(output_dir / "rc.whl"),
                "--sdist",
                str(output_dir / "rc.tar.gz"),
                "--execution-id",
                "execution-1",
                "--output-dir",
                str(output_dir / "reports"),
                *extra,
            ]
        )

    def _run_cli(
        self, *, allow_live: bool, environ: dict[str, str]
    ) -> dict[str, Any]:
        from m_agent.testing import __main__ as testing_cli

        manifest = make_rc_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "rc-manifest.json"
            manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
            extra = ("--allow-live",) if allow_live else ()
            arguments = self._arguments(root, manifest_path, *extra)
            with patch.object(
                testing_cli, "validate_installed_identity"
            ), patch.dict(os.environ, environ, clear=False):
                testing_cli._verify_provider(arguments)
            reports = sorted((root / "reports").glob("*.json"))
            self.assertEqual(len(reports), 1)
            return json.loads(reports[0].read_text(encoding="utf-8"))

    def test_cli_without_allow_live_is_opted_out_with_zero_requests(self) -> None:
        """CI 场景：环境里恰好有凭证 + opt-in，但命令未显式 --allow-live。"""
        payload = self._run_cli(
            allow_live=False,
            environ={
                PROVIDER_LIVE_OPT_IN_ENV: "1",
                "M_AGENT_OPENAI_API_KEY": CREDENTIAL_CANARY,
            },
        )
        self.assertEqual(len(payload["revisions"]), 1)
        records = payload["revisions"][0]["records"]
        matrix = provider_capability_matrix()
        self.assertEqual(len(records), len(matrix))
        for record in records:
            self.assertEqual(record["status"], "OPTED_OUT")
            self.assertEqual(record["request_count"], 0)
            self.assertEqual(record["reason_code"], "opted_out")
            self.assertEqual(record["binding"]["artifact_digest"], ARTIFACT_DIGEST)
        self.assertEqual(payload["revisions"][0]["request_count"], 0)
        self.assertEqual(payload["artifact_digest"], ARTIFACT_DIGEST)
        self.assertNotIn(CREDENTIAL_CANARY, json.dumps(payload))

    def test_cli_with_allow_live_but_no_credentials_reports_missing(self) -> None:
        payload = self._run_cli(
            allow_live=True,
            environ={
                PROVIDER_LIVE_OPT_IN_ENV: "1",
                "M_AGENT_OPENAI_API_KEY": "",
                "OPENAI_API_KEY": "",
            },
        )
        records = payload["revisions"][0]["records"]
        self.assertEqual(len(records), len(provider_capability_matrix()))
        for record in records:
            self.assertEqual(record["status"], "CREDENTIALS_MISSING")
            self.assertEqual(record["request_count"], 0)

    def test_cli_scopes_to_requested_endpoints(self) -> None:
        from m_agent.testing import __main__ as testing_cli

        manifest = make_rc_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "rc-manifest.json"
            manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
            arguments = self._arguments(
                root, manifest_path, "--endpoint", RESPONSES_ALIAS
            )
            with patch.object(
                testing_cli, "validate_installed_identity"
            ), patch.dict(os.environ, {}, clear=False):
                testing_cli._verify_provider(arguments)
            payload = json.loads(
                next((root / "reports").glob("*.json")).read_text(encoding="utf-8")
            )
        aliases = {
            record["binding"]["endpoint_alias"]
            for record in payload["revisions"][0]["records"]
        }
        self.assertEqual(aliases, {RESPONSES_ALIAS})

    def test_parser_wires_verify_provider_command(self) -> None:
        from m_agent.testing import __main__ as testing_cli

        parser = testing_cli._parser()
        arguments = parser.parse_args(
            [
                "verify-provider",
                "--manifest",
                "m.json",
                "--wheel",
                "w.whl",
                "--sdist",
                "s.tar.gz",
                "--execution-id",
                "e1",
                "--output-dir",
                "out",
            ]
        )
        self.assertEqual(arguments.command, "verify-provider")
        self.assertFalse(arguments.allow_live)
        self.assertIs(arguments.handler, testing_cli._verify_provider)

    def test_parser_rejects_unknown_endpoint(self) -> None:
        from m_agent.testing import __main__ as testing_cli

        parser = testing_cli._parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "verify-provider",
                    "--manifest",
                    "m.json",
                    "--wheel",
                    "w.whl",
                    "--sdist",
                    "s.tar.gz",
                    "--execution-id",
                    "e1",
                    "--output-dir",
                    "out",
                    "--endpoint",
                    "bogus-endpoint",
                ]
            )

    def test_cli_second_invocation_appends_revision_only(self) -> None:
        """同一 execution 的第二次调用只追加 revision，不改写历史。"""
        from m_agent.testing import __main__ as testing_cli

        manifest = make_rc_manifest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "rc-manifest.json"
            manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
            arguments = self._arguments(root, manifest_path)
            with patch.object(
                testing_cli, "validate_installed_identity"
            ), patch.dict(os.environ, {}, clear=False):
                testing_cli._verify_provider(arguments)
                exit_code = testing_cli._verify_provider(arguments)
            self.assertEqual(exit_code, EXIT_INCOMPLETE)
            reports = sorted((root / "reports").glob("*.json"))
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(
                [revision["revision"] for revision in payload["revisions"]],
                [1, 2],
            )
            for revision in payload["revisions"]:
                self.assertEqual(revision["request_count"], 0)
                for record in revision["records"]:
                    self.assertEqual(record["status"], "OPTED_OUT")
                    self.assertEqual(record["request_count"], 0)
                    self.assertEqual(
                        record["binding"]["artifact_digest"], ARTIFACT_DIGEST
                    )

    def test_cli_rejects_report_from_another_release_candidate(self) -> None:
        """同一 execution id 下其他 wheel 的报告不能被附加。"""
        from m_agent.testing import __main__ as testing_cli

        manifest = make_rc_manifest()
        other = make_rc_manifest(artifact_digest=OTHER_ARTIFACT_DIGEST)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "rc-manifest.json"
            manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
            with patch.object(
                testing_cli, "validate_installed_identity"
            ), patch.dict(os.environ, {}, clear=False):
                testing_cli._verify_provider(self._arguments(root, manifest_path))
            other_path = root / "other-manifest.json"
            other_path.write_text(other.model_dump_json(), encoding="utf-8")
            with patch.object(
                testing_cli, "validate_installed_identity"
            ), patch.dict(os.environ, {}, clear=False):
                with self.assertRaises(ProviderReportRewriteError):
                    testing_cli._verify_provider(
                        self._arguments(root, other_path)
                    )
            # 拒绝后不产生新报告，既有报告原样保留。
            reports = sorted((root / "reports").glob("*.json"))
            self.assertEqual(len(reports), 1)
            payload = json.loads(reports[0].read_text(encoding="utf-8"))
            self.assertEqual(len(payload["revisions"]), 1)
            self.assertEqual(payload["artifact_digest"], ARTIFACT_DIGEST)


if __name__ == "__main__":
    unittest.main()
