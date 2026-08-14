"""Setuptools hook that embeds immutable source provenance into built wheels."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import setuptools
from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


_ROOT = Path(__file__).resolve().parent
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_TREE = re.compile(r"[0-9a-f]{40}\Z")
_ARCHIVAL_TEMPLATE = b"commit $Format:%H$\ntree $Format:%T$\n"
_GENERATED_PATHS = {".git", ".venv", ".pytest_cache", "__pycache__", "build", "dist"}
_SOURCE_INTEGRITY_NAME = "SOURCE_INTEGRITY.json"


def _git_blob(data: bytes) -> bytes:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).digest()


def _archive_tree(path: Path) -> bytes:
    entries: list[bytes] = []
    for child in sorted(path.iterdir(), key=lambda candidate: os.fsencode(candidate.name)):
        if child.name in _GENERATED_PATHS or child.name.endswith(".egg-info"):
            continue
        name = os.fsencode(child.name)
        if child.is_symlink():
            mode, object_id = b"120000", _git_blob(os.fsencode(os.readlink(child)))
        elif child.is_dir():
            mode, object_id = b"40000", _archive_tree(child)
        elif child.is_file():
            data = (
                _ARCHIVAL_TEMPLATE
                if child == _ROOT / ".git_archival.txt"
                else child.read_bytes()
            )
            mode = b"100755" if child.stat().st_mode & 0o111 else b"100644"
            object_id = _git_blob(data)
        else:
            raise ValueError(f"unsupported source path: {child}")
        entries.append(mode + b" " + name + b"\0" + object_id)
    payload = b"".join(entries)
    return hashlib.sha1(b"tree " + str(len(payload)).encode() + b"\0" + payload).digest()


def _source_integrity_matches(root: Path) -> bool:
    """Validate the sdist's generated source map without trusting its tree shape."""
    try:
        declared = json.loads((root / _SOURCE_INTEGRITY_NAME).read_text())
        files = declared.get("files")
        if (
            declared.get("schema_version") != "1"
            or not isinstance(files, dict)
            or any(
                not isinstance(path, str) or not isinstance(digest, str)
                for path, digest in files.items()
            )
        ):
            return False
        def included(path: Path) -> bool:
            relative = path.relative_to(root)
            return (
                path.name != _SOURCE_INTEGRITY_NAME
                and not any(part in _GENERATED_PATHS for part in relative.parts)
                and not any(part.endswith(".egg-info") for part in relative.parts)
            )

        expected = {
            path: digest
            for path, digest in files.items()
            if not any(
                part in _GENERATED_PATHS or part.endswith(".egg-info")
                for part in Path(path).parts
            )
        }
        actual = {
            path.relative_to(root).as_posix(): "sha256:"
            + hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file() and included(path)
        }
        return expected == actual
    except (OSError, json.JSONDecodeError):
        return False


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
    archival = dict(
        line.split(" ", 1)
        for line in (_ROOT / ".git_archival.txt").read_text().splitlines()
        if " " in line
    )
    commit, tree = archival.get("commit", ""), archival.get("tree", "")
    if _COMMIT.fullmatch(commit) and _TREE.fullmatch(tree):
        if _archive_tree(_ROOT).hex() == tree or _source_integrity_matches(_ROOT):
            return commit, "clean"
        return "unavailable", "dirty"
    return "unavailable", "unknown"


def _write_build_identity(
    target: Path, identity: tuple[str, str] | None = None
) -> None:
    commit, state = identity or _source_identity()
    target.write_text(
        f"SOURCE_COMMIT = {commit!r}\n"
        f"SOURCE_STATE = {state!r}\n"
        "BUILD_TOOL = 'setuptools'\n"
        f"BUILD_TOOL_VERSION = {setuptools.__version__!r}\n"
    )


def _write_source_integrity(root: Path) -> None:
    files = {
        path.relative_to(root).as_posix(): "sha256:"
        + hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != _SOURCE_INTEGRITY_NAME
    }
    (root / _SOURCE_INTEGRITY_NAME).write_text(
        json.dumps(
            {"schema_version": "1", "files": files},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


class build_py(_build_py):
    """Replace the source-checkout fallback only in the generated wheel tree."""

    def run(self) -> None:
        package = Path(self.build_lib) / "m_agent"
        source = _ROOT / "src" / "m_agent"
        if package.is_dir():
            for module in package.rglob("*.py"):
                if not (source / module.relative_to(package)).is_file():
                    module.unlink()
        super().run()
        _write_build_identity(Path(self.build_lib) / "m_agent" / "_build_identity.py")


class sdist(_sdist):
    """Embed the same clean source provenance in the generated source artifact."""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        identity = _source_identity()
        super().make_release_tree(base_dir, files)
        _write_build_identity(
            Path(base_dir) / "src" / "m_agent" / "_build_identity.py", identity
        )
        _write_source_integrity(Path(base_dir))


setup(cmdclass={"build_py": build_py, "sdist": sdist})
