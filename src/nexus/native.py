"""Nexus-native knowledge documents: STORAGE x WRITE v0.

A Drive mirror (source_refs / source_sections) is a sealed, read-only copy whose Authority stays in
Drive. A *native* document is owned by Nexus itself:

* Document -> Section -> Paragraph tree; every node has a stable id that survives edits.
* Every write makes a new revision (append-only tables); old revisions stay fully readable.
* Writes carry ``expected_revision``; a write based on an old revision is refused
  (VERSION_CONFLICT). The check is a compare-and-swap inside ``BEGIN IMMEDIATE``, so it also holds
  between separate processes.
* Agents (``agent:<name>``) write directly when agent_writes is on (otherwise they only propose).
  Their relations and entities must cite a source; review.py decides how far each may be leaned
  on (policy-confirmed filing, UNVERIFIED lead, or check_source interpretation). No owner
  confirmation is required; the owner can still accept or reject any proposal.
* Drive-mirror objects are refused with MIRROR_READ_ONLY.
* The search index is updated for the touched document only, in the same call.
* Restore = a new revision whose tree equals an older one (history is never rewritten).
* Backup = SQLite online backup + manifest (sha256, integrity_check, counts); restore goes to a
  new file and is verified.

CLI: ``python -m nexus.native --db <path> <command> ...`` (the CLI acts as the owner, ``user``).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from .db import canonical_json, connect, init_schema, sha256_text, utcnow
from .errors import Status
from . import review
from . import search as search_index

OWNER = "user"
DOC_TYPE = "native_document"
SOURCE_SYSTEM = "NEXUS"
KINDS = ("section", "paragraph")


class NativeError(Exception):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def as_dict(self) -> dict:
        out = {"status": self.code, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _is_agent(actor: str) -> bool:
    return actor.startswith("agent:")


_ENTITY_TYPE = re.compile(r"^[a-z][a-z_]{1,23}$")
# types that belong to imported or native documents, not to entities
_RESERVED_TYPES = {"document", DOC_TYPE, "creator", "video", "course", "knowledge"}


def _norm_alias(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


class NativeDocs:
    """``agent_writes=True``: agents (``agent:<name>``) write directly, like the owner. Every write is
    still a new revision, so any of it can be undone (restore a revision, archive a document,
    remove a relation). With the default False, agents are refused and must use proposals."""

    def __init__(self, conn: sqlite3.Connection, agent_writes: bool = False):
        self.conn = conn
        self.agent_writes = agent_writes
        self._last_refs: dict[str, str] = {}

    # ------------------------------------------------------------ helpers
    def _tx(self):
        conn = self.conn

        class _T:
            def __enter__(self_):
                conn.execute("BEGIN IMMEDIATE")
                return conn

            def __exit__(self_, et, e, tb):
                conn.execute("COMMIT" if et is None else "ROLLBACK")
                return False
        return _T()

    def _doc(self, doc_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM native_documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if row:
            return dict(row)
        obj = self.conn.execute("SELECT object_id, type FROM objects WHERE object_id = ?",
                                (doc_id,)).fetchone()
        if obj is not None:
            mirror = self.conn.execute(
                "SELECT s.source_system, s.native_id FROM aspects a JOIN source_refs s"
                " ON s.source_ref_id = a.source_ref_id WHERE a.object_id = ? LIMIT 1",
                (doc_id,)).fetchone()
            raise NativeError(
                Status.MIRROR_READ_ONLY,
                f"{doc_id!r} is not a Nexus-native document; mirrored sources are read-only here "
                "and keep their Authority at the source",
                object_type=obj["type"],
                source_system=mirror["source_system"] if mirror else None)
        raise NativeError(Status.NOT_FOUND, f"no native document {doc_id!r}")

    def _check_scope(self, doc: dict, scope: str | None) -> None:
        if scope is not None and doc["scope"] != scope:
            raise NativeError(Status.NOT_FOUND, f"no native document {doc['doc_id']!r} in scope {scope!r}")

    def _check_owner(self, doc_id: str, actor: str) -> None:
        b = self.conn.execute(
            "SELECT owner, write_policy FROM authority_bindings WHERE target_type = 'object'"
            " AND target_id = ? AND field_scope IS NULL AND effective_to IS NULL", (doc_id,)).fetchone()
        owner = b["owner"] if b else OWNER
        if _is_agent(actor) and self.agent_writes:
            return
        if _is_agent(actor) or actor != owner:
            raise NativeError(Status.AUTHORITY_DENIED,
                              f"{actor!r} cannot commit to {doc_id!r}; owner is {owner!r}. "
                              "Agents submit a proposal instead.",
                              owner=owner, write_policy=b["write_policy"] if b else None)

    def _blob(self, text: str) -> str:
        sha = sha256_text(text)
        self.conn.execute("INSERT OR IGNORE INTO native_blobs(sha256, body) VALUES (?,?)", (sha, text))
        return sha

    def _load_tree(self, doc_id: str, revision: int) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT t.node_id, t.parent_id, t.position, t.kind, t.title, b.body FROM native_tree t"
            " LEFT JOIN native_blobs b ON b.sha256 = t.body_sha256"
            " WHERE t.doc_id = ? AND t.revision = ?", (doc_id, revision)).fetchall()
        return {r["node_id"]: dict(r) for r in rows}

    @staticmethod
    def _ordered(tree: dict[str, dict]) -> list[dict]:
        """Nodes in document order (depth-first), each with depth and section path."""
        kids: dict[str | None, list[dict]] = {}
        for n in tree.values():
            kids.setdefault(n["parent_id"], []).append(n)
        for v in kids.values():
            v.sort(key=lambda n: n["position"])
        out: list[dict] = []

        def walk(parent: str | None, depth: int, path: list[str]) -> None:
            for n in kids.get(parent, []):
                out.append({**n, "depth": depth, "path": path})
                if n["kind"] == "section":
                    walk(n["node_id"], depth + 1, path + [n["title"] or ""])
        walk(None, 0, [])
        return out

    @staticmethod
    def _tree_hash(title: str, tree: dict[str, dict]) -> str:
        nodes = sorted((n["node_id"], n["parent_id"], n["position"], n["kind"], n["title"],
                        sha256_text(n["body"]) if n.get("body") is not None else None)
                       for n in tree.values())
        return sha256_text(canonical_json({"title": title, "nodes": nodes}))

    def _write_revision(self, doc_id: str, revision: int, base: int | None, title: str,
                        tree: dict[str, dict], actor: str, message: str | None,
                        restored_from: int | None = None, proposal_id: str | None = None) -> str:
        th = self._tree_hash(title, tree)
        self.conn.execute(
            "INSERT INTO native_revisions(doc_id, revision, base_revision, title, author, message,"
            " created_at, restored_from, proposal_id, tree_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc_id, revision, base, title, actor, message, utcnow(), restored_from, proposal_id, th))
        for n in tree.values():
            sha = self._blob(n["body"]) if n["kind"] == "paragraph" else None
            self.conn.execute(
                "INSERT INTO native_tree(doc_id, revision, node_id, parent_id, position, kind, title,"
                " body_sha256) VALUES (?,?,?,?,?,?,?,?)",
                (doc_id, revision, n["node_id"], n["parent_id"], n["position"], n["kind"],
                 n.get("title"), sha))
        return th

    def _index_fresh(self) -> bool:
        self.conn.executescript(search_index.DDL)
        row = self.conn.execute("SELECT value FROM search_meta WHERE key = 'fingerprint'").fetchone()
        return bool(row) and row[0] == search_index._fingerprint(self.conn)

    def _reindex(self, doc_id: str, was_fresh: bool) -> int:
        """Replace this document's rows in the search index; keep the index fingerprint valid so
        the next search does not rebuild the whole index for one native write."""
        n = search_index.reindex_native(self.conn, doc_id)
        self._keep_fresh(was_fresh)
        return n

    def _keep_fresh(self, was_fresh: bool) -> None:
        """A write that adds rows the index fingerprint counts (objects, source_refs) but no
        searchable text must not force a full rebuild on the next search."""
        if was_fresh:
            self.conn.execute("INSERT OR REPLACE INTO search_meta(key, value) VALUES ('fingerprint', ?)",
                              (search_index._fingerprint(self.conn),))

    def _advance(self, doc_id: str, expected: int) -> int:
        cur = self.conn.execute(
            "UPDATE native_documents SET current_revision = current_revision + 1, updated_at = ?"
            " WHERE doc_id = ? AND current_revision = ?", (utcnow(), doc_id, expected))
        if cur.rowcount != 1:
            now = self.conn.execute("SELECT current_revision FROM native_documents WHERE doc_id = ?",
                                    (doc_id,)).fetchone()[0]
            raise NativeError(Status.VERSION_CONFLICT,
                              f"{doc_id!r} is at revision {now}, not {expected}; re-read and redo the change",
                              expected_revision=expected, current_revision=now)
        return expected + 1

    def _event(self, event_type: str, doc_id: str | None, payload: dict, actor: str) -> None:
        if _is_agent(actor) and event_type == "USER_DECISION":
            event_type = "AGENT_WRITE"
        eid = _new_id("ev_")
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, occurred_at, object_id, payload, authority_owner,"
            " dedupe_key, processed_at) VALUES (?,?,?,?,?,?,?,?)",
            (eid, event_type, utcnow(), doc_id, canonical_json(payload), actor, eid, utcnow()))

    # ------------------------------------------------------------ create
    def _build_tree(self, spec: dict) -> tuple[str, dict[str, dict]]:
        title = (spec.get("title") or "").strip() if isinstance(spec, dict) else ""
        if not title:
            raise NativeError(Status.INVALID_REQUEST, "a document needs a title")
        tree: dict[str, dict] = {}

        def add(children: list, parent: str | None) -> None:
            if not isinstance(children, list):
                raise NativeError(Status.INVALID_REQUEST, "children must be a list")
            for pos, c in enumerate(children):
                if isinstance(c, str):
                    c = {"kind": "paragraph", "body": c}
                if not isinstance(c, dict):
                    raise NativeError(Status.INVALID_REQUEST, "each child is a string or an object")
                kind = c.get("kind") or ("section" if "children" in c or "title" in c else "paragraph")
                node = self._make_node(kind, parent, pos, c.get("title"), c.get("body"))
                tree[node["node_id"]] = node
                if kind == "section":
                    add(c.get("children") or [], node["node_id"])
        add(spec.get("children") or [], None)
        if not tree:
            raise NativeError(Status.INVALID_REQUEST, "a document needs at least one section or paragraph")
        return title, tree

    def create(self, scope: str, spec: dict, actor: str = OWNER, message: str | None = None,
               *, _proposal_id: str | None = None) -> dict:
        if _is_agent(actor) and not self.agent_writes:
            raise NativeError(Status.AUTHORITY_DENIED, "agents cannot create native documents directly; "
                                                       "use a document proposal")
        policy = "USER_AND_AGENTS" if self.agent_writes else "USER_ONLY"
        if self.conn.execute("SELECT 1 FROM scopes WHERE scope = ?", (scope,)).fetchone() is None:
            raise NativeError(Status.INVALID_SCOPE, f"unknown scope {scope!r}")
        title, tree = self._build_tree(spec)
        doc_id = _new_id("ndoc/")
        was_fresh = self._index_fresh()
        now = utcnow()
        with self._tx():
            self.conn.execute("INSERT INTO objects(object_id, type, display_name, scope, created_at)"
                              " VALUES (?,?,?,?,?)", (doc_id, DOC_TYPE, title, scope, now))
            self.conn.execute("INSERT INTO native_documents(doc_id, scope, current_revision, created_at,"
                              " created_by, updated_at) VALUES (?,?,1,?,?,?)", (doc_id, scope, now, actor, now))
            self.conn.execute(
                "INSERT INTO authority_bindings(binding_id, target_type, target_id, field_scope, owner,"
                " scope, source_of_truth, effective_from, write_policy, migration_state)"
                " VALUES (?, 'object', ?, NULL, ?, ?, ?, ?, ?, 'NATIVE')",
                (_new_id("auth_"), doc_id, OWNER, scope, f"{SOURCE_SYSTEM}:{doc_id}", now, policy))
            th = self._write_revision(doc_id, 1, None, title, tree, actor, message or "create",
                                      proposal_id=_proposal_id)
            self._event("USER_DECISION", doc_id, {"action": "native.create", "revision": 1,
                                                  "proposal_id": _proposal_id}, actor)
            indexed = self._reindex(doc_id, was_fresh)
        return {"status": Status.OK, "doc_id": doc_id, "revision": 1, "tree_sha256": th,
                "nodes": len(tree), "indexed_rows": indexed}

    @staticmethod
    def _make_node(kind: str, parent: str | None, pos: int, title: str | None,
                   body: str | None) -> dict:
        if kind not in KINDS:
            raise NativeError(Status.INVALID_REQUEST, f"unknown node kind {kind!r}")
        if kind == "section" and not (title or "").strip():
            raise NativeError(Status.INVALID_REQUEST, "a section needs a title")
        if kind == "paragraph" and not (body or "").strip():
            raise NativeError(Status.INVALID_REQUEST, "a paragraph needs text")
        return {"node_id": _new_id("n_"), "parent_id": parent, "position": pos, "kind": kind,
                "title": title if kind == "section" else None,
                "body": body if kind == "paragraph" else None}

    # ------------------------------------------------------------ read
    def read(self, doc_id: str, revision: int | None = None, scope: str | None = None) -> dict:
        doc = self._doc(doc_id)
        self._check_scope(doc, scope)
        rev = doc["current_revision"] if revision is None else int(revision)
        meta = self.conn.execute("SELECT * FROM native_revisions WHERE doc_id = ? AND revision = ?",
                                 (doc_id, rev)).fetchone()
        if meta is None:
            raise NativeError(Status.NOT_FOUND, f"{doc_id!r} has no revision {rev}",
                              current_revision=doc["current_revision"])
        nodes = []
        for n in self._ordered(self._load_tree(doc_id, rev)):
            item = {"node_id": n["node_id"], "kind": n["kind"], "depth": n["depth"],
                    "parent_id": n["parent_id"]}
            if n["kind"] == "section":
                item["title"] = n["title"]
            else:
                item["text"] = n["body"]
                if n["path"]:
                    item["section_path"] = " > ".join(n["path"])
            nodes.append(item)
        return {"status": Status.OK, "doc_id": doc_id, "kind": "nexus_native_document",
                "title": meta["title"], "revision": rev, "current_revision": doc["current_revision"],
                "is_current": rev == doc["current_revision"],
                "archived": bool(doc.get("archived_at")),
                "authority": {"owner": OWNER, "source_of_truth": f"{SOURCE_SYSTEM}:{doc_id}",
                              "migration_state": "NATIVE", "canon": "CANON (Nexus is the source)"},
                "revision_info": {k: meta[k] for k in ("author", "message", "created_at",
                                                       "base_revision", "restored_from", "proposal_id",
                                                       "tree_sha256")},
                "nodes": nodes,
                "edit_hint": "to change it, pass expected_revision=current_revision; node_id values "
                             "stay the same across revisions"}

    def history(self, doc_id: str, scope: str | None = None) -> dict:
        doc = self._doc(doc_id)
        self._check_scope(doc, scope)
        revs = [dict(r) for r in self.conn.execute(
            "SELECT revision, base_revision, title, author, message, created_at, restored_from,"
            " proposal_id, tree_sha256 FROM native_revisions WHERE doc_id = ? ORDER BY revision",
            (doc_id,))]
        return {"status": Status.OK, "doc_id": doc_id, "current_revision": doc["current_revision"],
                "revisions": revs}

    def diff(self, doc_id: str, a: int, b: int) -> dict:
        self._doc(doc_id)
        ta, tb = self._load_tree(doc_id, a), self._load_tree(doc_id, b)
        if not ta and not tb:
            raise NativeError(Status.NOT_FOUND, "no such revisions")
        changes = []
        for nid in sorted(set(ta) | set(tb)):
            x, y = ta.get(nid), tb.get(nid)
            if x is None:
                changes.append({"node_id": nid, "change": "added", "after": y.get("body") or y.get("title")})
            elif y is None:
                changes.append({"node_id": nid, "change": "removed", "before": x.get("body") or x.get("title")})
            else:
                for f in ("body", "title", "parent_id", "position"):
                    if x.get(f) != y.get(f):
                        changes.append({"node_id": nid, "change": f, "before": x.get(f), "after": y.get(f)})
        return {"status": Status.OK, "doc_id": doc_id, "from": a, "to": b, "changes": changes}

    # ------------------------------------------------------------ write
    def _apply_ops(self, tree: dict[str, dict], ops: list[dict]) -> tuple[dict[str, dict], list[str]]:
        tree = {k: dict(v) for k, v in tree.items()}
        created: list[str] = []
        # an insert may carry a client "ref"; later ops in the same call can use that ref as a
        # parent_id / after / node_id, so one edit can add a section together with its paragraphs
        refs: dict[str, str] = {}

        def rid(x: str | None) -> str | None:
            return refs.get(x, x) if x is not None else None

        def need(nid: str) -> dict:
            nid = rid(nid)
            if nid not in tree:
                raise NativeError(Status.NOT_FOUND, f"node {nid!r} is not in this revision")
            return tree[nid]

        def siblings(parent: str | None) -> list[dict]:
            return sorted((n for n in tree.values() if n["parent_id"] == parent), key=lambda n: n["position"])

        for op in ops or []:
            kind = op.get("op")
            if kind == "set_text":
                n = need(op.get("node_id"))
                if n["kind"] != "paragraph" or not (op.get("body") or "").strip():
                    raise NativeError(Status.INVALID_REQUEST, "set_text needs a paragraph node and text")
                n["body"] = op["body"]
            elif kind == "set_title":
                n = need(op.get("node_id"))
                if n["kind"] != "section" or not (op.get("title") or "").strip():
                    raise NativeError(Status.INVALID_REQUEST, "set_title needs a section node and a title")
                n["title"] = op["title"]
            elif kind == "insert":
                parent = rid(op.get("parent_id"))
                if parent is not None and need(parent)["kind"] != "section":
                    raise NativeError(Status.INVALID_REQUEST, "only sections can contain nodes")
                sibs = siblings(parent)
                after = rid(op.get("after"))
                ref = op.get("ref")
                if ref is not None and (ref in refs or ref in tree):
                    raise NativeError(Status.INVALID_REQUEST, f"ref {ref!r} is already in use")
                if after is not None and after not in {s["node_id"] for s in sibs}:
                    raise NativeError(Status.INVALID_REQUEST, f"{after!r} is not a child of {parent!r}")
                node = self._make_node(op.get("kind", "paragraph"), parent, 0, op.get("title"), op.get("body"))
                idx = 0 if after is None else [s["node_id"] for s in sibs].index(after) + 1
                sibs.insert(idx, node)
                for i, s in enumerate(sibs):
                    s["position"] = i
                tree[node["node_id"]] = node
                created.append(node["node_id"])
                if ref is not None:
                    refs[ref] = node["node_id"]
            elif kind == "delete":
                root = need(op.get("node_id"))
                doomed, stack = set(), [root["node_id"]]
                while stack:
                    nid = stack.pop()
                    doomed.add(nid)
                    stack += [n["node_id"] for n in tree.values() if n["parent_id"] == nid]
                for nid in doomed:
                    del tree[nid]
                for i, s in enumerate(siblings(root["parent_id"])):
                    s["position"] = i
            else:
                raise NativeError(Status.INVALID_REQUEST, f"unknown op {kind!r}",
                                  allowed=["set_text", "set_title", "insert", "delete"])
        self._last_refs = refs
        return tree, created

    def edit(self, doc_id: str, expected_revision: int, ops: list[dict], actor: str = OWNER,
             message: str | None = None, title: str | None = None, *, _proposal_id: str | None = None,
             scope: str | None = None) -> dict:
        doc = self._doc(doc_id)
        self._check_scope(doc, scope)
        self._check_owner(doc_id, actor)
        if not ops and title is None:
            raise NativeError(Status.INVALID_REQUEST, "nothing to change")
        expected = int(expected_revision)
        was_fresh = self._index_fresh()
        with self._tx():
            new_rev = self._advance(doc_id, expected)     # refuses a stale base first
            base = self._load_tree(doc_id, expected)
            tree, created = self._apply_ops(base, ops)
            old_title = self.conn.execute("SELECT title FROM native_revisions WHERE doc_id = ? AND"
                                          " revision = ?", (doc_id, expected)).fetchone()[0]
            new_title = (title or old_title).strip()
            if title is not None:
                self.conn.execute("UPDATE objects SET display_name = ? WHERE object_id = ?", (new_title, doc_id))
            th = self._write_revision(doc_id, new_rev, expected, new_title, tree, actor, message,
                                      proposal_id=_proposal_id)
            self._event("USER_DECISION", doc_id, {"action": "native.edit", "revision": new_rev,
                                                  "base_revision": expected, "ops": len(ops or []),
                                                  "proposal_id": _proposal_id}, actor)
            indexed = self._reindex(doc_id, was_fresh)
        out = {"status": Status.OK, "doc_id": doc_id, "revision": new_rev, "base_revision": expected,
               "created_nodes": created, "tree_sha256": th, "indexed_rows": indexed}
        if self._last_refs:
            out["refs"] = dict(self._last_refs)
        return out

    def restore(self, doc_id: str, to_revision: int, expected_revision: int, actor: str = OWNER,
                message: str | None = None) -> dict:
        doc = self._doc(doc_id)
        self._check_owner(doc_id, actor)
        meta = self.conn.execute("SELECT title FROM native_revisions WHERE doc_id = ? AND revision = ?",
                                 (doc_id, int(to_revision))).fetchone()
        if meta is None:
            raise NativeError(Status.NOT_FOUND, f"{doc_id!r} has no revision {to_revision}")
        was_fresh = self._index_fresh()
        with self._tx():
            new_rev = self._advance(doc_id, int(expected_revision))
            tree = self._load_tree(doc_id, int(to_revision))
            self.conn.execute("UPDATE objects SET display_name = ? WHERE object_id = ?", (meta[0], doc_id))
            th = self._write_revision(doc_id, new_rev, int(expected_revision), meta[0], tree, actor,
                                      message or f"restore revision {to_revision}",
                                      restored_from=int(to_revision))
            self._event("USER_DECISION", doc_id, {"action": "native.restore", "revision": new_rev,
                                                  "restored_from": int(to_revision)}, actor)
            indexed = self._reindex(doc_id, was_fresh)
        return {"status": Status.OK, "doc_id": doc_id, "revision": new_rev,
                "restored_from": int(to_revision), "tree_sha256": th, "indexed_rows": indexed}

    def archive(self, doc_id: str, archived: bool = True, actor: str = OWNER) -> dict:
        """Undo for a created document: it leaves search and listings but stays readable with its
        whole history (native documents are never deleted). archived=False brings it back."""
        doc = self._doc(doc_id)
        self._check_owner(doc_id, actor)
        was_fresh = self._index_fresh()
        with self._tx():
            self.conn.execute("UPDATE native_documents SET archived_at = ? WHERE doc_id = ?",
                              (utcnow() if archived else None, doc_id))
            self._event("USER_DECISION", doc_id, {"action": "native.archive" if archived
                                                  else "native.unarchive"}, actor)
            self._reindex(doc_id, was_fresh)
        return {"status": Status.OK, "doc_id": doc_id, "archived": archived,
                "revision": doc["current_revision"]}

    def add_relation(self, scope: str, from_id: str, relation_type: str, to_id: str, actor: str,
                     evidence: list | None = None, note: str | None = None) -> dict:
        """A relation written directly; remove_relation undoes it.

        The owner's relations are VERIFIED. An agent's relation is written at once too, but as an
        UNVERIFIED candidate backed by a RELATION proposal (evidence required): it is visible in
        nexus_related immediately and becomes VERIFIED when the owner accepts the proposal."""
        if _is_agent(actor) and not self.agent_writes:
            raise NativeError(Status.AUTHORITY_DENIED, "agents cannot add relations directly here")
        if _is_agent(actor):
            out = self.propose_relation(scope, from_id, relation_type, to_id, evidence or [], actor,
                                        rationale=note)
            return {**out, "written_by": actor,
                    "note": "written as UNVERIFIED; the owner can confirm it (accept the proposal)"}
        if not (relation_type or "").strip():
            raise NativeError(Status.INVALID_REQUEST, "relation_type is required")
        for oid in (from_id, to_id):
            o = self.conn.execute("SELECT scope FROM objects WHERE object_id = ?", (oid,)).fetchone()
            if o is None or o["scope"] != scope:
                raise NativeError(Status.NOT_FOUND, f"no object {oid!r} in scope {scope!r}")
        rid = _new_id("rel_")
        was_fresh = self._index_fresh()
        with self._tx():
            items = [self._evidence(scope, e) for e in (evidence or [])]
            refs = [it["ref"] for it in items]
            self.conn.execute(
                "INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
                " provenance_refs, created_at, extraction_run) VALUES (?,?,?,?, 'VERIFIED', ?,?,?,?)",
                (rid, from_id, relation_type, to_id, scope, json.dumps(refs), utcnow(), actor))
            self._record_quotes(rid, from_id, items)
            self._event("USER_DECISION", None, {"action": "native.add_relation", "relation_id": rid,
                                                "note": note}, actor)
            self._keep_fresh(was_fresh)
        return {"status": Status.OK, "relation_id": rid, "evidence": refs, "written_by": actor}

    def remove_relation(self, relation_id: str, actor: str) -> dict:
        r = self.conn.execute("SELECT extraction_run, expired_at FROM relations WHERE relation_id = ?",
                              (relation_id,)).fetchone()
        if r is None:
            raise NativeError(Status.NOT_FOUND, f"no relation {relation_id!r}")
        if _is_agent(actor) and not self.agent_writes:
            raise NativeError(Status.AUTHORITY_DENIED, "agents cannot remove relations here")
        if r["expired_at"]:
            raise NativeError(Status.INVALID_REQUEST, f"{relation_id!r} was already removed")
        with self._tx():
            self.conn.execute("UPDATE relations SET expired_at = ? WHERE relation_id = ?", (utcnow(), relation_id))
            # a still-pending proposal behind this relation is withdrawn with it
            for (pid,) in self.conn.execute("SELECT proposal_id FROM native_proposals WHERE relation_id = ?"
                                            " AND status = 'PROPOSED'", (relation_id,)).fetchall():
                self._decide(pid, "WITHDRAWN", actor, {"relation_id": relation_id, "removed": True})
            self._event("USER_DECISION", None, {"action": "native.remove_relation",
                                                "relation_id": relation_id}, actor)
        return {"status": Status.OK, "relation_id": relation_id, "removed": True}

    # ------------------------------------------------------------ entities
    def create_object(self, scope: str, type: str, name: str, actor: str, aliases: list[str] | None = None,
                      description: str | None = None, evidence: list | None = None,
                      note: str | None = None) -> dict:
        """A typed entity (person, decision, principle, path, ...) with a stable id independent of any
        file: ``ent/<type>/<id>``. Its name and aliases are searchable and relations can point at it.

        The owner's entities are VERIFIED. An agent's entity is written at once but UNVERIFIED, backed
        by an OBJECT proposal (evidence required) that the owner accepts or rejects."""
        agent = _is_agent(actor)
        if agent and not self.agent_writes:
            raise NativeError(Status.AUTHORITY_DENIED, "agents cannot create entities here")
        type = (type or "").strip().lower()
        if not _ENTITY_TYPE.match(type) or type in _RESERVED_TYPES:
            raise NativeError(Status.INVALID_REQUEST,
                              f"type {type!r}: use a short lowercase word (person, decision, principle, ...)")
        name = (name or "").strip()
        if not name:
            raise NativeError(Status.INVALID_REQUEST, "name is required")
        if agent and not evidence:
            raise NativeError(Status.INVALID_REQUEST, "an agent's entity must cite at least one source")
        if self.conn.execute("SELECT 1 FROM scopes WHERE scope = ?", (scope,)).fetchone() is None:
            raise NativeError(Status.INVALID_SCOPE, f"unknown scope {scope!r}")
        names = [name] + [a.strip() for a in (aliases or []) if a and a.strip()]
        oid = f"ent/{type}/{uuid.uuid4().hex[:10]}"
        verification = "UNVERIFIED" if agent else "VERIFIED"
        pid = _new_id("prop_") if agent else None
        now = utcnow()
        was_fresh = self._index_fresh()
        with self._tx():
            items = [self._evidence(scope, e) for e in (evidence or [])]
            refs = [it["ref"] for it in items]
            rv = review.classify_object(type, refs)
            auto = agent and rv["tier"] == review.AUTO
            if auto:
                verification = "VERIFIED"
            self.conn.execute("INSERT INTO objects(object_id, type, display_name, scope, created_at)"
                              " VALUES (?,?,?,?,?)", (oid, type, name, scope, now))
            self.conn.execute(
                "INSERT INTO authority_bindings(binding_id, target_type, target_id, field_scope, owner,"
                " scope, source_of_truth, effective_from, write_policy, migration_state)"
                " VALUES (?, 'object', ?, NULL, ?, ?, ?, ?, ?, 'NATIVE')",
                (_new_id("auth_"), oid, OWNER, scope, f"{SOURCE_SYSTEM}:{oid}", now,
                 "USER_AND_AGENTS" if self.agent_writes else "USER_ONLY"))
            payload = {"name": name, "type": type, "aliases": names[1:], "description": description,
                       "created_by": actor, "evidence": [{"source": it["ref"], "anchor": it["anchor"]}
                                                         for it in items], "proposal_id": pid}
            self.conn.execute(
                "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle,"
                " verification, revision, is_current, valid_from, source_ref_id, updated_at)"
                " VALUES (?,?, 'entity', ?, 'nexus-entity-1', 'CURRENT', ?, 1, 1, ?, ?, ?)",
                (f"{oid}:entity@1", oid, canonical_json(payload), verification, now,
                 refs[0] if refs else None, now))
            for n in dict.fromkeys(names):
                self.conn.execute("INSERT OR IGNORE INTO entity_aliases(alias_norm, scope, object_id, alias)"
                                  " VALUES (?,?,?,?)", (_norm_alias(n), scope, oid, n))
            for i, it in enumerate(items):
                if it.get("quote"):
                    self.conn.execute(
                        "INSERT INTO observations(observation_id, object_id, dimension, statement, stance,"
                        " detail, source_ref_ids, created_at) VALUES (?,?, 'entity_evidence', ?, 'SUPPORTS', ?, ?, ?)",
                        (f"obs/{oid}/evidence/{i}", oid, it["quote"],
                         json.dumps({"anchor": it.get("anchor")}, ensure_ascii=False), json.dumps([it["ref"]]), now))
            if agent:
                self.conn.execute(
                    "INSERT INTO native_proposals(proposal_id, kind, payload, rationale, evidence, proposer,"
                    " created_at, status) VALUES (?, 'OBJECT', ?,?,?,?,?, 'PROPOSED')",
                    (pid, canonical_json({"object_id": oid, "type": type, "name": name,
                                          "review": rv["tier"]}), note,
                     json.dumps(refs), actor, now))
                if auto:
                    self._decide(pid, "ACCEPTED", review.POLICY_ACTOR,
                                 {"object_id": oid, "verification": "VERIFIED", "review": rv})
            self._event("AGENT_WRITE" if agent else "USER_DECISION", oid,
                        {"action": "native.create_object", "proposal_id": pid}, actor)
            search_index.reindex_object(self.conn, oid)
            self._keep_fresh(was_fresh)
        out = {"status": Status.OK, "object_id": oid, "type": type, "name": name, "aliases": names[1:],
               "verification": verification, "evidence": refs, "written_by": actor}
        if agent:
            out.update(proposal_id=pid, review=rv["tier"],
                       note=("confirmed by policy (mirrors a Drive folder)" if auto else
                             "written as UNVERIFIED (no confirmation needed)"))
        return out

    def update_object(self, object_id: str, actor: str, *, expected_revision: int | None = None,
                      name: str | None = None, aliases: list[str] | None = None,
                      description: str | None = None) -> dict:
        """Change an entity's name, aliases (the full new list) or description. Writes a new revision
        of its ``entity`` aspect; the old one stays readable (is_current = 0). ``expected_revision``
        refuses a change based on a stale read (VERSION_CONFLICT). Verification is unchanged: editing
        an agent's entity does not by itself confirm it."""
        if not object_id.startswith("ent/"):
            raise NativeError(Status.INVALID_REQUEST, f"{object_id!r} is not an entity")
        if _is_agent(actor) and not self.agent_writes:
            raise NativeError(Status.AUTHORITY_DENIED, "agents cannot edit entities here")
        o = self.conn.execute("SELECT scope, display_name FROM objects WHERE object_id = ?",
                              (object_id,)).fetchone()
        if o is None:
            raise NativeError(Status.NOT_FOUND, f"no entity {object_id!r}")
        if name is not None and not name.strip():
            raise NativeError(Status.INVALID_REQUEST, "name cannot be empty")
        was_fresh = self._index_fresh()
        with self._tx():
            cur = self.conn.execute("SELECT * FROM aspects WHERE object_id = ? AND name = 'entity'"
                                    " AND is_current = 1", (object_id,)).fetchone()
            if cur is None:
                raise NativeError(Status.NOT_FOUND, f"{object_id!r} has no entity record")
            if expected_revision is not None and int(expected_revision) != cur["revision"]:
                raise NativeError(Status.VERSION_CONFLICT,
                                  f"{object_id!r} is at revision {cur['revision']}, not {expected_revision}",
                                  expected_revision=int(expected_revision), current_revision=cur["revision"])
            payload = json.loads(cur["payload"])
            changed = {}
            if name is not None and name.strip() != payload.get("name"):
                payload["name"] = changed["name"] = name.strip()
            if aliases is not None:
                new_aliases = list(dict.fromkeys(a.strip() for a in aliases if a and a.strip()))
                if new_aliases != (payload.get("aliases") or []):
                    payload["aliases"] = changed["aliases"] = new_aliases
            if description is not None:
                d = description.strip() or None
                if d != payload.get("description"):
                    payload["description"] = changed["description"] = d
            if not changed:
                return {"status": Status.OK, "object_id": object_id, "revision": cur["revision"],
                        "changed": []}
            rev = cur["revision"] + 1
            now = utcnow()
            payload["edited_by"] = actor
            self.conn.execute("UPDATE aspects SET is_current = 0, valid_to = ? WHERE version_id = ?",
                              (now, cur["version_id"]))
            self.conn.execute(
                "INSERT INTO aspects(version_id, object_id, name, payload, schema_version, lifecycle,"
                " verification, revision, is_current, valid_from, source_ref_id, updated_at)"
                " VALUES (?,?, 'entity', ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (f"{object_id}:entity@{rev}", object_id, canonical_json(payload), cur["schema_version"],
                 cur["lifecycle"], cur["verification"], rev, now, cur["source_ref_id"], now))
            self.conn.execute("UPDATE objects SET display_name = ? WHERE object_id = ?",
                              (payload["name"], object_id))
            self.conn.execute("DELETE FROM entity_aliases WHERE object_id = ?", (object_id,))
            for n in dict.fromkeys([payload["name"]] + list(payload.get("aliases") or [])):
                self.conn.execute("INSERT OR IGNORE INTO entity_aliases(alias_norm, scope, object_id, alias)"
                                  " VALUES (?,?,?,?)", (_norm_alias(n), o["scope"], object_id, n))
            self._event("AGENT_WRITE" if _is_agent(actor) else "USER_DECISION", object_id,
                        {"action": "native.update_object", "revision": rev, "changed": sorted(changed)}, actor)
            search_index.reindex_object(self.conn, object_id)
            self._keep_fresh(was_fresh)
        return {"status": Status.OK, "object_id": object_id, "revision": rev, "changed": sorted(changed)}

    # ------------------------------------------------------------ proposals
    def _evidence_ref(self, scope: str, ev: dict | str) -> str:
        return self._evidence(scope, ev)["ref"]

    def _evidence(self, scope: str, ev: dict | str) -> dict:
        """Resolve one piece of evidence to {ref, anchor, quote}. Accepted forms:
        an existing source_ref_id; {doc_id, node_id, revision?} of a native node; or
        {object_id, anchor} of a paragraph of a mirrored document (the latest imported version)."""
        if isinstance(ev, dict) and ev.get("object_id") and ev.get("anchor"):
            o = self.conn.execute("SELECT scope FROM objects WHERE object_id = ?", (ev["object_id"],)).fetchone()
            if o is None or o["scope"] != scope:
                raise NativeError(Status.INVALID_REQUEST, f"evidence document {ev['object_id']!r} is not readable here")
            row = None
            for r in self.conn.execute(
                    "SELECT statement, source_ref_ids, detail FROM observations WHERE object_id = ?"
                    " AND dimension = 'document_paragraph' ORDER BY observation_id DESC", (ev["object_id"],)):
                d = json.loads(r["detail"] or "{}")
                if ev["anchor"] in (d.get("anchor"), d.get("legacy_anchor")) or \
                        str(d.get("anchor", "")).split("-")[0] == ev["anchor"]:
                    row, anchor = r, d.get("anchor")
                    break
            if row is None:
                raise NativeError(Status.INVALID_REQUEST,
                                  f"anchor {ev['anchor']!r} is not a paragraph of {ev['object_id']!r}")
            ref = json.loads(row["source_ref_ids"])[0]
            sr = self.conn.execute("SELECT access_scope, status FROM source_refs WHERE source_ref_id = ?",
                                   (ref,)).fetchone()
            if sr is None or sr["access_scope"] != scope or sr["status"] != "OK":
                raise NativeError(Status.INVALID_REQUEST, f"evidence {ref!r} is not a readable source here")
            return {"ref": ref, "anchor": anchor, "quote": row["statement"]}
        if isinstance(ev, str):
            ref = self.conn.execute("SELECT source_ref_id, access_scope, status FROM source_refs"
                                    " WHERE source_ref_id = ?", (ev,)).fetchone()
            if ref is None or ref["access_scope"] != scope or ref["status"] != "OK":
                raise NativeError(Status.INVALID_REQUEST, f"evidence {ev!r} is not a readable source here")
            return {"ref": ev, "anchor": None, "quote": None}
        doc = self._doc(ev.get("doc_id", ""))
        self._check_scope(doc, scope)
        rev = int(ev.get("revision") or doc["current_revision"])
        node = self._load_tree(doc["doc_id"], rev).get(ev.get("node_id"))
        if node is None:
            raise NativeError(Status.INVALID_REQUEST, f"node {ev.get('node_id')!r} not in revision {rev}")
        ref_id = f"nexus:{doc['doc_id']}@{rev}#{node['node_id']}"
        text = node.get("body") or node.get("title") or ""
        self.conn.execute(
            "INSERT OR IGNORE INTO source_refs(source_ref_id, source_system, native_id, revision, location,"
            " anchor, access_scope, retrieved_at, status, content_sha256) VALUES (?,?,?,?,?,?,?,?,'OK',?)",
            (ref_id, SOURCE_SYSTEM, doc["doc_id"], str(rev), f"nexus://{doc['doc_id']}", node["node_id"],
             scope, utcnow(), sha256_text(text)))
        return {"ref": ref_id, "anchor": node["node_id"], "quote": text}

    def _record_quotes(self, relation_id: str, from_id: str, items: list[dict]) -> None:
        """Keep the verbatim evidence of a relation (shown by nexus_related)."""
        for i, it in enumerate(items):
            if it.get("quote"):
                self.conn.execute(
                    "INSERT OR REPLACE INTO observations(observation_id, object_id, dimension, statement, stance,"
                    " detail, source_ref_ids, created_at) VALUES (?,?, 'relation_evidence', ?, 'SUPPORTS', ?, ?, ?)",
                    (f"obs/{relation_id}/{i}", from_id, it["quote"],
                     json.dumps({"relation_id": relation_id, "anchor": it.get("anchor"), "note": "evidence"},
                                ensure_ascii=False), json.dumps([it["ref"]]), utcnow()))
        if from_id.startswith("ent/"):
            search_index.reindex_object(self.conn, from_id)

    def propose_relation(self, scope: str, from_id: str, relation_type: str, to_id: str,
                         evidence: list, proposer: str, rationale: str | None = None) -> dict:
        if not evidence:
            raise NativeError(Status.INVALID_REQUEST, "a relation proposal must cite at least one source")
        if not (relation_type or "").strip():
            raise NativeError(Status.INVALID_REQUEST, "relation_type is required")
        for oid in (from_id, to_id):
            o = self.conn.execute("SELECT scope FROM objects WHERE object_id = ?", (oid,)).fetchone()
            if o is None or o["scope"] != scope:
                raise NativeError(Status.NOT_FOUND, f"no object {oid!r} in scope {scope!r}")
        pid, rid = _new_id("prop_"), _new_id("rel_")
        was_fresh = self._index_fresh()
        with self._tx():
            items = [self._evidence(scope, e) for e in evidence]
            refs = [it["ref"] for it in items]
            rv = review.classify_relation(self.conn, from_id, relation_type, to_id, refs)
            auto = rv["tier"] == review.AUTO
            verification = "VERIFIED" if auto else "UNVERIFIED"
            self.conn.execute(
                "INSERT INTO relations(relation_id, from_id, relation_type, to_id, verification, scope,"
                " provenance_refs, created_at, extraction_run) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, from_id, relation_type, to_id, verification, scope, json.dumps(refs), utcnow(), pid))
            self._record_quotes(rid, from_id, items)
            self.conn.execute(
                "INSERT INTO native_proposals(proposal_id, kind, relation_id, payload, rationale, evidence,"
                " proposer, created_at, status) VALUES (?, 'RELATION', ?,?,?,?,?,?, 'PROPOSED')",
                (pid, rid, canonical_json({"from_id": from_id, "relation_type": relation_type,
                                           "to_id": to_id, "review": rv["tier"]}),
                 rationale, json.dumps(refs), proposer, utcnow()))
            if auto:
                self._decide(pid, "ACCEPTED", review.POLICY_ACTOR,
                             {"relation_id": rid, "verification": "VERIFIED", "review": rv})
            self._keep_fresh(was_fresh)
        out = {"status": Status.OK, "proposal_id": pid, "relation_id": rid, "verification": verification,
               "review": rv["tier"], "evidence": refs}
        out["note"] = {
            review.AUTO: "plain filing: confirmed automatically by policy (undo with remove_relation)",
            review.QUIET: "stored as an UNVERIFIED candidate (a lead, not a confirmed fact)",
            review.GATE: ("stored as an UNVERIFIED interpretation (check_source): before acting on it, "
                          "read the quoted source paragraph and rely on that")}[rv["tier"]]
        return out

    def propose_edit(self, doc_id: str, base_revision: int, ops: list[dict], proposer: str,
                     rationale: str | None = None, evidence: list | None = None,
                     scope: str | None = None) -> dict:
        doc = self._doc(doc_id)
        self._check_scope(doc, scope)
        base = self._load_tree(doc_id, int(base_revision))
        if not base:
            raise NativeError(Status.NOT_FOUND, f"{doc_id!r} has no revision {base_revision}")
        self._apply_ops(base, ops)             # validate now; applied only on acceptance
        pid = _new_id("prop_")
        was_fresh = self._index_fresh()
        with self._tx():
            refs = [self._evidence_ref(doc["scope"], e) for e in (evidence or [])]
            self.conn.execute(
                "INSERT INTO native_proposals(proposal_id, kind, doc_id, base_revision, payload, rationale,"
                " evidence, proposer, created_at, status) VALUES (?, 'EDIT', ?,?,?,?,?,?,?, 'PROPOSED')",
                (pid, doc_id, int(base_revision), canonical_json({"ops": ops}), rationale,
                 json.dumps(refs), proposer, utcnow()))
            self._keep_fresh(was_fresh)
        return {"status": Status.OK, "proposal_id": pid, "doc_id": doc_id,
                "base_revision": int(base_revision), "state": "PROPOSED",
                "note": "not applied; the owner accepts it, and it is refused if the document moved on"}

    def propose_document(self, scope: str, spec: dict, proposer: str, rationale: str | None = None,
                         evidence: list | None = None) -> dict:
        """A new document, stored as a proposal; the owner's acceptance creates it (revision 1)."""
        if self.conn.execute("SELECT 1 FROM scopes WHERE scope = ?", (scope,)).fetchone() is None:
            raise NativeError(Status.INVALID_SCOPE, f"unknown scope {scope!r}")
        title, tree = self._build_tree(spec)      # validate now
        pid = _new_id("prop_")
        was_fresh = self._index_fresh()
        with self._tx():
            refs = [self._evidence_ref(scope, e) for e in (evidence or [])]
            self.conn.execute(
                "INSERT INTO native_proposals(proposal_id, kind, payload, rationale, evidence, proposer,"
                " created_at, status) VALUES (?, 'CREATE', ?,?,?,?,?, 'PROPOSED')",
                (pid, canonical_json({"scope": scope, "spec": spec}), rationale, json.dumps(refs),
                 proposer, utcnow()))
            self._keep_fresh(was_fresh)
        return {"status": Status.OK, "proposal_id": pid, "state": "PROPOSED", "title": title,
                "sections": sum(1 for n in tree.values() if n["kind"] == "section"),
                "paragraphs": sum(1 for n in tree.values() if n["kind"] == "paragraph"),
                "chars": sum(len(n["body"] or "") for n in tree.values()),
                "note": "not created yet; it becomes a Nexus document when the owner accepts it"}

    def proposals(self, status: str | None = None, review_tier: str | None = None) -> dict:
        """review_tier filters by tier (the owner never has to work through any of them); GATE
        also returns proposals with no tier recorded (EDIT/CREATE, or made before tiers)."""
        q, args, where = "SELECT * FROM native_proposals", [], []
        if status:
            where.append("status = ?"); args.append(status)
        if review_tier:
            tier = "json_extract(payload, '$.review')"
            if review_tier.upper() == review.GATE:
                where.append(f"({tier} = 'GATE' OR {tier} IS NULL)")
            else:
                where.append(f"{tier} = ?"); args.append(review_tier.upper())
        if where:
            q += " WHERE " + " AND ".join(where)
        rows = []
        for r in self.conn.execute(q + " ORDER BY created_at", args):
            d = dict(r)
            for k in ("payload", "evidence", "outcome"):
                d[k] = json.loads(d[k]) if d[k] else None
            rows.append(d)
        return {"status": Status.OK, "proposals": rows}

    def _proposal(self, pid: str) -> dict:
        r = self.conn.execute("SELECT * FROM native_proposals WHERE proposal_id = ?", (pid,)).fetchone()
        if r is None:
            raise NativeError(Status.NOT_FOUND, f"no proposal {pid!r}")
        if r["status"] != "PROPOSED":
            raise NativeError(Status.INVALID_REQUEST, f"proposal {pid!r} is already {r['status']}")
        return dict(r)

    def _decide(self, pid: str, status: str, actor: str, outcome: dict) -> None:
        self.conn.execute("UPDATE native_proposals SET status = ?, decided_by = ?, decided_at = ?, outcome = ?"
                          " WHERE proposal_id = ?", (status, actor, utcnow(), canonical_json(outcome), pid))

    def accept(self, pid: str, actor: str = OWNER) -> dict:
        p = self._proposal(pid)
        if _is_agent(actor) or actor != OWNER:
            raise NativeError(Status.AUTHORITY_DENIED, f"{actor!r} cannot accept proposals; owner is {OWNER!r}")
        if p["kind"] == "RELATION":
            with self._tx():
                self.conn.execute("UPDATE relations SET verification = 'VERIFIED' WHERE relation_id = ?",
                                  (p["relation_id"],))
                self._decide(pid, "ACCEPTED", actor, {"relation_id": p["relation_id"], "verification": "VERIFIED"})
                self._event("USER_DECISION", None, {"action": "native.accept_relation", "proposal_id": pid,
                                                    "relation_id": p["relation_id"]}, actor)
            return {"status": Status.OK, "proposal_id": pid, "relation_id": p["relation_id"],
                    "verification": "VERIFIED"}
        if p["kind"] == "OBJECT":
            oid = json.loads(p["payload"])["object_id"]
            with self._tx():
                self.conn.execute("UPDATE aspects SET verification = 'VERIFIED' WHERE object_id = ?"
                                  " AND name = 'entity' AND is_current = 1", (oid,))
                self._decide(pid, "ACCEPTED", actor, {"object_id": oid, "verification": "VERIFIED"})
                self._event("USER_DECISION", oid, {"action": "native.accept_object", "proposal_id": pid}, actor)
            return {"status": Status.OK, "proposal_id": pid, "object_id": oid, "verification": "VERIFIED"}
        if p["kind"] == "CREATE":
            payload = json.loads(p["payload"])
            out = self.create(payload["scope"], payload["spec"], actor,
                              message=f"accept {pid}" + (f": {p['rationale']}" if p["rationale"] else ""),
                              _proposal_id=pid)
            self._decide(pid, "ACCEPTED", actor, {"doc_id": out["doc_id"], "revision": 1})
            return {**out, "proposal_id": pid}
        ops = json.loads(p["payload"])["ops"]
        try:
            out = self.edit(p["doc_id"], p["base_revision"], ops, actor,
                            message=f"accept {pid}" + (f": {p['rationale']}" if p["rationale"] else ""),
                            _proposal_id=pid)
        except NativeError as e:
            if e.code == Status.VERSION_CONFLICT:
                self._decide(pid, "STALE", actor, e.as_dict())
            raise
        self._decide(pid, "ACCEPTED", actor, {"revision": out["revision"]})
        return {**out, "proposal_id": pid}

    def reject(self, pid: str, actor: str = OWNER, reason: str | None = None) -> dict:
        p = self._proposal(pid)
        if _is_agent(actor) or actor != OWNER:
            raise NativeError(Status.AUTHORITY_DENIED, f"{actor!r} cannot reject proposals")
        with self._tx():
            if p["kind"] == "RELATION":
                self.conn.execute("UPDATE relations SET verification = 'DISPUTED', expired_at = ?"
                                  " WHERE relation_id = ?", (utcnow(), p["relation_id"]))
            if p["kind"] == "OBJECT":
                # the object id stays valid (history), but it is marked disputed and leaves search
                oid = json.loads(p["payload"])["object_id"]
                self.conn.execute("UPDATE aspects SET verification = 'DISPUTED' WHERE object_id = ?"
                                  " AND name = 'entity' AND is_current = 1", (oid,))
                self.conn.execute("DELETE FROM search_index WHERE object_id = ?", (oid,))
            self._decide(pid, "REJECTED", actor, {"reason": reason})
        return {"status": Status.OK, "proposal_id": pid, "state": "REJECTED"}

    # ------------------------------------------------------------ export
    def export_markdown(self, doc_id: str, revision: int | None = None) -> str:
        d = self.read(doc_id, revision)
        lines = [f"<!-- nexus doc={doc_id} revision={d['revision']} tree_sha256="
                 f"{d['revision_info']['tree_sha256']} -->", f"# {d['title']}", ""]
        for n in d["nodes"]:
            if n["kind"] == "section":
                lines += [f"{'#' * min(6, n['depth'] + 2)} {n['title']} <!-- node={n['node_id']} -->", ""]
            else:
                lines += [f"<!-- node={n['node_id']} -->", n["text"], ""]
        return "\n".join(lines)

    def export_json(self, doc_id: str) -> dict:
        """Every revision in full: enough to rebuild the document elsewhere."""
        h = self.history(doc_id)
        return {"format": "nexus-native-export-1", "doc_id": doc_id,
                "current_revision": h["current_revision"],
                "revisions": [{**r, "nodes": self.read(doc_id, r["revision"])["nodes"]}
                              for r in h["revisions"]]}


# ---------------------------------------------------------------- backup / restore
def _db_summary(path: str | Path) -> dict:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for t in ("objects", "relations", "native_documents", "native_revisions", "native_tree",
                  "native_proposals", "source_sections"):
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[t] = None
        heads = {r[0]: r[1] for r in conn.execute("SELECT doc_id, current_revision FROM native_documents")} \
            if counts["native_documents"] is not None else {}
    finally:
        conn.close()
    return {"integrity_check": integrity, "counts": counts, "native_heads": heads}


def _file_sha(path: str | Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(db_path: str, dest: str) -> dict:
    """Consistent online copy (sqlite3 backup API, safe with WAL and concurrent readers)."""
    dest_p = Path(dest)
    if dest_p.exists():
        raise NativeError(Status.INVALID_REQUEST, f"{dest} already exists; backups are never overwritten")
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(str(dest_p))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    manifest = {"format": "nexus-backup-1", "source": str(Path(db_path).resolve()), "file": dest_p.name,
                "created_at": utcnow(), "sha256": _file_sha(dest_p), **_db_summary(dest_p)}
    dest_p.with_suffix(dest_p.suffix + ".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": Status.OK if manifest["integrity_check"] == "ok" else Status.CONFLICTED, **manifest}


def restore_backup(backup_path: str, target: str) -> dict:
    """Restore into a NEW file (never over a live database) and verify it against the manifest."""
    bp, tp = Path(backup_path), Path(target)
    if tp.exists():
        raise NativeError(Status.INVALID_REQUEST, f"{target} exists; restore goes to a new file")
    mpath = bp.with_suffix(bp.suffix + ".manifest.json")
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else None
    if manifest and _file_sha(bp) != manifest["sha256"]:
        raise NativeError(Status.CONFLICTED, "backup file does not match its manifest sha256")
    src = sqlite3.connect(f"file:{bp.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tp))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    summary = _db_summary(tp)
    ok = summary["integrity_check"] == "ok" and (manifest is None or (
        summary["counts"] == manifest["counts"] and summary["native_heads"] == manifest["native_heads"]))
    return {"status": Status.OK if ok else Status.CONFLICTED, "target": str(tp), "matches_manifest": ok,
            "manifest_checked": manifest is not None, **summary}


# ---------------------------------------------------------------- write timing log
def write_log_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".writes.jsonl")


def log_write(db_path: str | Path | None, record: dict) -> None:
    """One JSON line per write call (who, what, how long), next to the database. Never raises."""
    if not db_path:
        return
    try:
        with open(write_log_path(db_path), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": utcnow(), **record}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def accept_many(n: "NativeDocs", ids: list[str], pending: bool, db_path: str | None = None) -> dict:
    import time
    if pending:
        ids = [p["proposal_id"] for p in n.proposals("PROPOSED")["proposals"]]
    if not ids:
        raise NativeError(Status.INVALID_REQUEST, "no proposal ids given (or none pending)")
    results = []
    for pid in ids:
        t = time.perf_counter()
        try:
            r = n.accept(pid)
        except NativeError as e:
            r = {**e.as_dict(), "proposal_id": pid}
        r["elapsed_ms"] = round((time.perf_counter() - t) * 1000, 1)
        log_write(db_path, {"tool": "accept", "actor": OWNER, "proposal_id": pid,
                            "status": r.get("status"), "elapsed_ms": r["elapsed_ms"],
                            "doc_id": r.get("doc_id"), "revision": r.get("revision")})
        results.append(r)
    ok = all(r.get("status") == Status.OK for r in results)
    return {"status": Status.OK if ok else Status.PARTIAL, "accepted": results}


def timing_summary(db_path: str) -> dict:
    path = write_log_path(db_path)
    if not path.exists():
        raise NativeError(Status.NOT_FOUND, f"no write log yet at {path.name}")
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    by: dict[str, list[float]] = {}
    for r in rows:
        by.setdefault(r["tool"], []).append(r["elapsed_ms"])
    stats = {}
    for tool, xs in by.items():
        xs = sorted(xs)
        stats[tool] = {"calls": len(xs), "median_ms": xs[len(xs) // 2], "max_ms": xs[-1],
                       "total_ms": round(sum(xs), 1)}
    return {"status": Status.OK, "log": path.name, "by_tool": stats, "calls": rows}


# ---------------------------------------------------------------- CLI
def _cli(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.native",
                                description="Nexus-native documents. The CLI acts as the owner ('user').")
    p.add_argument("--db", required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--scope", required=True); c.add_argument("--spec", required=True)
    r = sub.add_parser("read"); r.add_argument("doc_id"); r.add_argument("--revision", type=int)
    sub.add_parser("history").add_argument("doc_id")
    d = sub.add_parser("diff"); d.add_argument("doc_id"); d.add_argument("a", type=int); d.add_argument("b", type=int)
    e = sub.add_parser("edit"); e.add_argument("doc_id"); e.add_argument("--expected-revision", type=int, required=True)
    e.add_argument("--ops", required=True, help="JSON file with a list of ops"); e.add_argument("--message")
    rs = sub.add_parser("restore"); rs.add_argument("doc_id"); rs.add_argument("--to", type=int, required=True)
    rs.add_argument("--expected-revision", type=int, required=True)
    pl = sub.add_parser("proposals"); pl.add_argument("--status")
    pl.add_argument("--review", choices=("GATE", "QUIET", "AUTO"), help="filter by tier; GATE = interpretations marked check_source")
    ac = sub.add_parser("accept", help="accept one or more proposals, or all pending with --pending")
    ac.add_argument("proposal_ids", nargs="*")
    ac.add_argument("--pending", action="store_true")
    sub.add_parser("timing", help="summarise the write timing log next to the database")
    rj = sub.add_parser("reject"); rj.add_argument("proposal_id"); rj.add_argument("--reason")
    ex = sub.add_parser("export"); ex.add_argument("doc_id"); ex.add_argument("--format", choices=("md", "json"), default="md")
    ex.add_argument("--revision", type=int)
    sub.add_parser("backup").add_argument("dest")
    rb = sub.add_parser("restore-backup"); rb.add_argument("backup"); rb.add_argument("target")
    a = p.parse_args(argv)
    try:
        if a.cmd == "backup":
            out: Any = backup(a.db, a.dest)
        elif a.cmd == "restore-backup":
            out = restore_backup(a.backup, a.target)
        else:
            conn = connect(a.db)
            init_schema(conn)
            n = NativeDocs(conn)
            load = lambda f: json.loads(Path(f).read_text(encoding="utf-8"))  # noqa: E731
            out = {
                "create": lambda: n.create(a.scope, load(a.spec)),
                "read": lambda: n.read(a.doc_id, a.revision),
                "history": lambda: n.history(a.doc_id),
                "diff": lambda: n.diff(a.doc_id, a.a, a.b),
                "edit": lambda: n.edit(a.doc_id, a.expected_revision, load(a.ops), message=a.message),
                "restore": lambda: n.restore(a.doc_id, a.to, a.expected_revision),
                "proposals": lambda: n.proposals(a.status, a.review),
                "accept": lambda: accept_many(n, a.proposal_ids, a.pending, a.db),
                "timing": lambda: timing_summary(a.db),
                "reject": lambda: n.reject(a.proposal_id, reason=a.reason),
                "export": lambda: (n.export_markdown(a.doc_id, a.revision) if a.format == "md"
                                   else n.export_json(a.doc_id)),
            }[a.cmd]()
    except NativeError as e:
        out = e.as_dict()
    if isinstance(out, str):
        sys.stdout.write(out + "\n")
        return 0
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    return 0 if out.get("status") == Status.OK else 2


if __name__ == "__main__":
    raise SystemExit(_cli())
