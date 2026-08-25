"""Foundation-release 0.4 profile probes and observers (Ticket 16).

本模块提供 0.4 发布 profile 的两段式 HOST 证据设施：

- **隔离探针**（``SESSION_HOST_PROBE`` / ``CONTEXT_HOST_PROBE``）：在
  ``python -I`` 隔离进程中从安装的 wheel 运行完整 Reference Scenario
  （session 场景含真实子进程崩溃窗口与 reopen），并打印一份最小 JSON
  观察。session 探针同时以公开 ``SessionStore`` API 驱动 InMemory 与
  SQLite 实现的同一操作序列，验证两个官方实现共享同一行为契约。
- **观察器**（:func:`observe_isolated_scenario_probe`）：父进程运行探针
  并按冻结期望分类——探针进程崩溃 / 输出不可解析 / schema 违约是
  ``ERROR``（harness 或环境问题），检查失败或观察值偏离是 ``FAIL``
  （被测 subject 在 wheel 下确实不满足），全部一致才是 ``PASS``。

所有判定只依赖探针的公开 JSON 观察；本模块绝不把 CONTRACT/HOST 结果
表述为 live 或 FIELD 结论，也不发起任何网络请求。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from typing import Mapping, cast

from ._pack import AcceptanceCheckResult, AcceptanceCheckStatus, EvidenceLevel
from ._subprocess import isolated_subprocess_environment

_PROBE_TIMEOUT_SECONDS = 900.0

# 冻结期望：探针必须报告的 check 集与最小观察值。观察键集必须精确匹配；
# 值偏离即 subject FAIL，键集偏离即 harness ERROR。
SESSION_HOST_EXPECTATION: Mapping[str, object] = {
    "scenario": "session-conversation",
    "check_ids": (
        "session.conversation.recovery-windows",
        "session.conversation.claim-no-ttl",
        "session.conversation.payload-protection",
        "session.conversation.scope-isolation",
        "session.conversation.mutation",
    ),
    "observation": {
        "recovery_window_repetitions": 9,
        "recovery_windows_clean": True,
        "recovery_window_outcomes_correct": True,
        "claim_no_ttl_observed": True,
        "payload_protection_observed": True,
        "scope_isolation_observed": True,
        "mutation_detected": True,
        "store_contract_equivalent": True,
    },
}

CONTEXT_HOST_EXPECTATION: Mapping[str, object] = {
    "scenario": "context-budget-compression",
    "check_ids": (
        "context.compression.plan-order",
        "context.compression.frame-checkpoints",
        "context.compression.hard-budget",
        "context.compression.protected-channels",
        "context.compression.no-recursion",
        "context.compression.mutation",
    ),
    "observation": {
        "scenario_runs_clean": True,
        "hard_budget_observed": True,
        "protected_channels_observed": True,
        "no_recursion_observed": True,
        "mutation_detected": True,
    },
}

_EXPECTATIONS_BY_CHECK_ID = {
    "session.conversation.host-wheel": SESSION_HOST_EXPECTATION,
    "context.compression.host-wheel": CONTEXT_HOST_EXPECTATION,
}

# 探针在隔离 wheel 进程内运行：只用公开 m_agent.testing 入口。
_SESSION_SCENARIO_PROBE = """
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path

from m_agent.testing import run_session_conversation


async def store_contract_equivalent():
    from m_agent.companion import (
        InMemorySessionStore,
        SessionScope,
        SessionTurn,
        SQLiteSessionStore,
    )

    scope = SessionScope(token="host-store-contract")
    session_id = "host-store-contract-1"
    with tempfile.TemporaryDirectory() as temporary:
        sqlite_store = SQLiteSessionStore(Path(temporary) / "contract.sqlite3")
        in_memory = InMemorySessionStore()
        try:
            observations = []
            for store in (in_memory, sqlite_store):
                await store.create_session(scope, session_id)
                snapshot = await store.read_snapshot(scope, session_id)
                await store.claim_run(
                    scope, session_id, "host-run-1", expected_version=snapshot.version
                )
                claim = await store.get_claim(scope, session_id)
                turn = SessionTurn(
                    turn_id="host-turn-1",
                    session_id=session_id,
                    run_id="host-run-1",
                    definition_id="host-definition",
                    definition_version="1.0",
                    user_input="host input",
                    assistant_output="host output",
                    created_at=datetime(2026, 8, 25, 12, 0, 0),
                )
                committed = await store.commit_turn(
                    scope, session_id, turn, expected_version=0
                )
                cleared = await store.get_claim(scope, session_id)
                final = await store.read_snapshot(scope, session_id)
                observations.append({
                    "created_version": snapshot.version,
                    "claim_run_id": claim.run_id if claim else None,
                    "commit_status": committed.status.value,
                    "claim_cleared": cleared is None,
                    "final_version": final.version,
                    "turn_run_ids": [item.run_id for item in final.turns],
                })
            return observations[0] == observations[1]
        finally:
            sqlite_store.close()


checks, evidence_view, _ = run_session_conversation()
print(json.dumps({
    "scenario": "session-conversation",
    "checks": {result.check_id: result.status.value for result in checks},
    "observation": {
        "recovery_window_repetitions": evidence_view["recovery_window_repetitions"],
        "recovery_windows_clean": evidence_view["recovery_windows_clean"],
        "recovery_window_outcomes_correct": evidence_view[
            "recovery_window_outcomes_correct"
        ],
        "claim_no_ttl_observed": evidence_view["claim_no_ttl_observed"],
        "payload_protection_observed": evidence_view["payload_protection_observed"],
        "scope_isolation_observed": evidence_view["scope_isolation_observed"],
        "mutation_detected": evidence_view["mutation_detected"],
        "store_contract_equivalent": asyncio.run(store_contract_equivalent()),
    },
}, sort_keys=True))
"""

_CONTEXT_SCENARIO_PROBE = """
import json

