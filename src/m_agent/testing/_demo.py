"""从已通过的 Scenario Evidence Bundles 重放发布演示（Ticket 22）。

演示脚本绝不在现场重新执行被测 subject：完整版（12–15 分钟）与短版
（3 分钟 recovery/report）都从**已验证的预生成 Bundle** 逐字重放——
每个 Bundle 在渲染前重新验证内容 digest 与全部完整性断言，执行状态
必须是 ``PASSED``，所有 Bundle 必须属于同一个 release candidate 与同
一次 Pack Execution。篡改的 Bundle 直接 ``BundleIntegrityError``，失败
或不完整的执行拒绝渲染，预生成证据在讲稿中逐段明确标注。

演示讲稿声明 PROVIDER 层（live provider）证据不在重放范围内——这是
一场 ``NOT a live provider`` 的离线证据重放，不是性能或生产演示。
"""

from __future__ import annotations

from collections.abc import Iterable

from ._pack import (
    DURABLE_EFFECTS_SCENARIO,
    BundleIntegrityError,
    PackExecutionStatus,
    ScenarioEvidenceBundle,
)

__all__ = ["PRE_GENERATED_EVIDENCE_LABEL", "render_release_demo"]

#: 演示讲稿中每段预生成证据的强制标注前缀。
PRE_GENERATED_EVIDENCE_LABEL = "PRE-GENERATED EVIDENCE"

_DEMO_MODES = ("full", "short")

_FULL_DEMO_MINUTES = "12–15"
_SHORT_DEMO_MINUTES = "3"

# 完整演示的六段讲稿（与 Manifest 场景顺序一致）的时间预算（分钟）。
_FULL_SEGMENT_MINUTES = {
    "core-lifecycle": 2,
    "durable-effects-recovery": 2,
    "session-conversation": 2,
    "context-budget-compression": 2,
    "model-routing": 2,
    "eval-regression": 2,
}


def _verified_bundles(
    bundles: tuple[ScenarioEvidenceBundle, ...]
) -> tuple[ScenarioEvidenceBundle, ...]:
    if not bundles:
        raise ValueError("a release demo requires at least one Evidence Bundle")
    for bundle in bundles:
        bundle.verify()
    manifest_digest = bundles[0].manifest_digest
    execution_id = bundles[0].execution.execution_id
    for bundle in bundles:
        if bundle.manifest_digest != manifest_digest:
            raise ValueError("demo Bundles must attest one release candidate")
        if bundle.execution.execution_id != execution_id:
            raise ValueError("demo Bundles must come from one Pack Execution")
        if bundle.execution.status is not PackExecutionStatus.PASSED:
            raise ValueError(
                "a release demo can only replay a PASSED Pack execution"
            )
    return bundles


def _segment(bundle: ScenarioEvidenceBundle, minutes: int) -> list[str]:
    manifest = bundle.manifest
    declared = {
        check.check_id: check
        for check in manifest.required_checks
        if check.scenario == bundle.scenario
    }
    lines = [
        f"## {bundle.scenario} ({minutes} min)",
        "",
        (
            f"{PRE_GENERATED_EVIDENCE_LABEL} — replayed from verified Bundle"
            f" {bundle.content_digest}"
        ),
        "",
        "Replayed checks:",
    ]
    for result in bundle.checks:
        check = declared[result.check_id]
        lines.append(
            f"- `{result.check_id}`: {result.status.value} —"
            f" {result.evidence_level.value} evidence via"
            f" `{check.authoritative_evidence}`;"
            f" non-claim: {check.non_claim}"
        )
    lines.append("")
    return lines


def _candidate_lines(bundle: ScenarioEvidenceBundle) -> list[str]:
    manifest = bundle.manifest
    return [
        "Release candidate (one Manifest identity binds every Scenario):",
        f"- distribution: {manifest.environment.get('distribution', 'unknown')}"
        f" {manifest.environment.get('version', '')}".rstrip(),
        f"- artifact digest: {manifest.artifact_digest}",
        f"- sdist digest: {manifest.sdist_digest}",
        f"- fixture digest: {manifest.fixture_digest}",
        f"- source commit: {manifest.source_commit}",
        f"- profile: {manifest.profile} (pack {manifest.pack_version})",
        f"- manifest digest: {manifest.digest}",
        "",
    ]


