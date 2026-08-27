"""Final Coverage Matrix validation for the 0.5 RC manifest (Ticket 22)."""

import json
from pathlib import Path

from m_agent.testing._coverage import coverage_matrix_gaps, documented_check_ids
from m_agent.testing._pack import AcceptanceManifest

manifest = AcceptanceManifest.model_validate_json(
    Path("/tmp/rel05/manifest.json").read_text(encoding="utf-8")
)
markdown = Path(
    "/Users/lingdu/workspace/agent/hello-agent/docs/acceptance-coverage-matrix.md"
).read_text(encoding="utf-8")

gaps = coverage_matrix_gaps(markdown, manifest)
doc_ids = documented_check_ids(markdown)
result = {
    "required_checks": len(manifest.required_checks),
    "documented_check_ids": len(doc_ids),
    "gaps": list(gaps),
    "overall": "PASS" if not gaps else "FAIL",
}
out = Path(
    "/Users/lingdu/workspace/agent/hello-agent/rel05-evidence/coverage_matrix.json"
)
out.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
