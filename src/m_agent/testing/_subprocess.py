"""Safe subprocess environment construction for offline acceptance probes."""

from __future__ import annotations

import os
from collections.abc import Mapping


# Deliberately small: process launches receive only runtime basics and an
# explicit offline marker.  Provider credentials/endpoints and Python import
# mutation knobs are never copied through ambient inheritance.
_ALLOWLIST = frozenset({"HOME", "LANG", "LC_ALL", "PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP"})
_DENIED_FRAGMENTS = ("API_KEY", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN", "BASE_URL", "ENDPOINT")


def isolated_subprocess_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return explicit safe environment for a child acceptance process.

    The optional ``source`` makes the seam testable with credential canaries;
    production callers omit it to snapshot the ambient process.  It never
    forwards Python path/venv state or provider-sensitive configuration.
    """
    source = os.environ if source is None else source
    environment = {
        key: value
        for key, value in source.items()
        if key in _ALLOWLIST
        and not any(fragment in key.upper() for fragment in _DENIED_FRAGMENTS)
    }
    environment["M_AGENT_RUN_LIVE_TESTS"] = "0"
    return environment
