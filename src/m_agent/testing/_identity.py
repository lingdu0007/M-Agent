"""Measurements used to bind an Acceptance Manifest to its installed subject."""

from __future__ import annotations

import ast
import csv
import base64
import binascii
import hashlib
from importlib.resources import files
import json
import platform
import sys
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile
from importlib.metadata import Distribution, PackageNotFoundError, distribution, distributions
from typing import TYPE_CHECKING

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from m_agent._build_identity import (
    BUILD_TOOL,
    BUILD_TOOL_VERSION,
    SOURCE_COMMIT,
    SOURCE_STATE,
)

if TYPE_CHECKING:
    from ._pack import AcceptanceManifest


_DISTRIBUTION_NAME = "m-agent"
_SUPPORTED_HOST_OSES = frozenset({"darwin", "linux"})
_INSTALLER_METADATA = {
    "RECORD",
    "INSTALLER",
    "REQUESTED",
    "direct_url.json",
    "uv_cache.json",
}
_BUILD_IDENTITY_PATH = PurePosixPath("src/m_agent/_build_identity.py")
_SOURCE_INTEGRITY_PATH = PurePosixPath("SOURCE_INTEGRITY.json")


def _installed_distribution() -> Distribution:
    matches = [
        candidate
        for candidate in distributions(name=_DISTRIBUTION_NAME)
        if candidate.read_text("RECORD") is not None
    ]
    if len(matches) != 1:
        raise ValueError("m-agent installed distribution metadata is unavailable")
    return matches[0]


