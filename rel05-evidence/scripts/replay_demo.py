"""Replay the release demo from the primary pack's verified bundles (Ticket 22)."""

import json
from pathlib import Path

from m_agent.testing import render_release_demo
from m_agent.testing._pack import ScenarioEvidenceBundle

output_dir = Path("/tmp/rel05/output")
out_dir = Path("/Users/lingdu/workspace/agent/hello-agent/rel05-evidence")

bundle_paths = sorted(output_dir.glob("*.json"))
if len(bundle_paths) != 6:
    raise SystemExit(f"expected 6 scenario bundles, found {len(bundle_paths)}")

bundles = tuple(
    ScenarioEvidenceBundle.model_validate_json(path.read_text(encoding="utf-8"))
    for path in bundle_paths
)

full = render_release_demo(bundles, mode="full")
short = render_release_demo(bundles, mode="short")

(out_dir / "release-demo-full.md").write_text(full, encoding="utf-8")
(out_dir / "release-demo-short.md").write_text(short, encoding="utf-8")

result = {
    "bundles": [bundle.content_digest for bundle in bundles],
    "execution_id": bundles[0].execution.execution_id,
    "execution_status": bundles[0].execution.status.value,
    "manifest_digest": bundles[0].manifest_digest,
    "full_demo_chars": len(full),
    "short_demo_chars": len(short),
    "full_segment_count": full.count("## "),
    "short_segment_count": short.count("## "),
}
print(json.dumps(result, indent=2))
print("--- short demo head ---")
print("\n".join(short.splitlines()[:20]))
