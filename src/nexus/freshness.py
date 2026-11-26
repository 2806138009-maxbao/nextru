"""Snapshot and live freshness of an ingested document, for agent tools.

``snapshot_info`` reads only the store: which source revision Nexus holds, when the source said it
was last modified, and when Nexus fetched it. ``DriveVersionChecker`` (optional) asks Google Drive
for the document's current version - metadata only, read-only - so an agent can tell whether the
snapshot is still the latest without a separate Drive round trip.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .db import Store

LiveCheck = Callable[[str], dict]


def snapshot_info(store: Store, object_id: str) -> dict[str, Any] | None:
    """Latest content revision Nexus holds for a document object, or None if it is not one."""
    row = store.conn.execute(
        "SELECT a.payload, s.source_system, s.native_id, s.revision, s.retrieved_at, s.archive_path,"
        " s.source_ref_id FROM aspects a JOIN source_refs s ON s.source_ref_id = a.source_ref_id"
        " WHERE a.object_id = ? AND a.name = 'content' ORDER BY a.revision DESC LIMIT 1",
        (object_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        payload = {}
    return {"source_system": row["source_system"], "doc_id": row["native_id"],
            "snapshot_version": row["revision"], "source_modified": payload.get("modified_time"),
            "snapshot_taken": row["retrieved_at"], "sealed": bool(row["archive_path"]),
            "source_ref_id": row["source_ref_id"]}


def with_live(info: dict, check: LiveCheck | None) -> dict:
    """Add ``live``: whether the source still has the snapshot version (never raises)."""
    if check is None or info.get("source_system") != "GOOGLE_DRIVE":
        info["live"] = {"status": "NOT_CHECKED",
                        "note": "no live source check configured; the snapshot may be stale"}
        return info
    try:
        meta = check(info["doc_id"])
        same = str(meta.get("version")) == str(info["snapshot_version"])
        info["live"] = {"status": "SAME" if same else "CHANGED",
                        "source_version": str(meta.get("version")),
                        "source_modified": meta.get("modifiedTime"),
                        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if not same:
            info["live"]["note"] = ("the source has changed since this snapshot; read the source "
                                    "itself if the change could matter")
    except Exception as exc:  # noqa: BLE001 - freshness is advisory
        info["live"] = {"status": "UNAVAILABLE", "error": type(exc).__name__}
    return info


class DriveVersionChecker:
    """Read-only Drive metadata lookup with a cached access token and short result cache."""

    def __init__(self, token_dir: str | Path, ttl_s: float = 60.0):
        from .ingest.sources import DriveSource
        self._src = DriveSource("x", token_dir)
        self._token: tuple[str, float] | None = None
        self._cache: dict[str, tuple[dict, float]] = {}
        self._ttl = ttl_s
        self._lock = threading.Lock()

    def __call__(self, doc_id: str) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(doc_id)
            if hit and now - hit[1] < self._ttl:
                return hit[0]
            if self._token is None or now - self._token[1] > 45 * 60:
                self._token = (self._src._access_token(), now)
            token = self._token[0]
        import urllib.request
        req = urllib.request.Request(f"{self._src.API}{doc_id}?fields=version,modifiedTime",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=5) as resp:  # advisory: fail fast
            meta = json.loads(resp.read())
        with self._lock:
            self._cache[doc_id] = (meta, now)
        return meta
