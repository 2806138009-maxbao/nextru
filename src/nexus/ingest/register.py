"""Course CORE REGISTER (Google Sheet) -> Nexus typed objects, read-only mirror.

A course register is a Google Sheet whose tabs already model the course:

    01 Objects          object_id, object_type, canonical_label, ...      -> objects + ``register_row`` aspect
    02 Relations        edge_id, from_object, relation_type, to_object    -> relations (+ quoted row)
    03 Evidence         evidence_id, source_url, anchor, ...              -> ``evidence`` objects
    04 Learner_State    knowledge_id, capability_state, ...               -> ``learner_state`` Current aspect
    05 Learner Receipts receipt_id, date, knowledge_ids, ...              -> ``learner_receipt`` objects

Tabs are recognised by their header row, not their names. Tabs whose title contains ``SIM`` are
simulations/trials: their rows stay searchable as text but get no typed layer. Every other tab
(navigators, views, README) is kept as text only.

What one ``import_register`` call does:

1. Renders every tab as text (one paragraph per row, ``<n>｜<tab title>`` headings; newlines inside a
   cell become ``⏎``) and ingests it with ``ingest_document`` under ``GOOGLE_DRIVE:<sheet id>@v<Drive
   version>``. The xlsx export is archived beside the text when an archive is given. So every typed
   item below can quote the exact row it came from, and freshness works like any Drive document.
2. Replaces this course's typed layer (``reg/<course>/...``) with what the current revision says:
   objects are kept (ids are stable), earlier register aspects become SUPERSEDED, relations and their
   quotes are rebuilt. History lives in the sealed revisions of the Sheet itself.

Nothing is inferred. A row is mirrored as written; the Sheet's own ``epistemic_state`` /
``support_type`` / ``record_status`` are kept verbatim. ``verification = VERIFIED`` here means only
"recorded by the course's Owner register", not that Nexus checked the claim. A relation whose end
is not an object in the register is skipped and reported, never guessed. Nothing is written to
Google.
"""

from __future__ import annotations

import io
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ..db import Store, utcnow
from .pipeline import ingest_document
from .sections import HEADING, MAX_HEADING_CHARS, normalise
from .sources import GOOGLE_DRIVE, DriveSource, FetchedDocument, Rendition

SCHEMA = "register.v1"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
NEWLINE_MARK = " ⏎ "


@dataclass
class Tab:
    title: str
    header: list[str]
    rows: list[dict[str, str]]          # header -> cell text (empty cells omitted)
    kind: str | None = None             # objects | relations | evidence | learner_state | receipts
    heading_no: int = 0
    anchors: list[str] = field(default_factory=list)   # anchor of each row's section


@dataclass
class SheetExport:
    sheet_id: str
    name: str
    version: str
    modified_time: str | None
    web_view: str | None
    xlsx: bytes


# ----------------------------------------------------------------------------- sources
class DriveSheetSource(DriveSource):
    """Read-only: Drive metadata + xlsx export of one Google Sheet (drive.readonly is enough)."""

    def export(self) -> SheetExport:
        token = self._access_token()
        meta = json.loads(self._get(f"{self.API}{self.doc_id}?fields=id,name,modifiedTime,version,"
                                    "webViewLink,mimeType", token))
        if meta.get("mimeType") != "application/vnd.google-apps.spreadsheet":
            raise ValueError(f"{self.doc_id} is not a Google Sheet ({meta.get('mimeType')})")
        data = self._get(f"{self.API}{self.doc_id}/export?mimeType=" + urllib.parse.quote(XLSX_MIME, safe=""),
                         token)
        return SheetExport(meta["id"], meta.get("name", self.doc_id), str(meta.get("version")),
                           meta.get("modifiedTime"), meta.get("webViewLink"), data)


# ----------------------------------------------------------------------------- parsing
def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).replace("\r\n", "\n").replace("\r", "\n").strip()
    return re.sub(r"\s*\n\s*", NEWLINE_MARK, s)


