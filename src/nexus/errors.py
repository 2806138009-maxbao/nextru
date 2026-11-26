"""Normalized status/error codes for the Nexus Core P1 prototype."""

from __future__ import annotations


class Status:
    OK = "OK"
    PARTIAL = "PARTIAL"
    NOT_FOUND = "NOT_FOUND"
    NOT_CHECKED = "NOT_CHECKED"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    MISSING = "MISSING"
    CONFLICTED = "CONFLICTED"
    BLOCKED = "BLOCKED"
    INVALID_SCOPE = "INVALID_SCOPE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    # Extension beyond Contract §07's basic list; recorded as a SPEC GAP in the
    # P1 report because the contract does not name an authority-refusal code.
    AUTHORITY_DENIED = "AUTHORITY_DENIED"
    # STORAGE x WRITE v0 (nexus.native)
    MIRROR_READ_ONLY = "MIRROR_READ_ONLY"
    INVALID_REQUEST = "INVALID_REQUEST"


QUERY_STATUSES = {
    Status.OK,
    Status.PARTIAL,
    Status.NOT_FOUND,
    Status.NOT_CHECKED,
    Status.UNKNOWN,
    Status.STALE,
    Status.MISSING,
    Status.CONFLICTED,
    Status.BLOCKED,
    Status.INVALID_SCOPE,
    Status.VERSION_CONFLICT,
    Status.AUTHORITY_DENIED,
    Status.MIRROR_READ_ONLY,
    Status.INVALID_REQUEST,
}
