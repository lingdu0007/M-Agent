"""Aggregate per-platform observations into the final PlatformMatrixEvidence (Ticket 22)."""

import json
import sys
from pathlib import Path

from m_agent.testing import PlatformMatrixEvidence, PlatformMatrixObservation

base = Path(sys.argv[1])
out_path = Path(sys.argv[2])

raw = []
for observations_path in sorted(base.glob("*/observations.json")):
    raw.extend(json.loads(observations_path.read_text(encoding="utf-8")))

observations = tuple(PlatformMatrixObservation.model_validate(item) for item in raw)
evidence = PlatformMatrixEvidence.create(observations=observations)

out_path.write_text(
    json.dumps(json.loads(evidence.model_dump_json()), indent=2, sort_keys=True),
    encoding="utf-8",
)
print(f"overall_status: {evidence.overall_status}")
print(f"observations: {len(evidence.observations)}")
print(f"gaps: {list(evidence.gaps)}")
for observation in sorted(evidence.observations, key=lambda o: o.key):
    print(
        f"  {observation.platform}-{observation.python} "
        f"{observation.level.value}/{observation.role}: {observation.status.value}"
    )
