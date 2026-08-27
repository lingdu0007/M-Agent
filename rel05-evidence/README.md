# 0.5 Runtime Foundation — Release Evidence Archive (Ticket 22)

This directory archives the release-acceptance evidence for the 0.5 Runtime
Foundation candidate. Everything here is **pre-generated, offline evidence**;
nothing in this archive is a live-provider, production-capacity, SLO/SLA, or
exactly-once claim.

## Release candidate identity

- source commit: `ed7177d047a528070c23bc06c84fb10c7817d2fb` (clean checkout)
- wheel digest: `sha256:4cf648474e6461e0285459a00d4f43ca3f26eff88ffe4c35fdece630bf74cb27`
  (`m_agent-0.5.0-py3-none-any.whl`)
- sdist digest: `sha256:09d458160b66509a4e0f393e41e6eafb0f235cab992689bb23189492bdb97263`
  (`m_agent-0.5.0.tar.gz`)
- fixture digest: `sha256:76d7fee8fbe3293841daf42091e723ab14e5c974121a23f66dfc21ee8b56562d`
- primary pack manifest digest: `sha256:560156e5ca26cc706c31c7231fbe7329ba56c3f800fd6fc1f0700758a6e6f2be`
  (`manifest.json`, generated from the installed RC wheel identity)

## Contents

- `bundles/` — the six PASSED Scenario Evidence Bundles of the primary Pack
  Execution `foundation-release-0-5-695552acfcce4876ae7888ddb4b2e35c`
  (core-lifecycle, durable-effects-recovery, session-conversation,
  context-budget-compression, model-routing, eval-regression). Files are named
  by their sha256 `content_digest`; any edit breaks verification.
- `manifest.json` — the frozen `foundation-release-0-5` acceptance manifest
  the primary Pack Execution ran against.
- `observations/` — per-platform `PlatformMatrixObservation` records for every
  cell of the frozen cross-platform matrix (`linux-3.11` … `linux-3.14`
  CONTRACT/required, `linux-3.11` HOST/primary, `darwin-3.11` and `darwin-3.14`
  HOST/secondary).
- `platform_matrix_evidence.json` — the aggregated `PlatformMatrixEvidence`
  verdict: 7/7 cells PASS, no gaps, one artifact digest.
- `coverage_matrix.json` — Coverage Matrix validation result: 48/48 required
  checks documented in `docs/acceptance-coverage-matrix.md`, no gaps.
- `consistency_checks.json` — release documentation/artifact consistency
  checks: 33/33 PASS (packaging version, classifiers, README, wheel metadata
  and provenance, sdist git-less fallback, license, benchmarks docs, ADR,
  examples).
- `release-demo-full.md` / `release-demo-short.md` — the 12–15 minute full
  demo and 3-minute recovery/report short demo rendered from the archived
  bundles via `render_release_demo` (PRE-GENERATED EVIDENCE labels included).
- `scripts/` — the as-run evidence-generation scripts, recorded verbatim
  (absolute paths are the release host's paths; they document provenance, they
  are not a turnkey runner). `run_linux_matrix.sh` executed inside a Docker
  linux/arm64 bookworm image; `run_darwin_matrix.sh` executed on the macOS
  host; `run_benchmarks.sh` executed the four correctness-first workloads
  against the installed RC wheel with `M_AGENT_BENCHMARK_ARTIFACT_DIGEST` /
  `M_AGENT_BENCHMARK_MANIFEST_DIGEST` bound to the RC identity above.

Benchmark result JSONs live in `benchmarks/results/release-0.5-*.json`
(durable-run, session, context, eval; all `validation: PASS` before metrics).

## Verification from this archive (offline)

```bash
python - <<'PY'
from pathlib import Path
from m_agent.testing import render_release_demo
from m_agent.testing._pack import ScenarioEvidenceBundle
bundles = tuple(
    ScenarioEvidenceBundle.model_validate_json(p.read_text())
    for p in sorted(Path("rel05-evidence/bundles").glob("*.json"))
)
print(render_release_demo(bundles, mode="short"))
PY
```

Every bundle re-verifies (content digest + structural assertions) before the
demo renders; tampering raises `BundleIntegrityError`.

## Verification record (release host, 2026-08-27)

- Full offline test suite: 1222 passed, 12 skipped, 524 subtests passed
  (`uv run pytest tests/ -q`).
- mypy `src/`: 157 pre-existing findings in 18 files both before (commit
  `3e3d71a`, 89 files) and after Ticket 22 (commit `ed7177d`, 92 files) —
  no new type errors introduced by this release.
- Dual-axis review: Standards Review PASS (0 P0, 0 P1, 1 P2 — structural
  duplication of the frozen release runner, consistent with the 0.3→0.4→0.5
  frozen-runner pattern, deferred to 0.6); Spec Review PASS (8/8 objectives
  MET, 0 P0, 0 P1, 3 P2 — evidence archival addressed by this directory,
  matrix/demo/coverage remain library seams plus recorded scripts by design,
  and the runner's fail-closed resume-state behavior is accepted).

## Non-claims

This archive contains offline acceptance evidence only. It does not claim
live-provider compatibility (see Ticket 21), production capacity, QPS,
latency, scalability, availability, SLO/SLA, exactly-once external effects,
automatic promotion, in-run model switching, or any 0.6 capability.
