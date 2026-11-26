"""Redaction of internal operational paths before content leaves Core.

A projection may legitimately embed the machine it was built on: audio work
paths, model directories, job folders.  That is operational detail, not
research evidence, and it must not reach an Agent response.  The redaction is
applied at the transport egress (`nexus.service`) so every operation inherits
it, including operations added later.

The policy is deliberately narrow: only strings that look like a local
filesystem location are replaced; research text, hashes, URLs and anchors are
left untouched.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED_LOCAL]"

_INTERNAL_PATH = re.compile(
    r"(?:^|[\s\"'(=])("
    r"/home/|/Users/|/mnt/|/media/|/opt/|/srv/|/var/|/tmp/"
    r"|[A-Za-z]:[\\/]"
    r"|\\\\[A-Za-z0-9._-]+[\\/]"
    r")"
)


def looks_internal(value: Any) -> bool:
    return isinstance(value, str) and bool(_INTERNAL_PATH.search(value))


class Scrubber:
    """Recursively replace internal-path strings; count what was replaced."""

    def __init__(self) -> None:
        self.count = 0
        self.fields: list[str] = []

    def walk(self, value: Any, field: str = "") -> Any:
        if isinstance(value, str):
            if looks_internal(value):
                self.count += 1
                self.fields.append(field or "<root>")
                return REDACTED
            return value
        if isinstance(value, list):
            return [self.walk(item, field) for item in value]
        if isinstance(value, dict):
            return {key: self.walk(item, f"{field}.{key}" if field else str(key))
                    for key, item in value.items()}
        return value


def scrub_local_paths(value: Any) -> tuple[Any, int, list[str]]:
    """Return (scrubbed value, replacement count, field names)."""
    scrubber = Scrubber()
    cleaned = scrubber.walk(value)
    return cleaned, scrubber.count, sorted(set(scrubber.fields))
