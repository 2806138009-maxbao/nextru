"""Record each mirrored document's self-declared status as its own aspect (Spine §4: one axis per field).

    python -m nexus.ingest.doc_status --db <db> --scope <scope> [--dry-run]

The imported ``content`` aspect stays DRAFT/UNVERIFIED on purpose: an imported snapshot is never
Current by itself (Source-Owned Authority). What a document says about *itself* - its Status line,
``状态：``, ``mission_state`` / ``plan_state`` / ``object_type`` / ``authority_class`` header fields
and its title tags - is a separate axis. This importer copies those declarations verbatim (with the
paragraph anchor) into a ``doc_status`` aspect and derives one small field from them:

    currency = WITHDRAWN | SUPERSEDED | HISTORICAL | DRAFT | CURRENT | UNDECLARED

by keyword rules over the first status token of each declaration and the title's tags; when header
and title disagree, the more retired state wins (an obsolete document relabelled in its title has
left Current, Spine §9.7). A document that declares nothing is UNDECLARED - never guessed.

It also records supersession that a retired document declares about itself: a header paragraph of a
HISTORICAL/SUPERSEDED/WITHDRAWN document whose pointer words ("superseded by / 有效 / 现行 / Current")
are directly followed (within 60 characters) by a link to another document in Nexus becomes ``<that document> -SUPERSEDES-> <this document>``,
quoting the paragraph. Writer ``import:doc-status``; re-running replaces the aspect only when the
declarations changed.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from ..db import canonical_json, connect, init_schema, utcnow
from ..native import NativeDocs, NativeError
from .. import search as search_index

ACTOR = "import:doc-status"
HEAD = 12
_FIELD = re.compile(r"^\s*(?:[-*]\s*)?(object_type|mission_state|plan_state|status|状态|authority_class)\s*[:：]\s*(.+)",
                    re.I)
_RULES = [  # most retired first; matched against one status token at a time
    ("WITHDRAWN", re.compile(r"\bWITHDRAWN\b|NOT ADOPTED|NO AUTHORITY|已撤回|撤销", re.I)),
    ("SUPERSEDED", re.compile(r"\bSUPERSEDED\b(?!\s+EARLIER)|已被.{0,8}取代", re.I)),
    ("HISTORICAL", re.compile(r"\bHISTORICAL\b|\bHISTORY\b|\bRETIRED\b|\bCLOSED\b|\bARCHIVED\b|\bINACTIVE\b"
                              r"|NOT ACTIVE|历史|已归档|退出当前", re.I)),
    ("DRAFT", re.compile(r"\bDRAFT\b|\bCANDIDATE\b|\bPROPOSED\b|\bPROPOSAL\b|\bWORKING\b|STUDY ONLY|草稿|候选", re.I)),
    ("CURRENT", re.compile(r"\bADOPTED\b|\bACTIVE\b|\bLIVING\b|\bFROZEN\b|\bCANONICAL\b|\bCURRENT\b|\bFORMAL\b"
                           r"|\bAUTHORITATIVE\b|\bFINAL\b|\bBLOCKED\b|CLOSEOUT_PENDING|现行|生效", re.I)),
]
_ORDER = [r[0] for r in _RULES] + ["UNDECLARED"]
_LINK = re.compile(r"docs\.google\.com/(?:document|spreadsheets|presentation)/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})")
_POINTS_TO_CURRENT = re.compile(r"superseded\s*by|SUPERSEDED_BY|有效|现行|Current\s*(?:Owner|指针|pointer|版本)|取代为|改由",
                                re.I)


_NEGATED = re.compile(r"\b(?:NOT|NO|NON)[- ](?:(?:A|AN|THE|YET)\s+)?[\w-]+", re.I)


def _currency(text: str) -> str | None:
    """Classify the first status token that carries a known state (tokens split on / · ; 。|).
    Retired states are matched on the raw token ("NOT ADOPTED", "NOT ACTIVE" are retirement words);
    DRAFT / CURRENT words are ignored when negated ("NON-AUTHORITATIVE", "NO CURRENT ...")."""
    for tok in re.split(r"[/·;；。|｜]", text)[:4]:
        for state, rx in _RULES:
            probe = tok if state in ("WITHDRAWN", "SUPERSEDED", "HISTORICAL") else _NEGATED.sub(" ", tok)
            if rx.search(probe):
                return state
    return None


def _title_tags(name: str) -> list[str]:
    return [t.strip() for t in name.split("｜") if t.strip() and re.fullmatch(r"[A-Z0-9 _/&-]{3,}", t.strip())]


def declarations(name: str, head: list[tuple[str, str]]) -> dict:
    found: list[dict] = []
    for i, (anchor, text) in enumerate(head):
        for part in re.split(r"\s+\|\s+", text):
            m = _FIELD.match(part)
            if m:
                found.append({"field": m.group(1).lower(), "text": m.group(2).strip()[:300], "anchor": anchor})
        if text.strip() in ("Status", "状态", "STATUS") and i + 1 < len(head):
            found.append({"field": "status", "text": head[i + 1][1].strip()[:300], "anchor": head[i + 1][0]})
    out: dict = {"declared": found, "title_tags": _title_tags(name)}
    for f in ("object_type", "mission_state", "plan_state", "authority_class"):
        v = next((d["text"] for d in found if d["field"] == f), None)
        if v:
            out[f] = re.split(r"[\s|；;。]", v, maxsplit=1)[0]
    states: list[tuple[str, str]] = []
    status = next((d for d in found if d["field"] in ("status", "状态", "mission_state", "plan_state")), None)
    if status and _currency(status["text"]):
        states.append((_currency(status["text"]), f"header:{status['field']}@{status['anchor']}"))
    for tag in out["title_tags"]:
        st = _currency(tag)
        if st:
            states.append((st, f"title:{tag}"))
            break
    if states:
        cur, basis = min(states, key=lambda s: _ORDER.index(s[0]))
    else:
        cur, basis = "UNDECLARED", "nothing declared"
    out["currency"], out["currency_basis"] = cur, basis
    return out


class _Run:
    def __init__(self, conn, scope: str, dry: bool):
        self.conn, self.scope, self.dry = conn, scope, dry
        self.n = NativeDocs(conn)
        self.stats = {"documents": 0, "written": 0, "unchanged": 0, "by_currency": {}, "supersedes": 0,
                      "supersedes_existing": 0, "errors": []}

    def head(self, oid: str) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT statement, detail, source_ref_ids FROM observations WHERE object_id = ? AND"
            " dimension = 'document_paragraph' ORDER BY observation_id", (oid,)).fetchall()
        out = []
        for s, d, _r in rows:
            a = json.loads(d or "{}").get("anchor") or ""
            if a.startswith("h0.") and len(out) < HEAD:
                out.append((a, s or ""))
        return out

    def ref(self, oid: str) -> str | None:
        r = self.conn.execute("SELECT source_ref_ids FROM observations WHERE object_id = ? AND"
                              " dimension = 'document_paragraph' LIMIT 1", (oid,)).fetchone()
        return json.loads(r[0])[0] if r else None

    def write_aspect(self, oid: str, payload: dict) -> None:
        cur = self.conn.execute("SELECT payload, revision FROM aspects WHERE object_id = ? AND name = 'doc_status'"
                                " AND is_current = 1", (oid,)).fetchone()
        if cur and json.loads(cur[0]) == payload:
            self.stats["unchanged"] += 1
            return
        self.stats["written"] += 1
        if self.dry:
            return
        now, rev = utcnow(), (cur[1] + 1 if cur else 1)
        if cur:
            self.conn.execute("UPDATE aspects SET is_current = 0, valid_to = ? WHERE object_id = ? AND"
                              " name = 'doc_status' AND is_current = 1", (now, oid))
        self.conn.execute(
            "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle, verification,"
            " revision, is_current, valid_from, source_ref_id, updated_at) VALUES (?,?, 'doc_status', ?,"
            " 'nexus-doc-status-1', 'CURRENT', 'VERIFIED', ?, 1, ?, ?, ?)",
            (f"{oid}:doc_status@{rev}", oid, canonical_json(payload), rev, now, self.ref(oid), now))
        self.conn.execute("DELETE FROM search_index WHERE item_id = ?", (f"aspect:{oid}/doc_status",))
        rows = [r for r in search_index._aspect_rows(self.conn, oid) if r[2] == f"aspect:{oid}/doc_status"]
        self.conn.executemany("INSERT INTO search_index(body, kind, item_id, object_id, scope, source_ref_id,"
                              " anchor) VALUES (?,?,?,?,?,?,?)", rows)

    def supersession(self, oid: str, payload: dict, head: list[tuple[str, str]]) -> None:
        if payload["currency"] not in ("HISTORICAL", "SUPERSEDED", "WITHDRAWN"):
            return
        for anchor, text in head:
            if not _POINTS_TO_CURRENT.search(text):
                continue
            # the document linked right after the pointer words ("有效 DLC Mission：<link>")
            new = None
            for kw in _POINTS_TO_CURRENT.finditer(text):
                link = _LINK.search(text, kw.end())
                if link and link.start() - kw.end() <= 60:
                    cand = "doc/" + link.group(1)[:16]
                    if cand != oid and self.conn.execute(
                            "SELECT 1 FROM objects WHERE object_id = ? AND scope = ?", (cand, self.scope)).fetchone():
                        new = cand
                        break
            if new is None:
                continue
            if self.conn.execute("SELECT 1 FROM relations WHERE from_id = ? AND relation_type = 'SUPERSEDES'"
                                 " AND to_id = ? AND expired_at IS NULL", (new, oid)).fetchone():
                self.stats["supersedes_existing"] += 1
                payload.setdefault("superseded_by", new)
                return
            self.stats["supersedes"] += 1
            payload.setdefault("superseded_by", new)
            if not self.dry:
                try:
                    self.n.add_relation(self.scope, new, "SUPERSEDES", oid, ACTOR,
                                        evidence=[{"object_id": oid, "anchor": anchor}],
                                        note="declared by the superseded document itself")
                except NativeError as e:
                    self.stats["errors"].append([oid, e.code, e.message[:80]])
            return

    def run(self) -> dict:
        fresh_before = self.n._index_fresh()
        docs = self.conn.execute("SELECT object_id, display_name FROM objects WHERE type = 'document' AND scope = ?",
                                 (self.scope,)).fetchall()
        for oid, name in docs:
            head = self.head(oid)
            payload = declarations(name or "", head)
            self.supersession(oid, payload, head)
            payload["extracted_by"] = ACTOR
            self.stats["documents"] += 1
            self.stats["by_currency"][payload["currency"]] = self.stats["by_currency"].get(payload["currency"], 0) + 1
            self.write_aspect(oid, payload)
        if not self.dry:
            if fresh_before:
                self.n._keep_fresh(True)
            self.conn.commit()
        return self.stats


def import_doc_status(conn, scope: str, dry_run: bool = False) -> dict:
    return _Run(conn, scope, dry_run).run()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.ingest.doc_status")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    conn = connect(a.db)
    init_schema(conn)
    out = import_doc_status(conn, a.scope, a.dry_run)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return 0 if not out["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