def _kind(title: str, header: list[str]) -> str | None:
    if "SIM" in title.upper().split() or re.search(r"(^|[_\s])SIM($|[_\s])", title.upper()):
        return None
    h = set(header)
    if {"edge_id", "from_object", "relation_type", "to_object"} <= h:
        return "relations"
    if {"object_id", "object_type"} <= h:
        return "objects"
    if {"evidence_id", "source_url"} <= h:
        return "evidence"
    if {"knowledge_id", "capability_state"} <= h:
        return "learner_state"
    if {"receipt_id", "knowledge_ids"} <= h:
        return "receipts"
    return None


def parse_workbook(xlsx: bytes) -> list[Tab]:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx), read_only=True, data_only=True)
    tabs = []
    for ws in wb.worksheets:
        grid = [[_cell(v) for v in row] for row in ws.iter_rows(values_only=True)]
        grid = [r for r in grid if any(r)]
        if not grid:
            tabs.append(Tab(ws.title, [], []))
            continue
        header = [h or f"col{i + 1}" for i, h in enumerate(grid[0])]
        kind = _kind(ws.title, header)
        if kind is None and not any(re.fullmatch(r"[a-z][a-z0-9_]*", h) for h in grid[0] if h):
            header = []                     # free-form tab (no machine header): every row is content
            body = grid
        else:
            body = grid[1:]
        rows = []
        for r in body:
            if header:
                rows.append({header[i]: v for i, v in enumerate(r) if v and i < len(header)})
            else:
                rows.append({f"col{i + 1}": v for i, v in enumerate(r) if v})
        tabs.append(Tab(ws.title, header, [x for x in rows if x], kind))
    return tabs


def _row_text(tab: Tab, row: dict[str, str]) -> str:
    if not tab.header:
        out = " | ".join(row.values())
    else:
        first = tab.header[0]
        head = row.get(first, "")
        rest = " | ".join(f"{k}: {v}" for k, v in row.items() if k != first)
        out = f"{head} | {rest}" if head and rest else (head or rest)
    if len(out) <= MAX_HEADING_CHARS and HEADING.match(out):
        out = "· " + out                    # a row must never be read as a section heading
    return out


def render(export: SheetExport, tabs: list[Tab]) -> str:
    lines = [f"REGISTER｜{export.name}",
             f"Google Sheet {export.sheet_id} · Drive version {export.version} · modified {export.modified_time}"
             " · 只读镜像：每行一段，⏎ 表示单元格内换行；标题含 SIM 的标签页是模拟/试验数据，不是 Current。"]
    for n, tab in enumerate(tabs, start=1):
        tab.heading_no = n
        lines.append(f"{n}｜{tab.title}" + ("（SIM，非 Current）" if tab.kind is None and "SIM" in tab.title.upper()
                                            else ""))
        if tab.header:
            lines.append("列：" + " / ".join(tab.header))
        for row in tab.rows:
            lines.append(_row_text(tab, row))
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------- import
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:\-]+", "_", s.strip())


