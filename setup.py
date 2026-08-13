"""Setuptools hook that embeds immutable source provenance into built wheels."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


_ROOT = Path(__file__).resolve().parent
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")


def _source_identity() -> tuple[str, str]:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    commit = completed.stdout.strip()
    if completed.returncode == 0 and _COMMIT.fullmatch(commit):
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        return commit, "clean" if status.returncode == 0 and not status.stdout else "dirty"
    archival = (_ROOT / ".git_archival.txt").read_text().strip().removeprefix("commit ")
    return (archival, "clean") if _COMMIT.fullmatch(archival) else ("unavailable", "unknown")


class build_py(_build_py):
    """Replace the source-checkout fallback only in the generated wheel tree."""

    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "m_agent" / "_build_identity.py"
        commit, state = _source_identity()
        target.write_text(f"SOURCE_COMMIT = {commit!r}\nSOURCE_STATE = {state!r}\n")


setup(cmdclass={"build_py": build_py})
