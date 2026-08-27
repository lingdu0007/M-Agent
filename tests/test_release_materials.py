"""Static contracts for the M-Agent 0.5 release material."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class ReleaseMaterialTests(unittest.TestCase):
    def test_identity_python_and_dependencies(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["name"], "m-agent")
        self.assertEqual(project["version"], "0.5.0")
        self.assertEqual(project["description"], "An embeddable Agent Application Runtime for Python")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["dependencies"], ["pydantic>=2"])
        self.assertEqual(project["optional-dependencies"]["testing"], ["packaging>=23"])
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertIn("httpx>=0.27", project["optional-dependencies"]["provider"])
        for extra in ("storage", "telemetry", "security"):
            self.assertIn(extra, project["optional-dependencies"])
        for version in ("3.11", "3.12", "3.13", "3.14"):
            self.assertIn(f"Programming Language :: Python :: {version}", project["classifiers"])

    def test_release_material_and_migration_table_exist(self) -> None:
        for relative in (
            "LICENSE",
            "NOTICE",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "MANIFEST.in",
            "docs/migrating-from-0.1.md",
            "docs/migrating-to-0.3.md",
            "docs/acceptance-coverage-matrix.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        migration = (ROOT / "docs/migrating-from-0.1.md").read_text()
        self.assertIn("agent_framework.Agent", migration)
        self.assertIn("m_agent.SyncRunner", migration)
        self.assertIn("0.3.0", migration)
        for symbol in (
            "AgentResult",
            "ModelClient",
            "OpenAICompatibleClient",
            "OpenAIResponsesClient",
            "ToolCall",
            "ToolRegistry",
            "tool",
        ):
            self.assertIn(symbol, migration)

    def test_runtime_foundation_documents_expand_and_0_3_import_migration(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertIn("testing", project["optional-dependencies"])
        migration = (ROOT / "docs/migrating-to-0.3.md").read_text()
        for marker in (
            "contract reset",
            "m_agent.runtime",
            "m_agent.adapters",
            "m_agent.companion",
            "m_agent.testing",
            "m_agent.provider",
            "removed",
            "m_agent.Clock",
        ):
            self.assertIn(marker, migration)
        matrix = (ROOT / "docs/acceptance-coverage-matrix.md").read_text()
        for required_check in (
            "core.lifecycle.public-namespaces",
            "core.lifecycle.dependency-direction",
            "core.lifecycle.migration",
            "core.lifecycle.host-wheel",
        ):
            self.assertIn(required_check, matrix)
        self.assertIn("core.lifecycle.telemetry` |", matrix)
        self.assertIn("core.lifecycle.telemetry-host` |", matrix)

    def test_public_docs_are_sanitized_and_scope_qualified(self) -> None:
        paths = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
        paths.extend((ROOT / "examples").rglob("*.py"))
        paths.extend((ROOT / "examples").rglob("*.md"))
        for path in paths:
            text = path.read_text()
            self.assertNotIn("/Users/", text, path)
            self.assertNotIn("codex.ciii.club", text, path)
            self.assertNotIn("你的认证密钥", text, path)
        readme = (ROOT / "README.md").read_text()
        for marker in ("deterministic fake", "credential-gated", "environment-qualified", "not production"):
            self.assertIn(marker, readme)
