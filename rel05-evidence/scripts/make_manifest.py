import json
from pathlib import Path

from m_agent.testing._identity import installed_identity
from m_agent.testing._pack import foundation_release_0_5_manifest

wheel = Path("/Users/lingdu/workspace/agent/hello-agent/dist/m_agent-0.5.0-py3-none-any.whl")
sdist = Path("/Users/lingdu/workspace/agent/hello-agent/dist/m_agent-0.5.0.tar.gz")
identity = installed_identity(artifact=wheel, sdist=sdist)
manifest = foundation_release_0_5_manifest(
    source_commit=identity["source_commit"],
    artifact_digest=identity["artifact_digest"],
    sdist_digest=identity["sdist_digest"],
    fixture_digest=identity["fixture_digest"],
    environment=identity["environment"],
)
manifest_path = Path("/tmp/rel05/manifest.json")
manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
print("manifest_digest:", manifest.digest)
print("source_commit:", manifest.source_commit)
print("scenario_count:", len(manifest.scenarios))
print("required_checks:", len(manifest.required_checks))
print(json.dumps(manifest.environment, indent=2))
