"""Extract honest platform-matrix observations from one release-pack output directory.

Reads every scenario bundle, aggregates per-level evidence digests, and emits
one observation JSON for each requested (level, role) cell for this environment.
Only cells that exist in FOUNDATION_PLATFORM_MATRIX_0_5 may be requested.
"""

import hashlib
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
platform_name = sys.argv[2]  # "linux" | "darwin"
python_version = sys.argv[3]  # e.g. "3.11"
artifact_digest = sys.argv[4]  # rc wheel digest "sha256:..."
evidence_source = sys.argv[5]
out_path = Path(sys.argv[6])
cells = sys.argv[7].split(",")  # e.g. "CONTRACT:required,HOST:primary"

bundles = sorted(output_dir.glob("*.json"))
if len(bundles) != 6:
    raise SystemExit(f"expected 6 scenario bundles, found {len(bundles)}")

all_passed = True
by_level: dict[str, list[tuple[str, str, str]]] = {}
execution_ids = set()
for bundle_path in bundles:
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    execution_ids.add(payload["execution"]["execution_id"])
    if payload["execution"]["status"] != "PASSED" or payload["execution"]["exit_code"] != 0:
        all_passed = False
    for check in payload["checks"]:
        by_level.setdefault(check["evidence_level"], []).append(
            (check["check_id"], check["status"], check["evidence_digest"])
        )

if len(execution_ids) != 1:
    raise SystemExit(f"expected one pack execution, found {execution_ids}")

observations = []
for cell in cells:
    level, role = cell.split(":")
    checks = sorted(by_level.get(level, []))
    if not checks:
        raise SystemExit(f"no {level} checks found in bundles")
    statuses = {status for _, status, _ in checks}
    if not all_passed or statuses != {"PASS"}:
        status = "FAIL" if statuses & {"FAIL", "ERROR"} else "ERROR"
    else:
        status = "PASS"
    payload = json.dumps(
        {"execution": sorted(execution_ids), "checks": checks},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    observations.append(
        {
            "platform": platform_name,
            "python": python_version,
            "level": level,
            "role": role,
            "status": status,
            "evidence_source": evidence_source,
            "evidence_digest": evidence_digest,
            "artifact_digest": artifact_digest,
        }
    )

out_path.write_text(json.dumps(observations, indent=2), encoding="utf-8")
for observation in observations:
    print(
        f"{observation['platform']}-{observation['python']} "
        f"{observation['level']}/{observation['role']}: {observation['status']} "
        f"({observation['evidence_digest'][:19]}...)"
    )
