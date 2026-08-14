"""Measurements used to bind an Acceptance Manifest to its installed subject."""

from __future__ import annotations

import csv
import base64
import binascii
import hashlib
from importlib.resources import files
import json
import platform
import sys
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
_INSTALLER_METADATA = {
    "RECORD",
    "INSTALLER",
    "REQUESTED",
    "direct_url.json",
    "uv_cache.json",
}


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
        raise ValueError(f"candidate wheel is not a file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(65536), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _fixture_digest() -> str:
    fixture = files("m_agent.testing").joinpath("fixtures/core_lifecycle.json")
    return "sha256:" + hashlib.sha256(fixture.read_bytes()).hexdigest()


def _runtime_dependencies(installed: Distribution) -> dict[str, str]:
    """Measure the active, non-extra runtime dependency closure."""
    pending = list(installed.requires or ())
    observed: dict[str, str] = {}
    environment = default_environment()
    environment["extra"] = ""
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
    *, artifact: Path | None = None
) -> dict[str, str | dict[str, str]]:
    """Return the installed distribution and host identity for a frozen Manifest."""
    installed = _installed_distribution()
    if artifact is not None:
        _assert_installed_wheel_matches(artifact, installed)
    installed_versions = _installed_distribution_versions()
    return {
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": _file_digest(artifact) if artifact else _artifact_digest(),
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
    manifest: "AcceptanceManifest", *, artifact: Path
) -> None:
    """Reject a Manifest whose claimed subject differs from this installation."""
    installation = _installation_kind()
    if installation != "wheel" or SOURCE_STATE != "clean":
        raise ValueError("Acceptance Pack requires a clean wheel subject")
    _assert_fresh_external_environment(_installed_distribution())
    measured = installed_identity(artifact=artifact)
    mismatches: list[str] = []
    if manifest.source_commit != measured["source_commit"]:
        mismatches.append("source_commit")
    if manifest.artifact_digest != measured["artifact_digest"]:
        mismatches.append("artifact_digest")
    if manifest.fixture_digest != measured["fixture_digest"]:
        mismatches.append("fixture_digest")
    if dict(manifest.environment) != measured["environment"]:
        mismatches.append("environment")
    if mismatches:
        raise ValueError(
            "Manifest does not match installed identity: " + ", ".join(mismatches)
        )
