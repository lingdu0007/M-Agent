"""Regression tests for the bounded 0.1 compatibility surface."""

from __future__ import annotations

import subprocess
import sys
import unittest
import warnings


class CompatibilityTests(unittest.TestCase):
    def test_supported_import_warns_with_migration(self) -> None:
        script = """
import warnings
warnings.simplefilter('always')
with warnings.catch_warnings(record=True) as caught:
    from agent_framework import Agent, EchoModel
assert any('agent_framework.Agent' in str(item.message) for item in caught)
assert any('m_agent.SyncRunner' in str(item.message) for item in caught)
assert any('0.3.0' in str(item.message) for item in caught)
"""
        result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_agent_behavior_is_preserved(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from agent_framework import Agent, EchoModel
        result = Agent(name="legacy", instructions="echo", model=EchoModel()).run("hello")
        self.assertEqual(result.output, "Echo: hello")

    def test_every_supported_export_warns_with_its_migration(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import agent_framework

        for name in agent_framework.__all__:
            if name == "LegacyMigrationError":
                continue
            with self.subTest(name=name), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", DeprecationWarning)
                getattr(agent_framework, name)
                self.assertTrue(caught)
                message = next(
                    str(item.message)
                    for item in caught
                    if f"agent_framework.{name}" in str(item.message)
                )
                self.assertIn(f"agent_framework.{name}", message)
                self.assertIn("0.3.0", message)
                self.assertIn("docs/migrating-from-0.1.md", message)

    def test_unsupported_exports_and_modules_fail_explicitly(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import agent_framework
        with self.assertRaises(agent_framework.LegacyMigrationError) as raised:
            agent_framework.Workflow
        self.assertIn("no semantics-preserving", str(raised.exception))
        script = """
import warnings
warnings.simplefilter('ignore', DeprecationWarning)
try:
    import agent_framework.workflow
except Exception as exc:
    from agent_framework import LegacyMigrationError
    assert isinstance(exc, LegacyMigrationError)
    assert 'docs/migrating-from-0.1.md' in str(exc)
else:
    raise AssertionError('unsupported module imported')
"""
        result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_legacy_namespace_and_agent_options_fail_explicitly(self) -> None:
        script = """
import warnings
warnings.simplefilter('ignore', DeprecationWarning)
from agent_framework import Agent, EchoModel, LegacyMigrationError
try:
    import agent_framework._legacy.workflow
except LegacyMigrationError as exc:
    assert 'docs/migrating-from-0.1.md' in str(exc)
else:
    raise AssertionError('private legacy namespace imported')
try:
    Agent(name='legacy', instructions='', model=EchoModel(), memory=object())
except LegacyMigrationError as exc:
    assert 'no semantics-preserving' in str(exc)
else:
    raise AssertionError('unsupported Agent option accepted')
"""
        result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_supported_submodule_import_warns_with_specific_migration(self) -> None:
        script = """
import warnings
warnings.simplefilter('always')
with warnings.catch_warnings(record=True) as caught:
    from agent_framework.models import EchoModel
assert EchoModel
messages = [str(item.message) for item in caught]
assert any('agent_framework.models' in message for message in messages)
assert any('EchoModel' in message and 'm_agent.DeterministicModelAdapter' in message for message in messages)
assert any('0.3.0' in message and 'docs/migrating-from-0.1.md' in message for message in messages)
"""
        result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
