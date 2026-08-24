"""Regression tests for the removed 0.1 compatibility surface."""

from __future__ import annotations

import subprocess
import sys
import unittest
import importlib


class CompatibilityTests(unittest.TestCase):
    def test_legacy_namespace_fails_directionally(self) -> None:
        with self.assertRaises(ImportError) as raised:
            importlib.import_module("agent_framework")
        self.assertIn("migrating-from-0.1.md", str(raised.exception))