def import_register(store: Store, export: SheetExport, scope: str, course: str,
                    archive=None) -> dict[str, Any]:
    tabs = parse_workbook(export.xlsx)
    text = render(export, tabs)
    doc = FetchedDocument(GOOGLE_DRIVE, export.sheet_id, export.name, export.version, export.modified_time,
                          export.web_view, text, raw=text.encode("utf-8"),
                          renditions=(Rendition("export.xlsx", XLSX_MIME, export.xlsx),),
                          notes=("Google Sheet: text rendered one row per paragraph from the xlsx export",))
    course_key = course.lower()
    prefix = f"reg/{course_key}"
    moved = store.conn.execute(
        "SELECT effective_from FROM authority_bindings WHERE target_id = ? AND field_scope IS NULL"
        " AND effective_to IS NULL AND migration_state = 'MIGRATED'", (prefix,)).fetchone()
    if moved:
        # nexus.register_edit.migrate made Nexus the source; re-importing would retire every row and
        # rebuild the relations from the Sheet, silently dropping what was edited in Nexus since.
        raise ValueError(f"{prefix} was migrated to Nexus at {moved[0]}; the Sheet is its pre-cutover "
                         "archive and is not imported again")
    doc_oid = f"doc/{export.sheet_id[:16]}"
    ing = ingest_document(store, doc, scope, object_id=doc_oid, object_type="register", archive=archive)
    ref = ing["source_ref_id"]
    c = store.conn

    marker = c.execute("SELECT payload FROM aspects WHERE object_id = ? AND name = 'register_import'"
                       " AND is_current = 1", (prefix,)).fetchone()
    if ing["status"] == "UNCHANGED" and marker and json.loads(marker["payload"]).get("source_ref_id") == ref:
        return {"status": "UNCHANGED", "course": course, "source_ref_id": ref}

    # anchors: row i of tab n is section h<n>.<k>; the optional "列：" line comes first.
    secs = {}
    for r in c.execute("SELECT anchor, body FROM source_sections WHERE source_ref_id = ?", (ref,)):
        m = re.match(r"h(\d+)\.(\d+)-", r["anchor"])
        if m:
            secs[(int(m.group(1)), int(m.group(2)))] = (r["anchor"], r["body"])
    for tab in tabs:
        offset = 1 if tab.header else 0
        tab.anchors = []
        for i, row in enumerate(tab.rows, start=1):
            a = secs.get((tab.heading_no, i + offset))
            if a is None or a[1] != normalise(_row_text(tab, row)):
                raise RuntimeError(f"row {i} of tab {tab.title!r} did not land in its own section")
            tab.anchors.append(a[0])

    now = utcnow()
    tag = ref.rsplit("@", 1)[1]
    report: dict[str, Any] = {"status": "IMPORTED", "course": course, "source_ref_id": ref,
                              "document": ing["status"], "tabs": {}, "skipped": []}
    by_kind: dict[str, Tab] = {}
    for t in tabs:
        if t.kind and t.kind not in by_kind:
            by_kind[t.kind] = t
    for t in tabs:
        report["tabs"][t.title] = {"rows": len(t.rows), "typed_as": t.kind}
    if sum(1 for t in tabs if t.kind) != len(by_kind):
        dup = [t.title for t in tabs if t.kind and by_kind[t.kind] is not t]
        report["skipped"].append({"tabs": dup, "reason": "second tab with the same header kind; first one used"})

    def oid_of(local: str) -> str:
        return f"{prefix}/{_slug(local)}"

    def quote(tab: Tab, i: int) -> tuple[str, str]:
        return tab.anchors[i], normalise(_row_text(tab, tab.rows[i]))

    def aspect(oid, name, value, row, anchor, extra=None):
        payload = {"value": value, "row": row, "kind": "owner_register_row", "register": export.sheet_id,
                   "register_version": export.version, "anchor": anchor, **(extra or {})}
        prior = c.execute("SELECT MAX(revision) AS r FROM aspects WHERE object_id = ? AND name = ?",
                          (oid, name)).fetchone()["r"] or 0
        store.add_aspect(f"{oid}:{name}@{tag}", oid, name, payload, SCHEMA, "CURRENT", "VERIFIED", prior + 1, True,
                         export.modified_time, None, ref, now)

    def relation(rid, frm, typ, to, anchor, text, detail=None):
        store.add_relation(rid, frm, typ, to, "VERIFIED", scope, [ref])
        store.add_observation(f"obs/{rid}@{tag}", frm, "relation_evidence", text, "SUPPORTS",
                              json.dumps({"relation_id": rid, "anchor": anchor, "note": "CORE REGISTER 原行",
                                          **(detail or {})}, ensure_ascii=False), [ref])

    with store.tx():
        # retire the previous typed layer of this course
        c.execute("UPDATE aspects SET is_current = 0, lifecycle = 'SUPERSEDED', valid_to = ?"
                  " WHERE schema_version = ? AND (object_id = ? OR object_id LIKE ?) AND is_current = 1",
                  (now, SCHEMA, prefix, prefix + "/%"))
        old = [r[0] for r in c.execute("SELECT relation_id FROM relations WHERE relation_id LIKE ?", (prefix + "/%",))]
        for rid in old:
            c.execute("DELETE FROM observations WHERE dimension = 'relation_evidence' AND detail LIKE ?",
                      (f'%"relation_id": "{rid}"%',))
        c.execute("DELETE FROM relations WHERE relation_id LIKE ?", (prefix + "/%",))

        def ensure(oid, typ, name):
            if store.get_object(oid) is None:
                store.add_object(oid, typ, name, scope, now)
            else:
                c.execute("UPDATE objects SET type = ?, display_name = ? WHERE object_id = ?", (typ, name, oid))
            if c.execute("SELECT 1 FROM authority_bindings WHERE target_id = ?", (oid,)).fetchone() is None:
                store.add_authority(f"auth/{oid}", "object", oid, None, "user", scope,
                                    f"GOOGLE_DRIVE:{export.sheet_id}", export.modified_time or now,
                                    "READ_ONLY_MIRROR", "MIRRORED")

        ensure(prefix, "course", f"{course}｜课程（CORE REGISTER）")
        aspect(prefix, "register_import", {"rows": {t.title: len(t.rows) for t in tabs}}, None, None,
               {"source_ref_id": ref, "register_name": export.name, "modified_time": export.modified_time})
        relation(f"{prefix}/register", doc_oid, "REGISTER_OF", prefix, None,
                 f"{export.name}（Drive version {export.version}）")

        known: dict[str, str] = {}
        t = by_kind.get("objects")
        if t:
            for i, row in enumerate(t.rows):
                local = row.get("object_id")
                if not local:
                    report["skipped"].append({"tab": t.title, "row": i + 1, "reason": "no object_id"})
                    continue
                oid = oid_of(local)
                known[local] = oid
                anchor, text = quote(t, i)
                ensure(oid, (row.get("object_type") or "object").lower(),
                       f"{local}｜{row.get('canonical_label', '')}".rstrip("｜"))
                aspect(oid, "register_row", row.get("epistemic_state") or row.get("processing_state"), row, anchor)
                relation(f"{prefix}/part_of/{_slug(local)}", oid, "PART_OF", prefix, anchor, text)

        t = by_kind.get("evidence")
        evidence: dict[str, str] = {}
        if t:
            for i, row in enumerate(t.rows):
                local = row.get("evidence_id")
                if not local:
                    continue
                oid = f"{prefix}/ev/{_slug(local)}"
                evidence[local] = oid
                anchor, _ = quote(t, i)
                ensure(oid, "evidence", f"{local}｜{row.get('anchor', '')}".rstrip("｜"))
                aspect(oid, "register_row", row.get("evidence_depth"), row, anchor,
                       {"external_source": row.get("source_url"), "external_anchor": row.get("anchor")})

        def link_evidence(frm, refs_cell, anchor, text, rid_base):
            for e in re.split(r"[;,\s]+", refs_cell or ""):
                if e in evidence:
                    relation(f"{rid_base}/evidenced_by/{_slug(e)}", frm, "EVIDENCED_BY", evidence[e], anchor, text)

        if by_kind.get("objects"):
            t = by_kind["objects"]
            for i, row in enumerate(t.rows):
                if row.get("object_id") in known:
                    anchor, text = quote(t, i)
                    link_evidence(known[row["object_id"]], row.get("evidence_refs"), anchor, text,
                                  f"{prefix}/obj/{_slug(row['object_id'])}")

        t = by_kind.get("relations")
        if t:
            for i, row in enumerate(t.rows):
                eid, frm, typ, to = (row.get(k) for k in ("edge_id", "from_object", "relation_type", "to_object"))
                if not (eid and frm and typ and to):
                    report["skipped"].append({"tab": t.title, "row": i + 1, "reason": "incomplete edge"})
                    continue
                if frm not in known or to not in known:
                    report["skipped"].append({"tab": t.title, "edge": eid, "reason": "endpoint not in Objects tab",
                                              "missing": [x for x in (frm, to) if x not in known]})
                    continue
                anchor, text = quote(t, i)
                relation(f"{prefix}/{_slug(eid)}", known[frm], _slug(typ).upper(), known[to], anchor, text,
                         {k: row.get(k) for k in ("support_type", "epistemic_state", "scope", "rationale",
                                                  "evidence_refs") if row.get(k)})

        t = by_kind.get("learner_state")
        if t:
            for i, row in enumerate(t.rows):
                k = row.get("knowledge_id")
                if k not in known:
                    report["skipped"].append({"tab": t.title, "row": i + 1, "reason": "knowledge_id not in Objects",
                                              "id": k})
                    continue
                anchor, _ = quote(t, i)
                aspect(known[k], "learner_state", row.get("capability_state"), row, anchor,
                       {"note": "Learner_State 是该课唯一正式学习 Current；Nexus 只镜像，不在这里改"})

        t = by_kind.get("receipts")
        if t:
            for i, row in enumerate(t.rows):
                rid = row.get("receipt_id")
                if not rid:
                    continue
                oid = f"{prefix}/receipt/{_slug(rid)}"
                anchor, text = quote(t, i)
                ensure(oid, "learner_receipt", f"{rid}｜{row.get('date', '')}｜{row.get('evidence_class', '')}")
                aspect(oid, "receipt", row.get("evidence_class"), row, anchor, {"date": row.get("date")})
                for k in re.split(r"[;,\s]+", row.get("knowledge_ids") or ""):
                    if k in known:
                        relation(f"{prefix}/receipt/{_slug(rid)}/evidence_for/{_slug(k)}", oid, "EVIDENCE_FOR",
                                 known[k], anchor, text)
                    elif k:
                        report["skipped"].append({"tab": t.title, "receipt": rid, "reason": "unknown knowledge id",
                                                  "id": k})

    report["objects"] = c.execute("SELECT COUNT(*) FROM objects WHERE object_id = ? OR object_id LIKE ?",
                                  (prefix, prefix + "/%")).fetchone()[0]
    report["relations"] = c.execute("SELECT COUNT(*) FROM relations WHERE relation_id LIKE ?",
                                    (prefix + "/%",)).fetchone()[0]
    report["current_aspects"] = dict(c.execute(
        "SELECT name, COUNT(*) FROM aspects WHERE schema_version = ? AND is_current = 1"
        " AND (object_id = ? OR object_id LIKE ?) GROUP BY name", (SCHEMA, prefix, prefix + "/%")).fetchall())
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    from ..db import connect, init_schema

    p = argparse.ArgumentParser(prog="python -m nexus.ingest.register",
                                description="Mirror a course CORE REGISTER Google Sheet into Nexus (read-only).")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--course", required=True, help="course code, e.g. DEMO0020 (objects become reg/<course>/...)")
    p.add_argument("--sheet", help="Google Sheet id (needs --token-dir)")
    p.add_argument("--token-dir")
    p.add_argument("--xlsx", help="local xlsx export instead of Drive (with --meta)")
    p.add_argument("--meta", help="JSON {sheet_id, name, version, modified_time} for --xlsx")
    p.add_argument("--archive")
    a = p.parse_args(argv)
    if a.sheet:
        if not a.token_dir:
            p.error("--sheet needs --token-dir")
        export = DriveSheetSource(a.sheet, a.token_dir).export()
    elif a.xlsx and a.meta:
        m = json.loads(Path(a.meta).read_text(encoding="utf-8"))
        export = SheetExport(m["sheet_id"], m.get("name", m["sheet_id"]), str(m["version"]), m.get("modified_time"),
                             m.get("web_view"), Path(a.xlsx).read_bytes())
    else:
        p.error("give --sheet or --xlsx with --meta")
    archive = None
    if a.archive:
        from ..archive import Archive
        archive = Archive(a.archive)
    conn = connect(a.db)
    init_schema(conn)
    rep = import_register(Store(conn), export, a.scope, a.course, archive=archive)
    if rep["status"] == "IMPORTED":
        from ..search import ensure_index
        ensure_index(conn)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