def render_release_demo(
    bundles: Iterable[ScenarioEvidenceBundle], *, mode: str = "full"
) -> str:
    """Render the 12–15 minute (or 3 minute) demo transcript from Bundles."""
    if mode not in _DEMO_MODES:
        raise ValueError(f"unsupported demo mode: {mode!r}")
    verified = _verified_bundles(tuple(bundles))
    by_scenario = {bundle.scenario: bundle for bundle in verified}
    manifest = verified[0].manifest
    if mode == "full":
        missing = [
            scenario for scenario in manifest.scenarios if scenario not in by_scenario
        ]
        if missing:
            raise ValueError(
                f"the full demo requires every required Scenario; missing: {missing}"
            )
    elif DURABLE_EFFECTS_SCENARIO not in by_scenario:
        raise ValueError(
            "the short demo requires the durable-effects-recovery Scenario"
        )

    lines: list[str] = []
    if mode == "full":
        lines.extend(
            (
                "# m-agent 0.5 Runtime Foundation — Release Demo"
                f" ({_FULL_DEMO_MINUTES} minutes)",
                "",
                (
                    f"{PRE_GENERATED_EVIDENCE_LABEL}: every observation in this demo"
                    " is replayed from previously verified Scenario Evidence"
                    " Bundles for the exact release candidate below. Nothing is"
                    " executed live in this room."
                ),
                "",
                (
                    "Evidence levels shown are CONTRACT (offline harness) and"
                    " HOST (installed wheel). PROVIDER-level live-provider"
                    " evidence is out of scope for this replay — this demo is"
                    " NOT a live provider demonstration and makes no live,"
                    " production capacity, or SLO claim."
                ),
                "",
            )
        )
        lines.extend(_candidate_lines(verified[0]))
        lines.append("## Run order")
        lines.append("")
        for index, scenario in enumerate(manifest.scenarios, start=1):
            minutes = _FULL_SEGMENT_MINUTES.get(scenario, 2)
            lines.append(
                f"{index}. {scenario} ({minutes} min)"
            )
        lines.append("")
        for scenario in manifest.scenarios:
            bundle = by_scenario[scenario]
            lines.extend(_segment(bundle, _FULL_SEGMENT_MINUTES.get(scenario, 2)))
        lines.extend(
            (
                "## Closing: what this demo does and does not claim",
                "",
                (
                    "The Pack verdict replayed here is PASSED for this release"
                    " candidate's six required Scenarios. This is offline"
                    " acceptance evidence only: it is NOT a live provider run,"
                    " it does not claim exactly-once external effects,"
                    " automatic promotion, in-run model switching, or any 0.6"
                    " capability."
                ),
                "",
            )
        )
    else:
        durable = by_scenario[DURABLE_EFFECTS_SCENARIO]
        lines.extend(
            (
                "# m-agent 0.5 Runtime Foundation — Recovery Demo"
                f" ({_SHORT_DEMO_MINUTES} minutes)",
                "",
                (
                    f"{PRE_GENERATED_EVIDENCE_LABEL}: this short demo replays the"
                    " crash-recovery story and the release report from verified"
                    " Bundles. Nothing is executed live in this room."
                ),
                "",
                (
                    "PROVIDER-level live-provider evidence is out of scope for"
                    " this replay — this demo is NOT a live provider"
                    " demonstration."
                ),
                "",
            )
        )
        lines.extend(_candidate_lines(verified[0]))
        lines.extend(_segment(durable, 2))
        lines.extend(
            (
                "## Report (1 min)",
                "",
                (
                    f"{PRE_GENERATED_EVIDENCE_LABEL} — the full Pack for this"
                    f" release candidate completed with status"
                    f" {verified[0].execution.status.value} (exit"
                    f" {verified[0].execution.exit_code}); the complete"
                    " 12–15 minute demo replays all six required Scenarios."
                ),
                "",
            )
        )
    return "\n".join(lines)
