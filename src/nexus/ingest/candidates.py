"""Import extracted entity/relation candidates — deterministic validation only.

Extraction (reading paragraphs, proposing entities and relations) is done
outside this module, by an LLM or a human, and handed over as JSON::

    {
      "run_id": "t008-extract-1",
      "entities": [
        {"key": "e1", "name": "ami.moment", "type": "creator",
         "aliases": ["ami", "@ami.moment"],
         "mentions": [{"source_ref_id": "...", "anchor": "h2.1-3811f68d",
                       "quote": "作者 @ami.moment"}]}
      ],
      "relations": [
        {"from": "e1", "to": "video/AMI-001", "type": "AUTHORED",
         "source_ref_id": "...", "anchor": "h2.1-3811f68d", "quote": "作者 @ami.moment",
         "note": "optional", "supersedes": "optional relation_id"}
      ]
    }

``from``/``to`` may be an entity ``key`` from the same payload or an existing
object id. Nothing the extractor says is trusted:

* every quote must occur verbatim (whitespace-normalised) in the cited section,
  and the section must belong to a readable SourceRef of this scope;
* entity names are resolved against existing objects and aliases; an ambiguous
  name is rejected rather than guessed;
* every accepted relation is ``UNVERIFIED`` with its quote stored as a
  ``relation_evidence`` observation — never VERIFIED, never Current;
* rejected items are returned with a reason and not written.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from ..db import Store, canonical_json, utcnow
from .sections import normalise

RELATION_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{1,40}$")
OBJECT_TYPE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")
EVIDENCE_DIMENSION = "relation_evidence"
MENTION_DIMENSION = "entity_mention"


def norm_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(r"[\s@#·._\-|｜:：《》「」\"'“”]+", "", text)


def _slug(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip()
    text = re.sub(r"[\s/\\]+", "-", text)
    return re.sub(r"[^\w\-.㐀-鿿]", "", text)[:48] or hashlib.sha1(name.encode()).hexdigest()[:8]


class _Reject(Exception):
    pass


class CandidateImporter:
    def __init__(self, store: Store, scope: str, run_id: str):
        self.store = store
        self.conn = store.conn
        self.scope = scope
        self.run_id = run_id
        self.now = utcnow()
        self.accepted: dict[str, list] = {"entities": [], "relations": [], "merged": []}
        self.rejected: list[dict] = []

    # ------------------------------------------------------------ evidence
    def _check_quote(self, source_ref_id: str, anchor: str, quote: str) -> tuple[str, dict]:
        ref = self.store.get_source_ref(source_ref_id or "")
        if ref is None:
            raise _Reject("SOURCE_REF_NOT_FOUND")
        if ref["access_scope"] != self.scope:
            raise _Reject("SOURCE_SCOPE_DENIED")
        if ref["status"] != "OK":
            raise _Reject(f"SOURCE_{ref['status']}")
        resolved = self.store.resolve_anchor(source_ref_id, anchor or "")
        if resolved is None:
            raise _Reject("ANCHOR_NOT_FOUND")
        section = self.store.get_sections(source_ref_id, [resolved])[0]
        q = normalise(quote or "")
        if len(q) < 2:
            raise _Reject("QUOTE_TOO_SHORT")
        if q not in normalise(section["body"]):
            raise _Reject("QUOTE_NOT_IN_SECTION")
        return resolved, ref

    # ------------------------------------------------------------ entities
    def _lookup(self, name: str) -> list[str]:
        key = norm_name(name)
        if not key:
            return []
        ids = {r["object_id"] for r in self.conn.execute(
            "SELECT object_id FROM entity_aliases WHERE alias_norm = ? AND scope = ?",
            (key, self.scope))}
        for r in self.conn.execute(
                "SELECT object_id, display_name FROM objects WHERE scope = ?", (self.scope,)):
            if norm_name(r["display_name"] or "") == key or \
                    norm_name(r["object_id"].split("/", 1)[-1]) == key:
                ids.add(r["object_id"])
        return sorted(ids)

    def _entity(self, ent: dict) -> str:
        name, etype = ent.get("name") or "", ent.get("type") or ""
        if not name.strip():
            raise _Reject("ENTITY_NAME_EMPTY")
        if not OBJECT_TYPE.match(etype):
            raise _Reject("ENTITY_TYPE_INVALID")
        mentions = ent.get("mentions") or []
        if not mentions:
            raise _Reject("ENTITY_WITHOUT_MENTION")
        checked = [(m, *self._check_quote(m.get("source_ref_id"), m.get("anchor"), m.get("quote")))
                   for m in mentions]
        names = [name, *(ent.get("aliases") or [])]
        matches = sorted({oid for n in names for oid in self._lookup(n)})
        if len(matches) > 1:
            raise _Reject(f"ENTITY_AMBIGUOUS:{','.join(matches)}")
        if matches:
            object_id, created = matches[0], False
        else:
            object_id, created = f"{etype}/{_slug(name)}", True
            if self.store.get_object(object_id) is not None:  # id taken in another scope
                raise _Reject("ENTITY_ID_COLLISION")
            self.store.add_object(object_id, etype, name.strip(), self.scope, self.now)
        for n in names:
            if norm_name(n):
                self.conn.execute(
                    "INSERT OR IGNORE INTO entity_aliases(alias_norm, scope, object_id, alias)"
                    " VALUES (?,?,?,?)", (norm_name(n), self.scope, object_id, n))
        for i, (m, anchor, ref) in enumerate(checked, start=1):
            digest = hashlib.sha1(f"{ref['source_ref_id']}#{anchor}".encode()).hexdigest()[:10]
            self.store.add_observation(
                f"obs/mention/{object_id}/{digest}", object_id, MENTION_DIMENSION,
                normalise(m["quote"]), "INFERENCE",
                json.dumps({"anchor": anchor, "extraction_run": self.run_id,
                            "text_review_status": "EXTRACTED_UNREVIEWED"}, ensure_ascii=False),
                [ref["source_ref_id"]])
        self.accepted["entities"].append({"key": ent.get("key"), "object_id": object_id,
                                          "created": created})
        return object_id

    # ------------------------------------------------------------ relations
    def _relation(self, rel: dict, keys: dict[str, str]) -> None:
        rtype = rel.get("type") or ""
        if not RELATION_TYPE.match(rtype):
            raise _Reject("RELATION_TYPE_INVALID")
        ends = []
        for side in ("from", "to"):
            ref = rel.get(side) or ""
            if ref in keys:
                ends.append(keys[ref])
                continue
            obj = self.store.get_object(ref)
            if obj is None:
                raise _Reject(f"{side.upper()}_UNRESOLVED")
            if obj["scope"] != self.scope:
                raise _Reject(f"{side.upper()}_SCOPE_DENIED")
            ends.append(ref)
        if ends[0] == ends[1]:
            raise _Reject("SELF_RELATION")
        anchor, ref = self._check_quote(rel.get("source_ref_id"), rel.get("anchor"), rel.get("quote"))
        rel_id = "rel/cand/" + hashlib.sha1(
            f"{self.scope}|{ends[0]}|{rtype}|{ends[1]}".encode()).hexdigest()[:12]
        existing = self.store.get_relation(rel_id)
        if existing and existing["expired_at"] is None:
            refs = json.loads(existing["provenance_refs"])
            if ref["source_ref_id"] not in refs:
                refs.append(ref["source_ref_id"])
                self.conn.execute("UPDATE relations SET provenance_refs = ? WHERE relation_id = ?",
                                  (canonical_json(refs), rel_id))
            self.accepted["merged"].append(rel_id)
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO relations(relation_id, from_id, relation_type, to_id,"
                " verification, scope, provenance_refs, valid_from, valid_to, created_at,"
                " expired_at, superseded_by, extraction_run)"
                " VALUES (?,?,?,?,'UNVERIFIED',?,?,?,NULL,?,NULL,NULL,?)",
                (rel_id, ends[0], rtype, ends[1], self.scope,
                 canonical_json([ref["source_ref_id"]]), rel.get("valid_from"), self.now,
                 self.run_id))
            self.accepted["relations"].append(rel_id)
        digest = hashlib.sha1(f"{ref['source_ref_id']}#{anchor}".encode()).hexdigest()[:10]
        self.store.add_observation(
            f"obs/relev/{rel_id.rsplit('/', 1)[-1]}/{digest}", ends[0], EVIDENCE_DIMENSION,
            normalise(rel["quote"]), "INFERENCE",
            json.dumps({"anchor": anchor, "relation_id": rel_id, "relation_type": rtype,
                        "to_id": ends[1], "note": rel.get("note"),
                        "extraction_run": self.run_id,
                        "text_review_status": "EXTRACTED_UNREVIEWED"}, ensure_ascii=False),
            [ref["source_ref_id"]])
        old = rel.get("supersedes")
        if old:
            prior = self.store.get_relation(old)
            if prior is None or prior["scope"] != self.scope:
                raise _Reject("SUPERSEDES_UNKNOWN_RELATION")
            if old != rel_id:
                self.conn.execute(
                    "UPDATE relations SET expired_at = ?, superseded_by = ?,"
                    " valid_to = COALESCE(valid_to, ?) WHERE relation_id = ? AND expired_at IS NULL",
                    (self.now, rel_id, rel.get("valid_from") or self.now, old))

    # ------------------------------------------------------------------ run
    def run(self, payload: dict) -> dict:
        if self.store.get_scope(self.scope) is None:
            return {"status": "INVALID_SCOPE", "accepted": self.accepted, "rejected": []}
        keys: dict[str, str] = {}
        for ent in payload.get("entities") or []:
            try:
                with self.store.tx():
                    keys[ent.get("key") or ent.get("name")] = self._entity(ent)
            except _Reject as exc:
                self.rejected.append({"kind": "entity", "item": ent, "reason": str(exc)})
        for rel in payload.get("relations") or []:
            try:
                with self.store.tx():
                    self._relation(rel, keys)
            except _Reject as exc:
                self.rejected.append({"kind": "relation", "item": rel, "reason": str(exc)})
        return {"status": "OK", "run_id": self.run_id, "scope": self.scope,
                "accepted": self.accepted, "rejected": self.rejected,
                "counts": {"entities": len(self.accepted["entities"]),
                           "relations_new": len(self.accepted["relations"]),
                           "relations_merged": len(self.accepted["merged"]),
                           "rejected": len(self.rejected)}}


def import_candidates(store: Store, payload: dict[str, Any], scope: str,
                      run_id: str | None = None) -> dict:
    return CandidateImporter(store, scope, run_id or payload.get("run_id") or "manual").run(payload)