def _artifact_digest() -> str:
    installed = _installed_distribution()
    record = installed.read_text("RECORD")
    assert record is not None
    entries: list[tuple[str, str, str]] = []
    for row in csv.reader(record.splitlines()):
        if len(row) != 3:
            raise ValueError("m-agent distribution RECORD is malformed")
        path, digest, size = row
        if digest:
            algorithm, separator, encoded_digest = digest.partition("=")
            if algorithm != "sha256" or not separator:
                raise ValueError("m-agent distribution RECORD has unsupported digest")
            try:
                expected = base64.urlsafe_b64decode(
                    encoded_digest + "=" * (-len(encoded_digest) % 4)
                )
            except binascii.Error as error:
                raise ValueError("m-agent distribution RECORD has malformed digest") from error
            artifact = installed.locate_file(path)
            artifact_bytes = artifact.read_bytes()
            actual = hashlib.sha256(artifact_bytes).digest()
            if actual != expected or str(len(artifact_bytes)) != size:
                raise ValueError(f"m-agent distribution file does not match RECORD: {path}")
            entries.append((path, digest, size))
    if not entries:
        raise ValueError("m-agent distribution RECORD has no hashed files")
    canonical = json.dumps(
        sorted(entries), ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _file_digest(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"candidate artifact is not a file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _assert_sdist_matches_installed(source_artifact: Path) -> None:
    """Bind a source distribution to the embedded identity without executing it."""
    try:
        with tarfile.open(source_artifact, "r:*") as source_distribution:
            members = source_distribution.getmembers()
            regular_files = [member for member in members if member.isfile()]
            if not regular_files or any(
                not member.isfile() and not member.isdir() for member in members
            ):
                raise ValueError("source distribution contains unsupported members")
            paths = [PurePosixPath(member.name) for member in regular_files]
            if any(
                not path.parts or path.is_absolute() or ".." in path.parts
                for path in paths
            ):
                raise ValueError("source distribution contains unsafe member paths")
            roots = {path.parts[0] for path in paths}
            if len(roots) != 1:
                raise ValueError("source distribution has no unique root")
            root = roots.pop()
            files_by_path = {
                path.as_posix(): member
                for path, member in zip(paths, regular_files, strict=True)
            }
            if len(files_by_path) != len(regular_files):
                raise ValueError("source distribution contains duplicate files")
            identity_path = str(PurePosixPath(root) / _BUILD_IDENTITY_PATH)
            integrity_path = str(PurePosixPath(root) / _SOURCE_INTEGRITY_PATH)
            if identity_path not in files_by_path:
                raise ValueError("source distribution has no unique build identity")
            if integrity_path not in files_by_path:
                raise ValueError("source distribution has no source integrity manifest")
            source = source_distribution.extractfile(files_by_path[identity_path])
            if source is None:
                raise ValueError("source distribution build identity is unreadable")
            tree = ast.parse(source.read().decode("utf-8"))
            integrity = source_distribution.extractfile(files_by_path[integrity_path])
            if integrity is None:
                raise ValueError("source distribution integrity manifest is unreadable")
            declared = json.loads(integrity.read().decode("utf-8"))
            declared_files = declared.get("files")
            if (
                declared.get("schema_version") != "1"
                or not isinstance(declared_files, dict)
                or any(
                    not isinstance(path, str) or not isinstance(digest, str)
                    for path, digest in declared_files.items()
                )
            ):
                raise ValueError("source distribution integrity manifest is invalid")
            actual_files: dict[str, str] = {}
            for path, member in files_by_path.items():
                relative = str(PurePosixPath(path).relative_to(root))
                if relative == str(_SOURCE_INTEGRITY_PATH):
                    continue
                content = source_distribution.extractfile(member)
                if content is None:
                    raise ValueError("source distribution file is unreadable")
                actual_files[relative] = "sha256:" + hashlib.sha256(
                    content.read()
                ).hexdigest()
            if declared_files != actual_files:
                raise ValueError(
                    "source distribution content does not match integrity manifest"
                )
    except (
        AttributeError,
        json.JSONDecodeError,
        OSError,
        tarfile.TarError,
        UnicodeDecodeError,
        SyntaxError,
    ) as error:
        raise ValueError("candidate source distribution is invalid") from error
    values: dict[str, object] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    if (
        values.get("SOURCE_COMMIT") != SOURCE_COMMIT
        or values.get("SOURCE_STATE") != SOURCE_STATE
        or SOURCE_STATE != "clean"
    ):
        raise ValueError("source distribution does not match installed wheel provenance")


def _fixture_digest() -> str:
    fixture = files("m_agent.testing").joinpath("fixtures/core_lifecycle.json")
    return "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()


def _runtime_dependencies(installed: Distribution) -> dict[str, str]:
    """Measure the active dependency closure for the Testing profile."""
    pending = list(installed.requires or ())
    observed: dict[str, str] = {}
    environment = default_environment()
    environment["extra"] = "testing"
    while pending:
        try:
            requirement = Requirement(pending.pop())
        except InvalidRequirement as error:
            raise ValueError("m-agent dependency metadata is malformed") from error
        if requirement.marker is not None and not requirement.marker.evaluate(environment):
            continue
        normalized = canonicalize_name(requirement.name)
        if normalized in observed:
            continue
        try:
            dependency = distribution(requirement.name)
        except PackageNotFoundError as error:
            raise ValueError(
                f"m-agent runtime dependency is unavailable: {requirement.name}"
            ) from error
        observed[normalized] = dependency.version
        pending.extend(dependency.requires or ())
    return observed


def _dependency_summary(installed: Distribution) -> str:
    """Digest the active runtime dependency closure."""
    canonical = json.dumps(
        _runtime_dependencies(installed),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _installed_distribution_versions() -> dict[str, str]:
    observed: dict[str, str] = {}
    for candidate in distributions():
        name = candidate.metadata.get("Name")
        if name:
            observed[canonicalize_name(name)] = candidate.version
    return observed


def _distribution_summary(versions: dict[str, str]) -> str:
    canonical = json.dumps(
        versions, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _assert_fresh_external_environment(installed: Distribution) -> None:
    prefix = Path(sys.prefix).resolve()
    if prefix == Path(sys.base_prefix).resolve() or any(
        (ancestor / ".git").exists() for ancestor in (prefix, *prefix.parents)
    ):
        raise ValueError("Acceptance Pack requires an external virtual environment")
    allowed = set(_runtime_dependencies(installed)) | {canonicalize_name(_DISTRIBUTION_NAME)}
    foreign = sorted(set(_installed_distribution_versions()) - allowed)
    if foreign:
        raise ValueError(
            "Acceptance Pack requires a fresh environment: " + ", ".join(foreign)
        )


def _assert_installed_wheel_matches(artifact: Path, installed: Distribution) -> None:
    try:
        with ZipFile(artifact) as wheel:
            wheel_members = [info.filename for info in wheel.infolist() if not info.is_dir()]
            if not wheel_members:
                raise ValueError("candidate wheel contains no files")
            if len(wheel_members) != len(set(wheel_members)):
                raise ValueError("candidate wheel contains duplicate members")
            record = installed.read_text("RECORD")
            assert record is not None
            installed_members = {
                row[0]
                for row in csv.reader(record.splitlines())
                if len(row) == 3
            }
            candidate_members = {
                member
                for member in wheel_members
                if not _is_installer_metadata(member)
            }
            installed_product_members = {
                member
                for member in installed_members
                if not _is_installer_metadata(member)
            }
            if installed_product_members != candidate_members:
                raise ValueError("installed m-agent does not match candidate wheel members")
            for member in wheel_members:
                if _is_installer_metadata(member):
                    continue
                installed_file = installed.locate_file(member)
                if (
                    not installed_file.is_file()
                    or installed_file.read_bytes() != wheel.read(member)
                ):
                    raise ValueError(
                        f"installed m-agent does not match candidate wheel: {member}"
                    )
    except BadZipFile as error:
        raise ValueError("candidate wheel is not a valid zip archive") from error


def _is_installer_metadata(member: str) -> bool:
    path = PurePosixPath(member)
    return path.parent.name.endswith(".dist-info") and path.name in _INSTALLER_METADATA


def _installation_kind() -> str:
    direct_url = _installed_distribution().read_text("direct_url.json")
    if direct_url:
        try:
            data = json.loads(direct_url)
        except json.JSONDecodeError as error:
            raise ValueError("m-agent direct_url metadata is malformed") from error
        if "dir_info" in data:
            return "editable" if data["dir_info"].get("editable") is True else "directory"
        if "archive_info" in data:
            return "wheel" if str(data.get("url", "")).endswith(".whl") else "archive"
        return "unknown"
    return "wheel"


def installed_identity(
    *, artifact: Path | None = None, sdist: Path | None = None
) -> dict[str, str | dict[str, str]]:
    """Return the installed distribution and host identity for a frozen Manifest."""
    installed = _installed_distribution()
    if artifact is not None:
        _assert_installed_wheel_matches(artifact, installed)
    if sdist is not None:
        _assert_sdist_matches_installed(sdist)
    installed_versions = _installed_distribution_versions()
    return {
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": _file_digest(artifact) if artifact else _artifact_digest(),
        "sdist_digest": _file_digest(sdist) if sdist else "",
        "fixture_digest": _fixture_digest(),
        "environment": {
            "distribution": installed.metadata["Name"],
            "version": installed.version,
            "python": ".".join(map(str, sys.version_info[:3])),
            "os": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "installation": _installation_kind(),
            "source_state": SOURCE_STATE,
            "build_tool": f"{BUILD_TOOL}=={BUILD_TOOL_VERSION}",
            "dependency_summary": _dependency_summary(installed),
            "installed_distribution_summary": _distribution_summary(installed_versions),
            "environment_prefix_digest": "sha256:"
            + hashlib.sha256(str(Path(sys.prefix).resolve()).encode("utf-8")).hexdigest(),
        },
    }


def validate_installed_identity(
    manifest: "AcceptanceManifest", *, artifact: Path, sdist: Path
) -> None:
    """Reject a Manifest whose claimed subject differs from this installation."""
    installation = _installation_kind()
    if installation != "wheel" or SOURCE_STATE != "clean":
        raise ValueError("Acceptance Pack requires a clean wheel subject")
    if platform.system().lower() not in _SUPPORTED_HOST_OSES:
        raise ValueError("Acceptance Pack HOST evidence is unsupported on this platform")
    _assert_fresh_external_environment(_installed_distribution())
    measured = installed_identity(artifact=artifact, sdist=sdist)
    mismatches: list[str] = []
    if manifest.source_commit != measured["source_commit"]:
        mismatches.append("source_commit")
    if manifest.artifact_digest != measured["artifact_digest"]:
        mismatches.append("artifact_digest")
    if manifest.sdist_digest != measured["sdist_digest"]:
        mismatches.append("sdist_digest")
    if manifest.fixture_digest != measured["fixture_digest"]:
        mismatches.append("fixture_digest")
    if dict(manifest.environment) != measured["environment"]:
        mismatches.append("environment")
    if mismatches:
        raise ValueError(
            "Manifest does not match installed identity: " + ", ".join(mismatches)
        )
