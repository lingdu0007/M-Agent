"""Measurements used to bind an Acceptance Manifest to its installed subject."""

from __future__ import annotations

import csv
import base64
import binascii
import hashlib
import json
import platform
import sys
from importlib.metadata import Distribution, distributions
from typing import TYPE_CHECKING

from m_agent._build_identity import SOURCE_COMMIT, SOURCE_STATE

if TYPE_CHECKING:
    from ._pack import AcceptanceManifest


_DISTRIBUTION_NAME = "m-agent"


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


def _installation_kind() -> str:
    direct_url = _installed_distribution().read_text("direct_url.json")
    if direct_url:
        try:
            data = json.loads(direct_url)
        except json.JSONDecodeError as error:
            raise ValueError("m-agent direct_url metadata is malformed") from error
        if data.get("dir_info", {}).get("editable") is True:
            return "editable"
    return "wheel"


def installed_identity() -> dict[str, str | dict[str, str]]:
    """Return the installed distribution and host identity for a frozen Manifest."""
    installed = _installed_distribution()
    return {
        "source_commit": SOURCE_COMMIT,
        "artifact_digest": _artifact_digest(),
        "environment": {
            "distribution": installed.metadata["Name"],
            "version": installed.version,
            "python": ".".join(map(str, sys.version_info[:3])),
            "os": platform.system().lower(),
            "architecture": platform.machine().lower(),
            "installation": _installation_kind(),
            "source_state": SOURCE_STATE,
        },
    }


def validate_installed_identity(manifest: "AcceptanceManifest") -> None:
    """Reject a Manifest whose claimed subject differs from this installation."""
    measured = installed_identity()
    mismatches: list[str] = []
    if manifest.source_commit != measured["source_commit"]:
        mismatches.append("source_commit")
    if manifest.artifact_digest != measured["artifact_digest"]:
        mismatches.append("artifact_digest")
    if dict(manifest.environment) != measured["environment"]:
        mismatches.append("environment")
    if mismatches:
        raise ValueError(
            "Manifest does not match installed identity: " + ", ".join(mismatches)
        )