from m_agent.testing import run_context_budget_compression

checks, evidence_view, _ = run_context_budget_compression()
print(json.dumps({
    "scenario": "context-budget-compression",
    "checks": {result.check_id: result.status.value for result in checks},
    "observation": {
        "scenario_runs_clean": evidence_view["scenario_runs_clean"],
        "hard_budget_observed": evidence_view["hard_budget_observed"],
        "protected_channels_observed": evidence_view[
            "protected_channels_observed"
        ],
        "no_recursion_observed": evidence_view["no_recursion_observed"],
        "mutation_detected": evidence_view["mutation_detected"],
    },
}, sort_keys=True))
"""

SESSION_HOST_PROBE = _SESSION_SCENARIO_PROBE
CONTEXT_HOST_PROBE = _CONTEXT_SCENARIO_PROBE

_VALID_CHECK_STATUSES = frozenset(
    status.value for status in AcceptanceCheckStatus
)


def _digest(payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def observe_isolated_scenario_probe(
    *,
    probe_source: str,
    check_id: str,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> tuple[AcceptanceCheckResult, dict[str, str | int | bool]]:
    """Run one scenario probe in an isolated wheel process and judge it.

    返回 ``(HOST AcceptanceCheckResult, evidence)``。分类规则：

    - 探针进程非零退出、超时、stdout 非单个 JSON 对象、键集 / scenario /
      check 集与冻结期望不符、check 值不是合法状态、观察键集不符 →
      ``ERROR``（harness / 环境问题，退出码 3 语义）；
    - 一切 schema 正确但存在非 PASS check 或观察值偏离期望 → ``FAIL``
      （被测 subject 在安装 wheel 下不满足已声明契约）；
    - 其余 → ``PASS``。
    """
    expectation = _EXPECTATIONS_BY_CHECK_ID.get(check_id)
    if expectation is None:
        raise ValueError(f"no frozen HOST expectation for {check_id!r}")
    expected_checks = cast(tuple[str, ...], expectation["check_ids"])
    expected_observation = dict(cast(dict[str, object], expectation["observation"]))
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", probe_source],
            env=isolated_subprocess_environment(),
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    stdout_digest = "sha256:" + hashlib.sha256(
        (completed.stdout if completed is not None else "").encode("utf-8")
    ).hexdigest()
    evidence: dict[str, str | int | bool] = {
        "probe_process_observed": completed is not None and completed.returncode == 0,
        "probe_returncode": completed.returncode if completed is not None else -1,
        "probe_stdout_digest": stdout_digest,
    }

    def result(
        status: AcceptanceCheckStatus, reason_code: str, digest: str
    ) -> tuple[AcceptanceCheckResult, dict[str, str | int | bool]]:
        return (
            AcceptanceCheckResult(
                check_id=check_id,
                status=status,
                evidence_level=EvidenceLevel.HOST,
                reason_code=reason_code,
                evidence_digest=digest,
            ),
            evidence,
        )

    def error_digest(reason: str) -> str:
        return _digest(
            {
                "check_id": check_id,
                "reason": reason,
                "returncode": evidence["probe_returncode"],
                "stdout_digest": stdout_digest,
            }
        )

    if completed is None or completed.returncode != 0:
        return result(
            AcceptanceCheckStatus.ERROR,
            "isolated_probe_process_error",
            error_digest("process"),
        )
    try:
        observed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return result(
            AcceptanceCheckStatus.ERROR,
            "isolated_probe_process_error",
            error_digest("stdout_json"),
        )
    if (
        not isinstance(observed, dict)
        or set(observed) != {"scenario", "checks", "observation"}
        or observed.get("scenario") != expectation["scenario"]
        or not isinstance(observed.get("checks"), dict)
        or not isinstance(observed.get("observation"), dict)
    ):
        return result(
            AcceptanceCheckStatus.ERROR,
            "isolated_probe_schema_mismatch",
            error_digest("payload_shape"),
        )
    checks = observed["checks"]
    if set(checks) != set(expected_checks) or any(
        value not in _VALID_CHECK_STATUSES for value in checks.values()
    ):
        return result(
            AcceptanceCheckStatus.ERROR,
            "isolated_probe_schema_mismatch",
            error_digest("check_set"),
        )
    observation = observed["observation"]
    if set(observation) != set(expected_observation):
        return result(
            AcceptanceCheckStatus.ERROR,
            "isolated_probe_schema_mismatch",
            error_digest("observation_keys"),
        )
    authoritative_digest = _digest(
        {"scenario": observed["scenario"], "checks": checks, "observation": observation}
    )
    if any(value != "PASS" for value in checks.values()):
        return result(
            AcceptanceCheckStatus.FAIL,
            "isolated_wheel_scenario_failed",
            authoritative_digest,
        )
    if observation != expected_observation:
        return result(
            AcceptanceCheckStatus.FAIL,
            "isolated_wheel_observation_mismatch",
            authoritative_digest,
        )
    return result(
        AcceptanceCheckStatus.PASS,
        "isolated_wheel_scenario_observed",
        authoritative_digest,
    )
