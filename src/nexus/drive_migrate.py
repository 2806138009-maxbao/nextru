"""Move one Google Drive document into Nexus as a native document (migrate on edit).

A Drive snapshot (``doc/...``) is a read-only mirror: its Authority is in Drive. When the owner
starts editing it in Nexus (an edit to its Obsidian note), ``migrate`` makes the move explicit:

* a native document is created whose revision 1 is the snapshot's text, so the Drive version stays
  the first revision of the new document and every later edit diffs against it;
* every live relation of the Drive document is carried over to the native one (same type,
  verification, evidence, author); the originals are expired with ``superseded_by`` pointing at
  their copies, so history keeps both;
* ``<native> -SUPERSEDES-> <drive doc>`` is recorded and the Drive document's ``doc_status`` becomes
  SUPERSEDED with ``superseded_by``: resolve treats it as retired and names its successor, identity
  cards show the lineage;
* the Drive document gets a ``MIGRATED`` authority binding whose source of truth is the native doc.

Idempotent: a Drive document that already has a native successor returns that successor.
"""

from __future__ import annotations

import json
from typing import Any

from .db import canonical_json, utcnow
from .errors import Status
from .native import NativeDocs, NativeError, _new_id
from . import search as search_index


def successor(conn, doc_oid: str) -> str | None:
    r = conn.execute("SELECT from_id FROM relations WHERE to_id = ? AND relation_type = 'SUPERSEDES'"
                     " AND expired_at IS NULL AND from_id LIKE 'ndoc/%' ORDER BY created_at LIMIT 1",
                     (doc_oid,)).fetchone()
    return r[0] if r else None


def migrate(nd: NativeDocs, scope: str, doc_oid: str, spec: dict, actor: str = "user",
            snapshot: str | None = None) -> dict:
    """``spec``: the native document's first revision (title + children), i.e. the snapshot as shown."""
    conn = nd.conn
    o = conn.execute("SELECT type, scope FROM objects WHERE object_id = ?", (doc_oid,)).fetchone()
    if o is None or o["scope"] != scope or not doc_oid.startswith("doc/"):
        raise NativeError(Status.NOT_FOUND, f"no Drive document {doc_oid!r} in scope {scope!r}")
    done = successor(conn, doc_oid)
    if done:
        return {"status": Status.OK, "doc_id": done, "from": doc_oid, "already": True, "relations_moved": 0}
    created = nd.create(scope, spec, actor, message=f"migrated from Google Drive {doc_oid}"
                        + (f" (snapshot {snapshot})" if snapshot else "") + "; this revision is the Drive text")
    new = created["doc_id"]
    now = utcnow()
    moved = 0
    with nd._tx():
        rows = conn.execute("SELECT * FROM relations WHERE (from_id = ? OR to_id = ?) AND expired_at IS NULL",
                            (doc_oid, doc_oid)).fetchall()
        for r in rows:
            frm = new if r["from_id"] == doc_oid else r["from_id"]
            to = new if r["to_id"] == doc_oid else r["to_id"]
            if frm == to:
                continue
            rid = _new_id("rel_")
            conn.execute(
                "INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
                " provenance_refs, valid_from, valid_to, created_at, extraction_run) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (rid, frm, r["relation_type"], to, r["verification"], r["scope"], r["provenance_refs"],
                 r["valid_from"], r["valid_to"], now, r["extraction_run"]))
            for i, ob in enumerate(conn.execute(
                    "SELECT * FROM observations WHERE dimension = 'relation_evidence' AND detail LIKE ?",
                    (f'%"relation_id": "{r["relation_id"]}"%',)).fetchall()):
                d = json.loads(ob["detail"] or "{}")
                d.update(relation_id=rid, carried_from=r["relation_id"])
                conn.execute(
                    "INSERT OR IGNORE INTO observations(observation_id, object_id, dimension, statement, stance,"
                    " detail, source_ref_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (f"obs/{rid}/{i}", frm, "relation_evidence", ob["statement"], ob["stance"],
                     json.dumps(d, ensure_ascii=False), ob["source_ref_ids"], now))
            conn.execute("UPDATE relations SET expired_at = ?, superseded_by = ? WHERE relation_id = ?",
                         (now, rid, r["relation_id"]))
            moved += 1
        conn.execute(
            "INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
            " provenance_refs, created_at, extraction_run) VALUES (?,?, 'SUPERSEDES', ?, 'VERIFIED', ?, '[]', ?, ?)",
            (_new_id("rel_"), new, doc_oid, scope, now, actor))
        cur = conn.execute("SELECT * FROM aspects WHERE object_id = ? AND name = 'doc_status' AND is_current = 1",
                           (doc_oid,)).fetchone()
        payload: dict[str, Any] = json.loads(cur["payload"]) if cur else {}
        payload.update(currency="SUPERSEDED", currency_basis="migrated to a Nexus native document",
                       superseded_by=new, migrated_at=now, migrated_by=actor,
                       previous_currency=(json.loads(cur["payload"]).get("currency") if cur else None))
        rev = (cur["revision"] + 1) if cur else 1
        if cur:
            conn.execute("UPDATE aspects SET is_current = 0, valid_to = ? WHERE version_id = ?", (now, cur["version_id"]))
        conn.execute(
            "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle, verification,"
            " revision, is_current, valid_from, source_ref_id, updated_at) VALUES (?,?, 'doc_status', ?, ?,"
            " 'CURRENT', 'VERIFIED', ?, 1, ?, ?, ?)",
            (f"{doc_oid}:doc_status@m{rev}", doc_oid, canonical_json(payload),
             cur["schema_version"] if cur else "doc-status-1", rev, now, cur["source_ref_id"] if cur else None, now))
        b = conn.execute("SELECT binding_id FROM authority_bindings WHERE target_type = 'object' AND target_id = ?"
                         " AND field_scope IS NULL AND effective_to IS NULL", (doc_oid,)).fetchone()
        if b:
            conn.execute("UPDATE authority_bindings SET effective_to = ? WHERE binding_id = ?", (now, b[0]))
        conn.execute(
            "INSERT INTO authority_bindings(binding_id, target_type, target_id, field_scope, owner, scope,"
            " source_of_truth, effective_from, write_policy, migration_state) VALUES (?, 'object', ?, NULL, 'user',"
            " ?, ?, ?, 'READ_ONLY', 'MIGRATED')", (_new_id("auth_"), doc_oid, scope, f"NEXUS:{new}", now))
        nd._event("USER_DECISION", doc_oid, {"action": "drive.migrate", "native": new, "relations_moved": moved,
                                             "snapshot": snapshot}, actor)
        search_index.reindex_native(conn, new)
    return {"status": Status.OK, "doc_id": new, "from": doc_oid, "already": False, "relations_moved": moved}
