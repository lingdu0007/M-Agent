"""Release documentation/artifact consistency checks for 0.5 RC (Ticket 22)."""

import json
import tarfile
import zipfile
from pathlib import Path

from m_agent.testing._coverage import coverage_matrix_gaps
from m_agent.testing._pack import AcceptanceManifest

ROOT = Path("/Users/lingdu/workspace/agent/hello-agent")
WHEEL = ROOT / "dist/m_agent-0.5.0-py3-none-any.whl"
SDIST = ROOT / "dist/m_agent-0.5.0.tar.gz"
checks: list[tuple[str, bool, str]] = []


def record(check_id: str, ok: bool, detail: str) -> None:
    checks.append((check_id, ok, detail))


# 1. packaging version
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
record("packaging.version", 'version = "0.5.0"' in pyproject, "pyproject.toml declares 0.5.0")

# 2. classifiers match frozen matrix (3.11-3.14)
for minor in ("3.11", "3.12", "3.13", "3.14"):
    record(
        f"packaging.classifier.{minor}",
        f"Programming Language :: Python :: {minor}" in pyproject,
        f"classifier for {minor}",
    )

# 3. README consistency
readme = (ROOT / "README.md").read_text(encoding="utf-8")
record("readme.version", "M-Agent 0.5.0" in readme, "README introduces 0.5.0")
record("readme.profile", "foundation-release-0-5" in readme, "README documents 0.5 release profile")
for scenario in (
    "core lifecycle",
    "durable effects",
    "session conversation",
    "context compression",
    "model routing",
    "eval regression",
):
    record(f"readme.scenario.{scenario.replace(' ', '-')}", scenario in readme, f"README mentions {scenario}")
record("readme.platform-matrix", "Windows is not supported" in readme, "README documents non-goal Windows")
record(
    "readme.non-claims",
    "not a universal production QPS" in readme,
    "README documents benchmark non-claims",
)

# 4. coverage matrix documents every 0.5 required check
manifest = AcceptanceManifest.model_validate_json(
    Path("/tmp/rel05/manifest.json").read_text(encoding="utf-8")
)
markdown = (ROOT / "docs/acceptance-coverage-matrix.md").read_text(encoding="utf-8")
gaps = coverage_matrix_gaps(markdown, manifest)
record(
    "coverage-matrix.no-gaps",
    not gaps,
    f"{len(manifest.required_checks)} required checks documented, gaps={list(gaps)}",
)

# 5. wheel metadata + provenance
with zipfile.ZipFile(WHEEL) as artifact:
    metadata = artifact.read("m_agent-0.5.0.dist-info/METADATA").decode("utf-8")
    identity = artifact.read("m_agent/_build_identity.py").decode("utf-8")
record("wheel.metadata-version", "Version: 0.5.0" in metadata, "wheel METADATA declares 0.5.0")
record(
    "wheel.provenance-clean",
    "SOURCE_STATE = 'clean'" in identity
    and "SOURCE_COMMIT = 'ed7177d047a528070c23bc06c84fb10c7817d2fb'" in identity,
    "wheel embeds clean source provenance for ed7177d",
)

# 6. sdist carries the git-less fallback fix
with tarfile.open(SDIST) as archive:
    setup = archive.extractfile("m_agent-0.5.0/setup.py").read().decode("utf-8")
record(
    "sdist.setup-gitless-fallback",
    "except OSError" in setup and "return None" in setup,
    "sdist setup.py carries the git-less host fallback fix",
)

# 7. license / notice
record("license.apache2", (ROOT / "LICENSE").exists(), "LICENSE present")
record("notice.present", (ROOT / "NOTICE").exists(), "NOTICE present")

# 8. benchmark README documents all four workloads
bench_readme = (ROOT / "benchmarks/README.md").read_text(encoding="utf-8")
for workload in ("Durable Run SQLite", "Durable Session conversation", "Durable Eval regression", "Explicit Context compression"):
    record(f"benchmarks.readme.{workload}", workload in bench_readme, f"benchmark README documents {workload}")
for result in (
    "release-0.5-durable-run-python311-macos-arm64.json",
    "release-0.5-session-python311-macos-arm64.json",
    "release-0.5-context-python311-macos-arm64.json",
    "release-0.5-eval-python311-macos-arm64.json",
):
    record(
        f"benchmarks.result.{result}",
        (ROOT / "benchmarks/results" / result).exists(),
        "0.5 benchmark result present",
    )

# 9. ADR 0042 present
record("adr.0042", (ROOT / "docs/adr/0042-reference-acceptance-pack-over-monolithic-demo.md").exists(), "ADR 0042 present")

# 10. examples referenced by README exist
for example in ("hello_offline.py", "m_agent_routing_eval.py", "m_agent_session_context.py"):
    record(f"examples.{example}", (ROOT / "examples" / example).exists(), f"example {example} present")

failed = [(c, d) for c, ok, d in checks if not ok]
report = {
    "total": len(checks),
    "passed": len(checks) - len(failed),
    "failed": [{"check": c, "detail": d} for c, d in failed],
    "overall": "PASS" if not failed else "FAIL",
}
out = ROOT / "rel05-evidence/consistency_checks.json"
out.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2))
