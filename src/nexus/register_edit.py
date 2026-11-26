"""A course register that has moved from its Google Sheet into Nexus (``reg/<course>/...``).

``nexus.ingest.register`` mirrors a CORE REGISTER Sheet read-only: every row is bound
``READ_ONLY_MIRROR`` / ``MIRRORED`` to the Sheet. ``migrate`` is the one explicit cutover that makes
Nexus the source of truth for the whole course register:

* every open authority binding of the course is closed (kept as history) and replaced by a
  ``MIGRATED`` binding whose ``source_of_truth`` is ``NEXUS:<object id>``;
* from then on ``nexus.ingest.register`` refuses to re-import the course (a re-import would retire
  every row and rebuild the relations from the Sheet, silently dropping what was edited here);
* the Sheet stays where it is as the pre-cutover archive.

After the cutover rows are edited with ``edit_fields`` (one new revision of the row's aspect; the
old one stays readable with ``is_current = 0``) and added with ``add_row``. Payloads keep the
importer's shape and schema (``row`` = the cells, ``value`` = the row's headline state), so
``nexus.learning`` and every reader keep working unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import canonical_json, utcnow
from .errors import Status
from .ingest.register import SCHEMA, _slug
from .native import NativeDocs, NativeError, _is_agent, _new_id
from . import search as search_index

MIGRATED = "MIGRATED"
POLICY = "USER_AND_AGENTS"
# aspect -> the cell that is its headline "value" (what the importer stores as payload.value)
HEADLINE = {"learner_state": ("capability_state",), "receipt": ("evidence_class",),
            "register_row": ("epistemic_state", "processing_state", "evidence_depth")}
# object type -> (aspect a new row gets, id prefix under reg/<course>/, id cell)
NEW_ROW = {"evidence": ("register_row", "ev/", "evidence_id"),
           "learner_receipt": ("receipt", "receipt/", "receipt_id")}


def course_of(oid: str) -> str | None:
    m = re.match(r"^(reg/[^/]+)", oid or "")
    return m.group(1) if m else None


def binding(conn, oid: str) -> dict | None:
    r = conn.execute("SELECT * FROM authority_bindings WHERE target_type = 'object' AND target_id = ?"
                     " AND field_scope IS NULL AND effective_to IS NULL", (oid,)).fetchone()
    return dict(r) if r else None


def is_migrated(conn, oid: str) -> bool:
    b = binding(conn, course_of(oid) or oid)
    return bool(b) and b["migration_state"] == MIGRATED


def migrate(nd: NativeDocs, course_oid: str, actor: str = "user", note: str | None = None) -> dict:
    """Cut the course register over from its Sheet to Nexus. Idempotent."""
    conn = nd.conn
    if _is_agent(actor):
        raise NativeError(Status.AUTHORITY_DENIED, "only the owner moves a register's source of truth")
    if conn.execute("SELECT 1 FROM objects WHERE object_id = ? AND type = 'course'", (course_oid,)).fetchone() is None:
        raise NativeError(Status.NOT_FOUND, f"no course register {course_oid!r}")
    now = utcnow()
    moved = 0
    with nd._tx():
        oids = [r[0] for r in conn.execute("SELECT object_id FROM objects WHERE object_id = ? OR object_id LIKE ?",
                                           (course_oid, course_oid + "/%"))]
        for oid in oids:
            b = binding(conn, oid)
            if b and b["migration_state"] == MIGRATED:
                continue
            scope = b["scope"] if b else conn.execute("SELECT scope FROM objects WHERE object_id = ?",
                                                      (oid,)).fetchone()[0]
            if b:
                conn.execute("UPDATE authority_bindings SET effective_to = ? WHERE binding_id = ?",
                             (now, b["binding_id"]))
            conn.execute(
                "INSERT INTO authority_bindings(binding_id, target_type, target_id, field_scope, owner, scope,"
                " source_of_truth, effective_from, write_policy, migration_state) VALUES (?, 'object', ?, NULL,"
                " ?, ?, ?, ?, ?, ?)",
                (_new_id("auth_"), oid, "user", scope, f"NEXUS:{oid}", now, POLICY, MIGRATED))
            moved += 1
        prior = conn.execute("SELECT source_of_truth FROM authority_bindings WHERE target_id = ? AND"
                             " effective_to = ?", (course_oid, now)).fetchone()
        nd._event("USER_DECISION", course_oid, {
            "action": "register.migrate", "objects": moved, "from": prior[0] if prior else None,
            "note": note or "the register's source of truth moved from the Google Sheet to Nexus; "
                            "the Sheet is the pre-cutover archive"}, actor)
    return {"status": Status.OK, "course": course_oid, "migrated_objects": moved,
            "from": prior[0] if prior else None, "at": now}


def _require_editable(conn, oid: str) -> None:
    b = binding(conn, oid)
    if b is None or b["migration_state"] != MIGRATED:
        raise NativeError(Status.MIRROR_READ_ONLY,
                          f"{oid!r} is a read-only mirror of its Sheet; migrate the register first",
                          migration_state=b["migration_state"] if b else None)


def _headline(aspect: str, row: dict, old: Any) -> Any:
    for k in HEADLINE.get(aspect, ()):
        if row.get(k):
            return row[k]
    return old


def edit_fields(nd: NativeDocs, oid: str, aspect: str, actor: str, *, expected_revision: int | None = None,
                set_fields: dict[str, Any] | None = None, unset: list[str] | None = None) -> dict:
    """Change cells of one row (one aspect of a register object). New revision, compare-and-swap."""
    conn = nd.conn
    _require_editable(conn, oid)
    if _is_agent(actor) and not nd.agent_writes:
        raise NativeError(Status.AUTHORITY_DENIED, "agents cannot edit register rows here")
    was_fresh = nd._index_fresh()
    with nd._tx():
        cur = conn.execute("SELECT * FROM aspects WHERE object_id = ? AND name = ? AND is_current = 1",
                           (oid, aspect)).fetchone()
        if cur is None and aspect == "learner_state" and not expected_revision:
            # a knowledge row added after the cutover has no Learner_State yet: the first edit creates it
            if conn.execute("SELECT 1 FROM objects WHERE object_id = ?", (oid,)).fetchone() is None:
                raise NativeError(Status.NOT_FOUND, f"no register row {oid!r}")
            row = {"knowledge_id": oid.rsplit("/", 1)[-1],
                   **{k: ("" if v is None else str(v)) for k, v in (set_fields or {}).items()}}
            now = utcnow()
            payload = {"value": _headline(aspect, row, None), "row": row, "kind": "owner_register_row",
                       "register": None, "register_version": None, "anchor": None, "source": "nexus",
                       "edited_by": actor, "edited_at": now}
            conn.execute(
                "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle, verification,"
                " revision, is_current, valid_from, source_ref_id, updated_at) VALUES (?,?,?,?,?, 'CURRENT', ?, 1,"
                " 1, ?, NULL, ?)",
                (f"{oid}:{aspect}@n1", oid, aspect, canonical_json(payload), SCHEMA,
                 "UNVERIFIED" if _is_agent(actor) else "VERIFIED", now, now))
            nd._event("AGENT_WRITE" if _is_agent(actor) else "USER_DECISION", oid,
                      {"action": "register.edit", "aspect": aspect, "revision": 1, "created": True}, actor)
            search_index.reindex_object(conn, oid)
            nd._keep_fresh(was_fresh)
            return {"status": Status.OK, "object_id": oid, "aspect": aspect, "revision": 1,
                    "changed": sorted(row), "created": True}
        if cur is None:
            raise NativeError(Status.NOT_FOUND, f"{oid!r} has no current {aspect!r} row")
        if expected_revision is not None and int(expected_revision) != cur["revision"]:
            raise NativeError(Status.VERSION_CONFLICT,
                              f"{oid!r} {aspect} is at revision {cur['revision']}, not {expected_revision}",
                              expected_revision=int(expected_revision), current_revision=cur["revision"])
        payload = json.loads(cur["payload"])
        row = dict(payload.get("row") or {})
        changed = []
        for k, v in (set_fields or {}).items():
            v = "" if v is None else str(v)
            if row.get(k) != v:
                row[k] = v
                changed.append(k)
        for k in unset or []:
            if k in row:
                del row[k]
                changed.append(k)
        if not changed:
            return {"status": Status.OK, "object_id": oid, "aspect": aspect, "revision": cur["revision"],
                    "changed": []}
        rev = cur["revision"] + 1
        now = utcnow()
        payload.update(row=row, value=_headline(aspect, row, payload.get("value")), edited_by=actor,
                       edited_at=now, source="nexus")
        if "只镜像" in str(payload.get("note", "")):
            payload.pop("note")                # the mirror-era "do not edit here" note no longer holds
        if aspect == "register_row" and "canonical_label" in changed:
            # same display name the importer gives: "<object_id>｜<canonical_label>"
            local = row.get("object_id") or oid.rsplit("/", 1)[-1]
            conn.execute("UPDATE objects SET display_name = ? WHERE object_id = ?",
                         (f"{local}｜{row['canonical_label']}".rstrip("｜"), oid))
        conn.execute("UPDATE aspects SET is_current = 0, valid_to = ? WHERE version_id = ?", (now, cur["version_id"]))
        conn.execute(
            "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle, verification,"
            " revision, is_current, valid_from, source_ref_id, updated_at) VALUES (?,?,?,?,?, 'CURRENT', ?, ?, 1,"
            " ?, NULL, ?)",
            (f"{oid}:{aspect}@n{rev}", oid, aspect, canonical_json(payload), cur["schema_version"],
             "UNVERIFIED" if _is_agent(actor) else "VERIFIED", rev, now, now))
        nd._event("AGENT_WRITE" if _is_agent(actor) else "USER_DECISION", oid,
                  {"action": "register.edit", "aspect": aspect, "revision": rev, "changed": sorted(changed)}, actor)
        search_index.reindex_object(conn, oid)
        nd._keep_fresh(was_fresh)
    return {"status": Status.OK, "object_id": oid, "aspect": aspect, "revision": rev, "changed": sorted(changed)}


def add_row(nd: NativeDocs, course_oid: str, obj_type: str, local_id: str, actor: str,
            fields: dict[str, Any] | None = None, label: str | None = None) -> dict:
    """A new row of a migrated register: an object ``reg/<course>/[ev/|receipt/]<slug>`` PART_OF the course."""
    conn = nd.conn
    _require_editable(conn, course_oid)
    if _is_agent(actor) and not nd.agent_writes:
        raise NativeError(Status.AUTHORITY_DENIED, "agents cannot add register rows here")
    obj_type = (obj_type or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z_]{1,30}", obj_type) or obj_type == "course":
        raise NativeError(Status.INVALID_REQUEST, f"object type {obj_type!r}: a short lowercase word")
    local_id = (local_id or "").strip()
    if not local_id:
        raise NativeError(Status.INVALID_REQUEST, "a row needs an id")
    aspect, sub, id_cell = NEW_ROW.get(obj_type, ("register_row", "", "object_id"))
    oid = f"{course_oid}/{sub}{_slug(local_id)}"
    if conn.execute("SELECT 1 FROM objects WHERE object_id = ?", (oid,)).fetchone():
        raise NativeError(Status.INVALID_REQUEST, f"{oid!r} already exists")
    scope = conn.execute("SELECT scope FROM objects WHERE object_id = ?", (course_oid,)).fetchone()[0]
    row = {k: ("" if v is None else str(v)) for k, v in (fields or {}).items()}
    row.setdefault(id_cell, local_id)
    if aspect == "register_row" and obj_type != "evidence":
        row.setdefault("object_type", obj_type.upper())
        row.setdefault("course_ref", course_oid.split("/")[1].upper())
    name = label or (f"{local_id}｜{row['canonical_label']}" if row.get("canonical_label") else local_id)
    now = utcnow()
    was_fresh = nd._index_fresh()
    with nd._tx():
        conn.execute("INSERT INTO objects(object_id, type, display_name, scope, created_at) VALUES (?,?,?,?,?)",
                     (oid, obj_type, name, scope, now))
        conn.execute(
            "INSERT INTO authority_bindings(binding_id, target_type, target_id, field_scope, owner, scope,"
            " source_of_truth, effective_from, write_policy, migration_state) VALUES (?, 'object', ?, NULL,"
            " 'user', ?, ?, ?, ?, ?)", (_new_id("auth_"), oid, scope, f"NEXUS:{oid}", now, POLICY, MIGRATED))
        payload = {"value": _headline(aspect, row, None), "row": row, "kind": "owner_register_row",
                   "register": None, "register_version": None, "anchor": None, "source": "nexus",
                   "edited_by": actor, "edited_at": now}
        conn.execute(
            "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle, verification,"
            " revision, is_current, valid_from, source_ref_id, updated_at) VALUES (?,?,?,?,?, 'CURRENT', ?, 1, 1,"
            " ?, NULL, ?)",
            (f"{oid}:{aspect}@n1", oid, aspect, canonical_json(payload), SCHEMA,
             "UNVERIFIED" if _is_agent(actor) else "VERIFIED", now, now))
        conn.execute(
            "INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
            " provenance_refs, created_at, extraction_run) VALUES (?,?, 'PART_OF', ?, 'VERIFIED', ?, '[]', ?, ?)",
            (_new_id("rel_"), oid, course_oid, scope, now, actor))
        linked, unknown = [], []
        if aspect == "receipt":
            # same rule as the Sheet importer: a receipt is EVIDENCE_FOR each knowledge row it names
            for k in re.split(r"[;,\s]+", row.get("knowledge_ids") or ""):
                if not k:
                    continue
                target = f"{course_oid}/{_slug(k)}"
                if conn.execute("SELECT 1 FROM objects WHERE object_id = ?", (target,)).fetchone():
                    conn.execute(
                        "INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
                        " provenance_refs, created_at, extraction_run) VALUES (?,?, 'EVIDENCE_FOR', ?, ?, ?, '[]', ?, ?)",
                        (_new_id("rel_"), oid, target, "UNVERIFIED" if _is_agent(actor) else "VERIFIED", scope,
                         now, actor))
                    linked.append(target)
                else:
                    unknown.append(k)
        nd._event("AGENT_WRITE" if _is_agent(actor) else "USER_DECISION", oid,
                  {"action": "register.add_row", "aspect": aspect, "course": course_oid}, actor)
        search_index.reindex_object(conn, oid)
        nd._keep_fresh(was_fresh)
    out = {"status": Status.OK, "object_id": oid, "aspect": aspect, "display_name": name}
    if aspect == "receipt":
        out.update(evidence_for=linked, unknown_knowledge_ids=unknown)
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from .db import connect, init_schema
    ap = argparse.ArgumentParser(prog="python -m nexus.register_edit")
    ap.add_argument("--db", required=True)
    ap.add_argument("command", choices=["migrate", "status"])
    ap.add_argument("course", help="course register object id, e.g. reg/demo0020")
    a = ap.parse_args(argv)
    conn = connect(a.db)
    conn.execute("PRAGMA busy_timeout = 10000")
    init_schema(conn)
    if a.command == "migrate":
        out = migrate(NativeDocs(conn), a.course, "user")
    else:
        b = binding(conn, a.course)
        out = {"course": a.course, "binding": b}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
