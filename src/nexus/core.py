"""Nexus Core — read-only semantic services (P1 prototype).

Implements the five required operations plus local-only event evaluation:

    resolve_object, get_current_state, traverse_relations, trace_evidence, get_context

Design constraints from Nexus Core Contract v0.1:
  * execution status (did the query run?) is separate from fact state
    (is the fact Current / Historical / Conflicted?);
  * Relevance, Authority, Verification, State and Freshness are distinct;
  * no historical item is ever silently promoted to Current;
  * an unverified relation is never presented as an effective relation;
  * permission-blocked sources are not served from cache.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from .db import Store, canonical_json, sha256_text, utcnow
from .errors import Status
from . import search as search_index

CONTRACT_VERSION = "nexus-core-p1/0.1"

# Intent -> minimal-sufficient retrieval plan. Deterministic and inspectable:
# the plan itself is returned as `retrieval_path` so a reviewer can check that a
# status question did *not* drag in the whole policy corpus.
CONTEXT_ROUTES: dict[str, dict[str, Any]] = {
    "current_decision": {
        "aspects": ["decision"],
        "relations": ["GOVERNED_BY", "INFORMS"],
        "policy": True,
        "events": True,
    },
    "start_class": {
        "aspects": ["decision", "status"],
        "relations": ["GOVERNED_BY"],
        "policy": True,
        "events": True,
    },
    "learning_progress": {
        "aspects": ["learner_state"],
        "policy": False,
        "events": False,
    },
    "first_video_status": {"aspects": ["first_video"], "policy": False},
    "opening_quality": {
        "aspects": ["creative_purpose", "style_quality"],
        "extra_aspects": ["research_candidates"],
        "evidence": True,
        "policy": False,
    },
    "release_boundaries": {"aspects": ["release_boundaries"], "policy": False},
    "sponsorship": {
        "aspects": ["profile", "economics"],
        "dimensions": ["commercial", "disclosure"],
        "policy": False,
    },
    "production_stack": {"dimensions": ["equipment", "ai_workflow"], "policy": False},
    "profit": {"dimensions": ["commercial", "cost"], "extra_aspects": ["economics"]},
    "creator_overview": {
        "dimensions": ["career", "commercial", "equipment", "ai_workflow", "operations", "tradeoff"],
        "policy": False,
    },
    "cross_dimension": {"dimensions": ["commercial", "tradeoff", "operations"]},
}

# Routing must not be able to drop facts that are necessary for the intent.
# These aspects are appended even if a future route edit forgets them.
REQUIRED_ASPECTS_BY_INTENT: dict[str, list[str]] = {
    "sponsorship": ["profile", "economics"],
    "profit": ["economics"],
    "cross_dimension": ["profile", "economics"],
}


def _j(value: str | None) -> Any:
    return json.loads(value) if value else None


def _dedupe(items: list[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        k = str(item.get(key))
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


class _EventRejected(Exception):
    """Internal rollback signal for an event that fails in-transaction validation."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class NexusCore:
    def __init__(self, store: Store):
        self._store = store

    # ------------------------------------------------------------- envelopes
    def _envelope(
        self,
        request: dict,
        status: str,
        path: list[str] | None = None,
        *,
        result: Any = None,
        authority: Any = None,
        state: Any = None,
        evidence: list[dict] | None = None,
        missing: list[Any] | None = None,
        conflicts: list[dict] | None = None,
        warnings: list[str] | None = None,
        provider_io: dict | None = None,
        error: dict | None = None,
        extra: dict | None = None,
    ) -> dict:
        body = {
            "contract_version": CONTRACT_VERSION,
            "request": request,
            "execution": {
                "status": status,
                "retrieval_path": path or [],
                "provider_io": provider_io
                or {
                    "drive_api_calls": 0,
                    "source_documents_read": 0,
                    "source_chars_returned": 0,
                    "full_source_reads": 0,
                },
                "generated_at": utcnow(),
            },
            "result": result,
            "authority": authority,
            "state": state,
            "evidence": evidence or [],
            "missing": missing or [],
            "conflicts": conflicts or [],
            "warnings": warnings or [],
            "error": error,
        }
        if extra:
            body.update(extra)
        return body

    def _error(self, request, path, code, message, *, detail=None) -> dict:
        return self._envelope(
            request,
            code,
            path=path,
            error={"code": code, "message": message, "detail": detail},
        )

    # ----------------------------------------------------------------- scopes
    def _validate_scope(self, scope: str) -> tuple[dict | None, dict | None]:
        row = self._store.get_scope(scope)
        if row is None:
            known = self._store.list_scopes()
            return None, {
                "code": Status.INVALID_SCOPE,
                "message": f"scope {scope!r} is not registered",
                "detail": {"known_scopes": [s["scope"] for s in known]},
            }
        return row, None

    def _check_object_scope(self, obj: dict, scope: str) -> dict | None:
        if scope == "global":
            return None
        if obj["scope"] != scope:
            return {
                "code": Status.INVALID_SCOPE,
                "message": f"object {obj['object_id']!r} lives in scope {obj['scope']!r},"
                f" not {scope!r}",
                "detail": {"object_scope": obj["scope"]},
            }
        return None

    # -------------------------------------------------------------- freshness
    def _freshness(self, source_ref: dict | None) -> tuple[str, str | None]:
        if source_ref is None:
            return "UNKNOWN", None
        if source_ref["status"] == "BLOCKED":
            return "BLOCKED", None
        if source_ref["status"] in {"MISSING", "STALE"}:
            return source_ref["status"], None
        head = self._store.get_source_head(source_ref["source_system"], source_ref["native_id"])
        if head is None:
            return "NOT_CHECKED", None
        if head["latest_revision"] != source_ref["revision"]:
            return "STALE", head["latest_revision"]
        return "FRESH", head["latest_revision"]

    def _source_evidence(self, source_ref_id: str | None,
                         redact: bool = False) -> dict | None:
        if not source_ref_id:
            return None
        ref = self._store.get_source_ref(source_ref_id)
        if ref is None:
            return None
        if redact or ref["status"] in {"BLOCKED", "MISSING"}:
            return {
                "source_ref_id": ref["source_ref_id"],
                "source_system": ref["source_system"],
                "status": ref["status"],
                "access_scope": ref["access_scope"],
            }
        return {
            "source_ref_id": ref["source_ref_id"],
            "source_system": ref["source_system"],
            "native_id": ref["native_id"],
            "revision": ref["revision"],
            # Content identity is separate from the native revision: the hash
            # covers the source document bytes, while `revision` is the Drive
            # (or other) native revision.  Neither may be substituted for the
            # other when judging freshness.
            "content_sha256": ref["content_sha256"],
            "location": ref["location"],
            "anchor": ref["anchor"],
            "access_scope": ref["access_scope"],
            "retrieved_at": ref["retrieved_at"],
            "status": ref["status"],
        }

    def _authority_summary(self, binding: dict | None) -> dict | None:
        if binding is None:
            return None
        return {
            "binding_id": binding["binding_id"],
            "target_type": binding["target_type"],
            "target_id": binding["target_id"],
            "field_scope": binding["field_scope"],
            "owner": binding["owner"],
            "scope": binding["scope"],
            "source_of_truth": binding["source_of_truth"],
            "write_policy": binding["write_policy"],
            "migration_state": binding["migration_state"],
            "effective_from": binding["effective_from"],
            "is_open_binding": binding["effective_to"] is None,
            "authority_level": "aspect" if binding["target_type"] == "aspect" else "object",
        }

    def _resolve_aspect_owner(self, obj: dict, name: str) -> dict:
        """Resolve an aspect that lives on a directly related object.

        A Video does not own its creator's identity/profile; following the
        verified AUTHORED relation prevents the router from silently dropping
        creator-level facts for video-centric questions.
        """
        if (self._store.get_aspect(obj["object_id"], name)
                or self._store.get_aspect_history(obj["object_id"], name)):
            return obj
        for rel in self._store.get_verified_relations(
                obj["object_id"], ["AUTHORED"], obj["scope"]):
            other_id = rel["to_id"] if rel["from_id"] == obj["object_id"] else rel["from_id"]
            other = self._store.get_object(other_id)
            if other and (self._store.get_aspect(other_id, name)
                          or self._store.get_aspect_history(other_id, name)):
                return other
        return obj

    # ----------------------------------------------------------- aspect reads
    def _read_aspect(self, obj: dict, name: str, scope: str, mode: str,
                     as_of: str | None = None) -> dict:
        """Return one normalized aspect entry; never invents a Current value."""
        object_id = obj["object_id"]
        effective_scope = scope if scope != "global" else obj["scope"]
        entry: dict[str, Any] = {
            "name": name,
            "value": None,
            "lifecycle": None,
            "resolution": "NOT_RESOLVED",
            "verification": None,
            "freshness": None,
            "is_latest_verified": False,
            "revision": None,
            "valid_from": None,
            "authority": None,
            "source_refs": [],
            "notes": [],
        }

        conflicts = self._store.get_conflicts(object_id, name)
        if conflicts:
            conflict_refs: list[str] = []
            for c in conflicts:
                conflict_refs.extend(_j(c["source_ref_ids"]) or [])
            inaccessible = [
                r for r in (self._store.get_source_ref(x) for x in conflict_refs)
                if r and r["status"] in {"BLOCKED", "MISSING"}
            ]
            if inaccessible:
                entry["resolution"] = inaccessible[0]["status"]
                entry["notes"].append(
                    "conflicting evidence is not readable in this scope; no candidates served"
                )
                return entry
            entry["resolution"] = "CONFLICTED"
            entry["notes"].append("applicable facts conflict; no single Current value is served")
            entry["conflict_candidates"] = [_j(c["candidates"]) for c in conflicts]
            return entry

        current = self._store.get_aspect(object_id, name, current_only=True)
        if as_of:
            row = self._store.conn.execute(
                "SELECT * FROM aspects WHERE object_id = ? AND name = ?"
                " AND (valid_from IS NULL OR valid_from <= ?)"
                " AND (valid_to IS NULL OR valid_to > ?)"
                " ORDER BY revision DESC LIMIT 1",
                (object_id, name, as_of, as_of),
            ).fetchone()
            current = dict(row) if row else None
        history = self._store.get_aspect_history(object_id, name)
        if current is None and as_of:
            entry["resolution"] = "NOT_RESOLVED"
            entry["notes"].append(f"no aspect version was valid at as_of={as_of}")
            return entry
        if current is None:
            if history:
                latest = history[-1]
                latest_ref = (self._store.get_source_ref(latest["source_ref_id"])
                              if latest["source_ref_id"] else None)
                if (latest_ref is not None
                        and latest_ref["access_scope"] != effective_scope):
                    entry.update(
                        lifecycle=latest["lifecycle"],
                        resolution="INVALID_SCOPE",
                        verification=latest["verification"],
                        revision=latest["revision"],
                        freshness="INVALID_SCOPE",
                    )
                    entry["source_refs"] = [
                        e for e in [self._source_evidence(latest["source_ref_id"],
                                                          redact=True)] if e
                    ]
                    entry["notes"].append(
                        "historical SourceRef belongs to another scope; no value served"
                    )
                    return entry
                if latest_ref is not None and latest_ref["status"] in {"BLOCKED", "MISSING"}:
                    entry.update(
                        lifecycle=latest["lifecycle"],
                        resolution=latest_ref["status"],
                        verification=latest["verification"],
                        revision=latest["revision"],
                        freshness=latest_ref["status"],
                    )
                    entry["source_refs"] = [
                        e for e in [self._source_evidence(latest["source_ref_id"], redact=True)] if e
                    ]
                    entry["notes"].append(
                        "historical source is not readable in this scope; no value served"
                    )
                    return entry
                entry.update(
                    lifecycle=latest["lifecycle"],
                    resolution="HISTORICAL_ONLY",
                    verification=latest["verification"],
                    revision=latest["revision"],
                    valid_from=latest["valid_from"],
                    historical_value=_j(latest["payload"]),
                )
                entry["source_refs"] = [
                    e for e in [self._source_evidence(latest["source_ref_id"])] if e
                ]
                entry["notes"].append("only historical versions exist; not a Current value")
            else:
                entry["resolution"] = "NOT_RESOLVED"
                entry["notes"].append("aspect has no stored version")
            return entry

        binding = self._store.get_binding(object_id, name, effective_scope, as_of=as_of)
        authority = self._authority_summary(binding)
        source_ref_id = current["source_ref_id"]
        ref = self._store.get_source_ref(source_ref_id) if source_ref_id else None
        freshness, head_rev = self._freshness(ref)
        payload = _j(current["payload"])
        scope_mismatch = (ref is not None
                          and ref["access_scope"] != effective_scope)
        redact = (ref is None or scope_mismatch
                  or ref["status"] in {"BLOCKED", "MISSING"})

        entry.update(
            lifecycle=current["lifecycle"],
            verification=current["verification"],
            freshness=freshness,
            revision=current["revision"],
            valid_from=current["valid_from"],
            authority=authority,
        )
        entry["source_refs"] = [
            e for e in [self._source_evidence(source_ref_id, redact=redact)] if e
        ]

        # Access is checked before lifecycle/authority so a blocked or missing
        # source cannot leak cached Current *or* historical payloads.
        if scope_mismatch:
            entry["resolution"] = "INVALID_SCOPE"
            entry["freshness"] = "INVALID_SCOPE"
            entry["notes"].append(
                "SourceRef access_scope does not match the object/request scope;"
                " no value served"
            )
            return entry

        if freshness in {"BLOCKED", "MISSING"}:
            entry["resolution"] = freshness
            entry["notes"].append(
                "source is not readable in this scope; cached content is not served"
            )
            return entry

        if ref is None or freshness == "UNKNOWN":
            entry["resolution"] = "UNKNOWN"
            entry["notes"].append("no readable SourceRef for this aspect")
            return entry

        if current["lifecycle"] != "CURRENT":
            entry["resolution"] = "HISTORICAL_ONLY"
            entry["historical_value"] = payload
            entry["notes"].append(
                f"stored lifecycle is {current['lifecycle']}; returning it as history, not Current"
            )
            return entry

        if binding is None:
            entry["resolution"] = "UNKNOWN"
            entry["notes"].append("no applicable AuthorityBinding for this scope")
            return entry

        if freshness in {"STALE", "NOT_CHECKED"}:
            # Q2 is OPEN in the contract. Conservative P1 semantics: expose a
            # clearly-labelled last-verified snapshot, never "latest Current".
            entry["value"] = payload
            entry["resolution"] = "SNAPSHOT_ONLY"
            entry["is_latest_verified"] = False
            entry["authority"] = authority
            entry["q2_branch"] = "SPEC_OPEN_Q2"
            entry["notes"].append(
                "served as last verified snapshot; freshness is not verified against the"
                " current source head"
            )
            entry["source_head_revision"] = head_rev
            if mode == "strict_latest":
                entry["strict_latest_failed"] = True
            return entry

        if freshness != "FRESH":
            entry["resolution"] = "UNKNOWN"
            entry["notes"].append(f"freshness {freshness!r} does not support a Current claim")
            return entry

        entry["value"] = payload
        entry["resolution"] = "CURRENT"
        entry["is_latest_verified"] = True
        return entry

    # ---------------------------------------------------------- 1. resolve
    def resolve_object(self, query: str, scope: str = "global") -> dict:
        req = {"operation": "resolve_object", "query": query, "scope": scope}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))

        path.append("index:exact_object_id")
        obj = self._store.get_object(query)
        if obj:
            scope_err = self._check_object_scope(obj, scope)
            if scope_err:
                return self._error(req, path, scope_err["code"], scope_err["message"],
                                   detail=scope_err.get("detail"))
            return self._envelope(
                req,
                Status.OK,
                path + ["object:found"],
                result={
                    "resolution": "RESOLVED",
                    "match_reason": "exact_object_id",
                    "object": obj,
                    "candidates": [],
                },
                authority=None,
            )

        path.append("index:native_source_id")
        objs = self._store.find_objects_by_native_id(query)
        if len(objs) == 1:
            return self._envelope(
                req,
                Status.OK,
                path + ["object:found"],
                result={
                    "resolution": "RESOLVED",
                    "match_reason": "native_source_id",
                    "object": objs[0],
                    "candidates": [],
                },
            )

        path.append("index:display_name_and_observation")
        candidates = self._store.search_objects(query, scope)
        if not candidates and scope != "global":
            # Task 008: term-level full-text fallback (a whole-string LIKE misses
            # multi-word and natural-language queries).
            path.append("index:fts5_trigram")
            found = search_index.search(self._store.conn, query, scope, limit=20)
            best: dict[str, float] = {}
            for hit in found["hits"]:
                best[hit["object_id"]] = max(best.get(hit["object_id"], 0), hit["score"])
            top = max(best.values(), default=0) or 1
            for object_id, score in sorted(best.items(), key=lambda kv: -kv[1])[:5]:
                obj = self._store.get_object(object_id)
                if obj:
                    candidates.append({**obj, "score": round(score / top, 3),
                                       "match_reason": "full_text"})
        if candidates:
            return self._envelope(
                req,
                Status.OK,
                path + ["candidates:ranked"],
                result={
                    "resolution": "CANDIDATES",
                    "match_reason": "text_match",
                    "object": None,
                    "candidates": candidates,
                    "relevance_only": True,
                    "note": "text relevance is a discovery signal; it confers no Authority",
                },
            )

        return self._envelope(
            req,
            Status.NOT_FOUND,
            path + ["object:none"],
            error={
                "code": Status.NOT_FOUND,
                "message": "no object matched this identifier inside the checked scope",
                "detail": {"checked_scope": scope, "absence_is_conclusive": False},
            },
        )

    # ------------------------------------------------------ 2. current state
    def get_current_state(
        self,
        object_id: str,
        aspect: str | None = None,
        scope: str = "global",
        as_of: str | None = None,
        mode: str = "strict_latest",
    ) -> dict:
        req = {
            "operation": "get_current_state",
            "object_id": object_id,
            "aspect": aspect,
            "scope": scope,
            "as_of": as_of,
            "mode": mode,
        }
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))

        path.append("object:resolve")
        obj = self._store.get_object(object_id)
        if obj is None:
            return self._envelope(
                req,
                Status.NOT_FOUND,
                path + ["object:none"],
                error={
                    "code": Status.NOT_FOUND,
                    "message": "object_id is not present in this isolated database",
                    "detail": {"absence_is_conclusive": False},
                },
            )
        scope_err = self._check_object_scope(obj, scope)
        if scope_err:
            return self._error(req, path, scope_err["code"], scope_err["message"],
                               detail=scope_err.get("detail"))

        path.append("authority:binding")
        if aspect:
            names = [aspect]
        else:
            names = [a["name"] for a in self._store.get_aspects(object_id, current_only=True)]
            if not names:
                names = self._store.list_aspect_names(object_id)
        if not names:
            return self._envelope(
                req, Status.UNKNOWN, path + ["aspect:none"],
                error={"code": Status.UNKNOWN, "message": "object has no stored aspects"},
            )

        entries = [self._read_aspect(obj, name, scope, mode, as_of) for name in names]

        # Execution status: the query itself can succeed while the fact is
        # Historical/Conflicted. Only unresolved/blocked reads downgrade it.
        resolved = [e for e in entries if e["resolution"] == "CURRENT"]
        if len(entries) == 1:
            e = entries[0]
            status_map = {
                "CURRENT": Status.OK,
                "HISTORICAL_ONLY": Status.OK,
                "BLOCKED": Status.BLOCKED,
                "MISSING": Status.MISSING,
                "INVALID_SCOPE": Status.INVALID_SCOPE,
                "CONFLICTED": Status.CONFLICTED,
                "UNKNOWN": Status.UNKNOWN,
                "NOT_RESOLVED": Status.UNKNOWN,
            }
            status = status_map.get(e["resolution"], Status.UNKNOWN)
            if e["resolution"] == "SNAPSHOT_ONLY":
                if mode != "strict_latest":
                    status = Status.OK
                elif e["freshness"] == "NOT_CHECKED":
                    status = Status.NOT_CHECKED
                else:
                    status = Status.STALE
        else:
            status = Status.OK if len(resolved) == len(entries) else Status.PARTIAL

        warnings: list[str] = []
        if any(e.get("q2_branch") == "SPEC_OPEN_Q2" for e in entries):
            warnings.append(
                "Q2_SPEC_OPEN: a last-verified snapshot is shown; it is not asserted to be"
                " the latest Current"
            )
        if any(e["resolution"] == "HISTORICAL_ONLY" for e in entries):
            warnings.append("HISTORICAL_NOT_CURRENT: historical versions were not promoted")
        if any(e["resolution"] == "CONFLICTED" for e in entries):
            warnings.append("CONFLICTED: no single Current value was selected")

        primary = next((e for e in entries if e["name"] == aspect), entries[0])
        state = {
            "aspect": primary["name"],
            "value": primary["value"],
            "lifecycle": primary["lifecycle"],
            "resolution": primary["resolution"],
            "verification": primary["verification"],
            "freshness": primary["freshness"],
            "is_latest_verified": primary["is_latest_verified"],
            "revision": primary["revision"],
            "as_of": as_of or utcnow(),
        }
        evidence = _dedupe(
            [e for entry in entries for e in entry["source_refs"]], "source_ref_id"
        )
        authority = primary["authority"]

        return self._envelope(
            req,
            status,
            path + ["aspect:read", "source:freshness", "conflict:scan"],
            result={"object": obj, "aspects": entries},
            authority=authority,
            state=state,
            evidence=evidence,
            conflicts=primary.get("conflict_candidates", []),
            warnings=warnings,
        )

    # ------------------------------------------------- 3. relation traversal
    def traverse_relations(
        self,
        object_id: str,
        relation_types: list[str] | None = None,
        direction: str = "both",
        max_depth: int = 2,
        scope: str = "global",
        include_candidates: bool = False,
    ) -> dict:
        req = {
            "operation": "traverse_relations",
            "object_id": object_id,
            "relation_types": relation_types,
            "direction": direction,
            "max_depth": max_depth,
            "scope": scope,
            "include_candidates": include_candidates,
        }
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))
        if direction not in {"out", "in", "both"}:
            return self._error(req, path, Status.INVALID_SCOPE,
                               f"direction {direction!r} is not one of out/in/both")
        if max_depth < 1:
            return self._error(req, path, Status.INVALID_SCOPE, "max_depth must be >= 1")
        warnings: list[str] = []
        if max_depth > 3:
            warnings.append("max_depth capped at 3 for P1")
            max_depth = 3

        path.append("object:resolve")
        obj = self._store.get_object(object_id)
        if obj is None:
            return self._envelope(
                req, Status.NOT_FOUND, path + ["object:none"],
                error={"code": Status.NOT_FOUND,
                       "message": "traversal start object was not found",
                       "detail": {"absence_is_conclusive": False}},
            )
        scope_err = self._check_object_scope(obj, scope)
        if scope_err:
            return self._error(req, path, scope_err["code"], scope_err["message"],
                               detail=scope_err.get("detail"))

        path.append("relation:expand")
        nodes: dict[str, dict] = {
            object_id: {"object_id": object_id, "depth": 0, "display_name": obj["display_name"],
                        "type": obj["type"]}
        }
        edges: list[dict] = []
        excluded: list[dict] = []
        seen_edges: set[str] = set()
        frontier: list[tuple[str, int, list[str]]] = [(object_id, 0, [object_id])]
        while frontier:
            node, depth, node_path = frontier.pop(0)
            if depth >= max_depth:
                continue
            for rel in self._store.get_relations(node, relation_types,
                                                 None if scope == "global" else scope):
                if rel["from_id"] == node:
                    edge_direction = "out"
                    other = rel["to_id"]
                elif rel["to_id"] == node:
                    edge_direction = "in"
                    other = rel["from_id"]
                else:
                    continue
                if direction != "both" and edge_direction != direction:
                    continue
                edge_key = f"{rel['relation_id']}:{edge_direction}"
                if edge_key in seen_edges:
                    continue
                verified_claim = rel["verification"] == "VERIFIED"
                verified = verified_claim and self._relation_active(rel)
                if not verified and not include_candidates:
                    excluded.append(
                        {
                            "relation_id": rel["relation_id"],
                            "relation_type": rel["relation_type"],
                            "from_id": rel["from_id"],
                            "to_id": rel["to_id"],
                            "verification": rel["verification"],
                            "excluded_reason": (
                                "UNVERIFIED_NOT_EFFECTIVE" if not verified_claim
                                else "INACTIVE_RELATION"),
                        }
                    )
                    seen_edges.add(edge_key)
                    continue
                seen_edges.add(edge_key)
                if other in nodes and nodes[other]["depth"] <= depth:
                    # Cycle / back-edge: do not re-emit an edge to a node that
                    # is already at the same or a shallower depth.
                    continue
                other_obj = self._store.get_object(other)
                edges.append(
                    {
                        "relation_id": rel["relation_id"],
                        "from_id": rel["from_id"],
                        "to_id": rel["to_id"],
                        "relation_type": rel["relation_type"],
                        "direction_from_start": edge_direction,
                        "verification": rel["verification"],
                        "scope": rel["scope"],
                        "effective": verified,
                        "candidate": not verified,
                        "inactive": verified_claim and not verified,
                        "valid_from": rel["valid_from"],
                        "valid_to": rel["valid_to"],
                        "provenance": _j(rel["provenance_refs"]) or [],
                        "path": node_path + [other],
                        "depth": depth + 1,
                        "other_object": {
                            "object_id": other,
                            "type": other_obj["type"] if other_obj else None,
                            "display_name": other_obj["display_name"] if other_obj else None,
                        },
                    }
                )
                if other not in nodes:
                    nodes[other] = {
                        "object_id": other,
                        "depth": depth + 1,
                        "display_name": other_obj["display_name"] if other_obj else None,
                        "type": other_obj["type"] if other_obj else None,
                    }
                    frontier.append((other, depth + 1, node_path + [other]))

        if excluded:
            warnings.append(
                f"{len(excluded)} unverified relation(s) excluded as non-effective;"
                " pass include_candidates=true to inspect them as candidates"
            )
        if not edges:
            warnings.append("NO_EFFECTIVE_RELATIONS: no verified edge matched this traversal")

        status = Status.OK if edges else Status.UNKNOWN
        included_candidates = sum(1 for edge in edges if edge.get("candidate"))
        return self._envelope(
            req,
            status,
            path + ["relation:verified_filter", "relation:bfs"],
            result={
                "start": obj,
                "max_depth": max_depth,
                "nodes": list(nodes.values()),
                "edges": edges,
                "excluded_candidates": excluded,
                # Total candidate edges this traversal knows about: the ones
                # returned (when include_candidates=true) plus the ones filtered
                # out.  Counting only the excluded ones made this field say "0"
                # while candidate edges were being returned, which misled an
                # agent into reporting an empty graph.
                "included_candidates": included_candidates,
                "candidate_count": included_candidates + len(excluded),
            },
            evidence=_dedupe(
                [
                    e
                    for edge in edges
                    for ref_id in edge["provenance"]
                    for e in [self._source_evidence(ref_id)]
                    if e
                ],
                "source_ref_id",
            ),
            warnings=warnings,
        )

    # ------------------------------------------------ 3b. discovery helpers
    def list_objects(self, scope: str = "global", object_type: str | None = None,
                     limit: int = 100) -> dict:
        """Catalog of object identities inside one scope (no payloads).

        Added for cold-start navigation: an Agent that does not know any object
        id cannot enumerate the corpus otherwise.  Only identity metadata is
        returned — never aspects, payloads, sources or evidence — and the scope
        rule is the same as every other read (global never sees scoped objects).
        """
        req = {"operation": "list_objects", "scope": scope,
               "object_type": object_type, "limit": limit}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"],
                               detail=err.get("detail"))
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return self._error(req, path, Status.INVALID_SCOPE,
                               "limit must be an integer")
        limit = max(1, min(500, limit))
        sql = ("SELECT object_id, type, display_name FROM objects WHERE scope = ?")
        params: list[Any] = [scope]
        if object_type:
            sql += " AND type = ?"
            params.append(object_type)
        sql += " ORDER BY type, object_id LIMIT ?"
        params.append(limit + 1)
        rows = [dict(r) for r in self._store.conn.execute(sql, params).fetchall()]
        truncated = len(rows) > limit
        rows = rows[:limit]
        path.append("object:catalog")
        known_types = [r["type"] for r in self._store.conn.execute(
            "SELECT DISTINCT type FROM objects WHERE scope = ? ORDER BY type",
            (scope,)).fetchall()]
        return self._envelope(
            req, Status.OK, path + ["anchor:return"],
            result={
                "scope": scope,
                "objects": [
                    {"object_id": r["object_id"], "type": r["type"],
                     "display_name": r["display_name"]}
                    for r in rows
                ],
                "count": len(rows),
                "truncated": truncated,
                "types_available": known_types,
                "content_withheld": ["aspects", "payloads", "source_refs",
                                     "observations", "evidence"],
            },
        )

    def list_relation_types(self, scope: str = "global") -> dict:
        """Which typed relations exist in this scope, before traversing.

        A cold-start agent has no way to guess AUTHORED / SPONSORED_BY /
        GOVERNED_BY; guessing produced a false "the graph is empty" conclusion
        in Task 006.  This returns the vocabulary and its verification mix,
        without returning any edge endpoints.
        """
        req = {"operation": "list_relation_types", "scope": scope}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"],
                               detail=err.get("detail"))
        rows = [dict(r) for r in self._store.conn.execute(
            "SELECT relation_type, verification, COUNT(*) AS n FROM relations"
            " WHERE scope = ? AND expired_at IS NULL GROUP BY relation_type, verification"
            " ORDER BY relation_type, verification", (scope,)).fetchall()]
        types: dict[str, dict] = {}
        for row in rows:
            entry = types.setdefault(row["relation_type"], {
                "relation_type": row["relation_type"], "count": 0,
                "by_verification": {}})
            entry["count"] += row["n"]
            entry["by_verification"][row["verification"]] = row["n"]
        path.append("relation:vocabulary")
        return self._envelope(
            req, Status.OK, path + ["anchor:return"],
            result={
                "scope": scope,
                "relation_types": list(types.values()),
                "count": len(types),
                "total_relations": sum(t["count"] for t in types.values()),
            },
        )

    def search(self, query: str, scope: str = "global", kinds: list[str] | None = None,
               limit: int = 10) -> dict:
        """Full-text discovery across objects, paragraphs, observations, Current aspects.

        Returns ids, anchors and short snippets only — never full bodies. A hit
        is a pointer to read next (``get_research_evidence`` for paragraphs,
        ``get_current_state`` for aspects, ``trace_evidence`` for provenance),
        not a fact. Snippets from a source that is not readable in this scope
        are withheld.
        """
        req = {"operation": "search", "query": query, "scope": scope,
               "kinds": kinds, "limit": limit}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            return self._error(req, path, Status.INVALID_SCOPE, "limit must be an integer")
        if isinstance(kinds, str):
            kinds = [kinds]
        bad = [k for k in (kinds or []) if k not in search_index.KINDS]
        if bad:
            return self._error(req, path, Status.INVALID_SCOPE, f"unknown kind(s): {bad}",
                               detail={"allowed_kinds": list(search_index.KINDS)})
        if not (query or "").strip():
            return self._error(req, path, Status.INVALID_SCOPE, "query must not be empty")
        path.append("index:fts5_trigram")
        found = search_index.search(self._store.conn, query, scope, kinds, limit)
        withheld = 0
        for hit in found["hits"]:
            ref = self._store.get_source_ref(hit["source_ref_id"]) if hit["source_ref_id"] else None
            if ref is not None and (ref.get("status") != "OK" or ref.get("access_scope") != scope):
                hit["snippet"] = None
                hit["anchor"] = None
                hit["snippet_withheld"] = ref.get("status") if ref.get("status") != "OK" \
                    else "SOURCE_SCOPE_DENIED"
                withheld += 1
            hit["read_next"] = {
                "paragraph": "get_research_evidence(object_id)",
                "observation": "trace_evidence(item_id)",
                "aspect": "get_current_state(object_id, aspect)",
                "object": "get_context_pack(question, object_id)",
                "native": "nexus_read(object_id) (Nexus-native document)",
            }[hit["kind"]]
        status = Status.OK if found["hits"] else Status.NOT_FOUND
        warnings = []
        if withheld:
            warnings.append(f"SNIPPET_WITHHELD: {withheld} hit(s) point at a source that is"
                            " not readable in this scope")
        result = {"scope": scope, "query_terms": found["terms"], "hits": found["hits"],
                  "returned": len(found["hits"]), "total_candidates": found["total_candidates"],
                  "truncated": found["total_candidates"] > len(found["hits"]),
                  "relevance_only": True,
                  "note": "text relevance is a discovery signal; it confers no Authority"}
        if found.get("semantic"):
            result["semantic"] = found["semantic"]
        if not found["hits"]:
            return self._envelope(req, status, path + ["hits:none"], result=result,
                                  error={"code": Status.NOT_FOUND,
                                         "message": "no indexed text matched inside this scope",
                                         "detail": {"checked_scope": scope,
                                                    "absence_is_conclusive": False}},
                                  warnings=warnings)
        return self._envelope(req, status, path + ["hits:ranked"], result=result,
                              warnings=warnings)

    # ----------------------------------------------------- 4. trace evidence
    def _relation_active(self, rel: dict, as_of: str | None = None) -> bool:
        now = as_of or utcnow()
        return (
            (rel["valid_from"] is None or rel["valid_from"] <= now)
            and (rel["valid_to"] is None or rel["valid_to"] > now)
        )

    def _ref_access(self, refs: list[dict | None],
                    expected_scope: str | None = None) -> tuple[str, list[str]]:
        """All-source access decision; never just the first SourceRef."""
        if not refs:
            return "OK", []
        inaccessible: list[str] = []
        statuses: list[str] = []
        for ref in refs:
            if ref is None:
                statuses.append("MISSING")
                inaccessible.append("MISSING")
                continue
            if expected_scope and ref["access_scope"] != expected_scope:
                # A SourceRef from another scope must never be served through
                # an object/relation/observation indirection.
                statuses.append("INVALID_SCOPE")
                inaccessible.append(ref["source_ref_id"])
                continue
            status = ref["status"]
            if status in {"BLOCKED", "MISSING"}:
                statuses.append(status)
                inaccessible.append(ref["source_ref_id"])
            else:
                statuses.append("OK")
        if not inaccessible:
            return "OK", []
        if "INVALID_SCOPE" in statuses:
            return "INVALID_SCOPE", inaccessible
        if len(inaccessible) == len(refs):
            if all(s == "BLOCKED" for s in statuses):
                return "BLOCKED", inaccessible
            if all(s == "MISSING" for s in statuses):
                return "MISSING", inaccessible
            return "PARTIAL", inaccessible
        return "PARTIAL", inaccessible

    def trace_evidence(self, item_id: str, scope: str = "global", expand: bool = False) -> dict:
        req = {"operation": "trace_evidence", "item_id": item_id, "scope": scope,
               "expand": expand}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))

        path.append("evidence:lookup")
        kind = None
        refs: list[dict | None] = []
        item: dict[str, Any] = {}
        object_id: str | None = None
        expected_scope: str | None = None

        ref = self._store.get_source_ref(item_id)
        if ref:
            kind = "source_ref"
            refs = [ref]
            expected_scope = ref["access_scope"]
            if ref["access_scope"] != scope:
                return self._error(req, path, Status.INVALID_SCOPE,
                                   "SourceRef is outside the requested scope")
            item = {"kind": "source_ref",
                    "source_ref": self._source_evidence(item_id,
                                                        redact=ref["status"] in
                                                        {"BLOCKED", "MISSING"})}
        else:
            rel = self._store.get_relation(item_id)
            if rel:
                kind = "relation"
                if rel["scope"] != scope:
                    return self._error(req, path, Status.INVALID_SCOPE,
                                       "Relation is outside the requested scope")
                from_obj = self._store.get_object(rel["from_id"])
                to_obj = self._store.get_object(rel["to_id"])
                if any(o is None or o["scope"] != scope
                       for o in (from_obj, to_obj)):
                    return self._error(req, path, Status.INVALID_SCOPE,
                                       "Relation endpoint is outside the requested scope")
                ref_ids = _j(rel["provenance_refs"]) or []
                refs = [self._store.get_source_ref(r) for r in ref_ids]
                expected_scope = rel["scope"]
                item = {
                    "kind": "relation",
                    "relation": {
                        "relation_id": rel["relation_id"],
                        "from_id": rel["from_id"],
                        "to_id": rel["to_id"],
                        "relation_type": rel["relation_type"],
                        "verification": rel["verification"],
                        "scope": rel["scope"],
                        "provenance_refs": ref_ids,
                        "valid_from": rel["valid_from"],
                        "valid_to": rel["valid_to"],
                    },
                }
            else:
                row = None
                if item_id.startswith("aspect:") and "/" in item_id[len("aspect:"):]:
                    # Stable id `aspect:<object_id>/<aspect>` as returned by the
                    # context packs.  Resolving it here keeps one identity for
                    # the same fact across pack, trace and research reads; the
                    # object scope is still enforced below.
                    path.append("evidence:stable_aspect_id")
                    owner_id, name = item_id[len("aspect:"):].rsplit("/", 1)
                    row = self._store.conn.execute(
                        "SELECT version_id, object_id, name, revision, lifecycle,"
                        " verification, is_current, source_ref_id FROM aspects"
                        " WHERE object_id = ? AND name = ?"
                        " ORDER BY is_current DESC, revision DESC LIMIT 1",
                        (owner_id, name),
                    ).fetchone()
                if row is None:
                    row = self._store.conn.execute(
                        "SELECT version_id, object_id, name, revision, lifecycle,"
                        " verification, is_current, source_ref_id FROM aspects"
                        " WHERE version_id = ?",
                        (item_id,),
                    ).fetchone()
                if row:
                    kind = "aspect_version"
                    object_id = row["object_id"]
                    refs = [self._store.get_source_ref(row["source_ref_id"])
                            if row["source_ref_id"] else None]
                    item = {"kind": "aspect_version",
                            "aspect": {
                                "version_id": row["version_id"],
                                "object_id": row["object_id"],
                                "name": row["name"],
                                "revision": row["revision"],
                                "lifecycle": row["lifecycle"],
                                "verification": row["verification"],
                                # A non-current version stays non-current: tracing
                                # evidence never promotes it to a Fact.
                                "is_current": bool(row["is_current"]),
                                "source_ref_id": row["source_ref_id"],
                            }}
                else:
                    obs = self._store.conn.execute(
                        "SELECT observation_id, object_id, dimension, stance,"
                        " source_ref_ids FROM observations WHERE observation_id = ?",
                        (item_id,),
                    ).fetchone()
                    if not obs:
                        return self._envelope(
                            req, Status.NOT_FOUND, path + ["evidence:none"],
                            error={"code": Status.NOT_FOUND,
                                   "message": "no evidence record matched this id",
                                   "detail": {"checked_scope": scope}},
                        )
                    kind = "observation"
                    object_id = obs["object_id"]
                    ref_ids = _j(obs["source_ref_ids"]) or []
                    refs = [self._store.get_source_ref(r) for r in ref_ids]
                    item = {"kind": "observation",
                            "observation": {
                                "observation_id": obs["observation_id"],
                                "object_id": obs["object_id"],
                                "dimension": obs["dimension"],
                                "stance": obs["stance"],
                                "source_ref_ids": ref_ids,
                            }}

        if object_id is not None:
            target = self._store.get_object(object_id)
            if target is None:
                return self._error(req, path, Status.NOT_FOUND,
                                   "target object was not found")
            if target["scope"] != scope:
                return self._error(req, path, Status.INVALID_SCOPE,
                                   "target object is outside the requested scope")
            expected_scope = target["scope"]

        access, inaccessible = self._ref_access(refs, expected_scope)
        warnings: list[str] = []
        if access != "OK":
            warnings.append(
                "EVIDENCE_ACCESS_INCOMPLETE: one or more SourceRefs are not readable;"
                " protected statement/detail/payload/section content is withheld")
        if kind == "observation" and access == "OK":
            obs_row = self._store.conn.execute(
                "SELECT statement, detail FROM observations WHERE observation_id = ?",
                (item_id,),
            ).fetchone()
            if obs_row:
                item["observation"]["statement"] = obs_row["statement"]
                item["observation"]["detail"] = obs_row["detail"]

        sections: list[dict] = []
        provider_io = {"drive_api_calls": 0, "source_documents_read": 0,
                       "source_chars_returned": 0, "full_source_reads": 0}
        if expand:
            path.append("source:expand_section")
            if access != "OK":
                path.append("source:access_blocked")
            else:
                for ref_obj in refs:
                    if ref_obj is None:
                        continue
                    for sec in self._store.get_sections(ref_obj["source_ref_id"]):
                        sections.append({
                            "source_ref_id": sec["source_ref_id"],
                            "anchor": sec["anchor"],
                            "heading": sec["heading"],
                            "body": sec["body"],
                        })
                provider_io["source_documents_read"] = len(
                    {s["source_ref_id"] for s in sections})
                provider_io["source_chars_returned"] = sum(
                    len(s["body"]) for s in sections)
                if not sections:
                    path.append("source:no_section_index")

        evidence = []
        for r in refs:
            if r is None:
                continue
            redact = (r["status"] in {"BLOCKED", "MISSING"}
                      or (expected_scope is not None
                          and r["access_scope"] != expected_scope))
            ev = self._source_evidence(r["source_ref_id"], redact=redact)
            if ev:
                evidence.append(ev)
        status = Status.OK if access == "OK" else access
        return self._envelope(
            req,
            status,
            path + ["anchor:return"],
            result={"item": item, "sections": sections,
                    "inaccessible_source_refs": inaccessible,
                    "anchors_are_synthetic": True},
            evidence=evidence,
            warnings=warnings,
            provider_io=provider_io,
        )

    # --------------------------------------------------------- 5. get context
    def get_context(
        self,
        intent: str,
        object_id: str | None = None,
        aspects: list[str] | None = None,
        scope: str = "global",
        depth: int = 1,
        max_chars: int = 6000,
    ) -> dict:
        req = {"operation": "get_context", "intent": intent, "object_id": object_id,
               "aspects": aspects, "scope": scope, "depth": depth, "max_chars": max_chars}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))

        route = CONTEXT_ROUTES.get(intent)
        if route is None and not aspects:
            return self._error(
                req, path, Status.UNKNOWN,
                f"intent {intent!r} has no registered retrieval route",
                detail={"known_intents": sorted(CONTEXT_ROUTES)},
            )
        route = route or {}
        if isinstance(aspects, str):
            aspects = [aspects]
        if object_id is None:
            return self._error(req, path, Status.INVALID_SCOPE,
                               "object_id is required for this intent")
        path.append("object:resolve")
        obj = self._store.get_object(object_id)
        if obj is None:
            return self._envelope(
                req, Status.NOT_FOUND, path + ["object:none"],
                error={"code": Status.NOT_FOUND, "message": "context target object not found"},
            )
        scope_err = self._check_object_scope(obj, scope)
        if scope_err:
            return self._error(req, path, scope_err["code"], scope_err["message"],
                               detail=scope_err.get("detail"))

        wanted_aspects = list(aspects or (route.get("aspects", []) + route.get("extra_aspects", [])))
        for required in REQUIRED_ASPECTS_BY_INTENT.get(intent, []):
            if required not in wanted_aspects:
                wanted_aspects.append(required)
        dimensions = route.get("dimensions", [])
        warnings: list[str] = []
        provider_io = {"drive_api_calls": 0, "source_documents_read": 0,
                       "source_chars_returned": 0, "full_source_reads": 0}

        path.append("navigation:identity")
        navigation = {
            "object_id": obj["object_id"],
            "type": obj["type"],
            "display_name": obj["display_name"],
            "scope": obj["scope"],
            "available_aspects": [a["name"] for a in
                                  self._store.get_aspects(object_id, current_only=True)],
        }

        path.append("aspect:read")
        entries = []
        for name in wanted_aspects:
            owner = self._resolve_aspect_owner(obj, name)
            entry = self._read_aspect(owner, name, scope, "snapshot_ok")
            if owner["object_id"] != obj["object_id"]:
                entry["object_id"] = owner["object_id"]
            entries.append(entry)

        package: dict[str, Any] = {
            "navigation": navigation,
            "aspects": {e["name"]: e for e in entries},
        }
        state_primary = entries[0] if entries else None

        obs_refs: list[dict] = []
        withheld_observations: list[dict] = []
        total_observations = 0
        if route.get("dimensions") or dimensions:
            path.append("observation:by_dimension")
            obs = self._store.get_observations(object_id, dimensions or None)
            total_observations = len(obs)
            compact_obs = []
            for o in obs:
                ref_ids = _j(o["source_ref_ids"]) or []
                refs = [self._store.get_source_ref(r) for r in ref_ids]
                if not refs:
                    withheld_observations.append(
                        {"observation_id": o["observation_id"], "status": "NOT_CHECKED"})
                    continue
                if any(r is not None and r["access_scope"] != obj["scope"] for r in refs):
                    withheld_observations.append(
                        {"observation_id": o["observation_id"], "status": "INVALID_SCOPE"})
                    continue
                if any(r is None or r["status"] == "BLOCKED" for r in refs):
                    withheld_observations.append(
                        {"observation_id": o["observation_id"], "status": "BLOCKED"})
                    continue
                if any(r["status"] == "MISSING" for r in refs):
                    withheld_observations.append(
                        {"observation_id": o["observation_id"], "status": "MISSING"})
                    continue
                compact_obs.append({
                    "observation_id": o["observation_id"],
                    "object_id": o["object_id"],
                    "dimension": o["dimension"],
                    "statement": o["statement"],
                    "stance": o["stance"],
                    "detail": o["detail"],
                    "source_ref_ids": ref_ids,
                })
                obs_refs.extend(e for e in
                                (self._source_evidence(r) for r in ref_ids) if e)
            package["observations"] = compact_obs
            if withheld_observations:
                package["withheld_observations"] = withheld_observations
                counts: dict[str, int] = {}
                for row in withheld_observations:
                    counts[row["status"]] = counts.get(row["status"], 0) + 1
                warnings.append(
                    "OBSERVATIONS_WITHHELD: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                )

        if route.get("relations"):
            path.append("relation:relevant")
            rels = self._store.get_relations(object_id, route["relations"],
                                             None if scope == "global" else scope)
            now = utcnow()
            verified = [
                r for r in rels
                if r["verification"] == "VERIFIED" and self._relation_active(r, now)
            ]
            candidates = [r for r in rels if r not in verified]
            package["relations"] = [
                {
                    "relation_id": r["relation_id"],
                    "type": r["relation_type"],
                    "from_id": r["from_id"],
                    "to_id": r["to_id"],
                    "verification": r["verification"],
                    "effective": True,
                }
                for r in verified
            ]
            if candidates:
                package["relation_candidates"] = [
                    {
                        "relation_id": r["relation_id"],
                        "type": r["relation_type"],
                        "from_id": r["from_id"],
                        "to_id": r["to_id"],
                        "verification": r["verification"],
                        "effective": False,
                        "candidate": True,
                        "inactive": (r["verification"] == "VERIFIED"
                                     and not self._relation_active(r, now)),
                    }
                    for r in candidates
                ]
                warnings.append(
                    f"UNVERIFIED_RELATIONS_EXCLUDED: {len(candidates)} relation(s) returned"
                    " only as candidates, not as effective context"
                )

        policy_status = "NOT_REQUESTED"
        if route.get("policy"):
            path.append("policy:constraint")
            policy, policy_status = self._policy_for(obj)
            package["policy"] = policy
            package["policy_status"] = policy_status
            if policy is None:
                warnings.append(f"POLICY_WITHHELD:{policy_status}")

        if route.get("events"):
            path.append("events:recent")
            package["events"] = [
                {
                    "event_id": e["event_id"],
                    "event_type": e["event_type"],
                    "occurred_at": e["occurred_at"],
                    "authority_owner": e["authority_owner"],
                    "outcome": _j(e["outcome"]),
                }
                for e in self._store.recent_events(object_id, limit=5)
            ]

        exceptions = []
        for entry in entries:
            value = entry.get("value") or {}
            for key in ("decisive_exception", "exception", "exceptions", "boundary"):
                if key in value:
                    exceptions.append(
                        {"aspect": entry["name"], "field": key, "content": value[key]}
                    )
        if exceptions:
            package["decisive_exceptions"] = exceptions

        # Evidence layer is optional: only load on explicit need (depth >= 2),
        # ambiguous verification, or an intent that asks for sourcing.
        needs_evidence = (
            route.get("evidence")
            or depth >= 2
            or any(e["verification"] in {"CASE", "INFERENCE", "TO_TEST", "UNVERIFIED"}
                   for e in entries)
        )
        if needs_evidence:
            path.append("evidence:refs")
            refs = _dedupe(
                [r for e in entries for r in e["source_refs"]] + obs_refs,
                "source_ref_id",
            )
            if depth >= 2:
                path.append("source:expand_on_demand")
                # Expand only the sections that carry a decisive exception or
                # quality boundary, not every section of the source document.
                target_refs: list[str] = []
                for entry in entries:
                    value = entry.get("value") or {}
                    if ("decisive_exception" in value or "boundary" in value) and entry["source_refs"]:
                        ref_id = entry["source_refs"][0]["source_ref_id"]
                        ref = self._store.get_source_ref(ref_id)
                        if ref and ref["status"] not in {"BLOCKED", "MISSING"}:
                            target_refs.append(ref_id)
                if not target_refs:
                    for r in refs[:1]:
                        ref = self._store.get_source_ref(r["source_ref_id"])
                        if ref and ref["status"] not in {"BLOCKED", "MISSING"}:
                            target_refs.append(r["source_ref_id"])
                sections = []
                for ref_id in dict.fromkeys(target_refs):
                    sections.extend(self._store.get_sections(ref_id))
                provider_io["source_documents_read"] = len({s["source_ref_id"] for s in sections})
                provider_io["source_chars_returned"] = sum(s["char_count"] for s in sections)
                package["expanded_sections"] = sections

        unverified = [o for o in package.get("observations", [])
                      if o["stance"] in {"CASE", "INFERENCE", "TO_TEST"}]
        if unverified:
            package["epistemic_boundaries"] = [
                {"observation_id": o["observation_id"], "stance": o["stance"],
                 "usable_as_verified_fact": False}
                for o in unverified
            ]
            warnings.append(
                "STANCE_SEPARATION: author self-report / inference / to-test items are not"
                " independent verification"
            )

        missing = [e["name"] for e in entries if e["resolution"] != "CURRENT"]
        if missing:
            warnings.append(f"ASPECTS_NOT_CURRENT: {', '.join(missing)}")

        timecode_missing = False
        provenance_refs = [r for e in entries for r in e["source_refs"]] + obs_refs
        if any((r.get("anchor") or "") in {"NO_TIMECODE", "TIME_CODE_MISSING"}
               for r in provenance_refs):
            timecode_missing = True
            warnings.append(
                "TIMECODE_MISSING: exact video timestamps are unavailable; anchors are not"
                " fabricated and this result is PARTIAL"
            )

        # De-duplicate evidence metadata: the full SourceRef list appears once
        # at envelope level; aspects/observations reference it by id.
        compact_aspects: dict[str, Any] = {}
        for entry in entries:
            compact = {k: v for k, v in entry.items() if k != "source_refs"}
            compact["source_ref_ids"] = [r["source_ref_id"] for r in entry["source_refs"]]
            compact_aspects[entry["name"]] = compact
        package["aspects"] = compact_aspects
        package.pop("evidence", None)
        evidence = _dedupe(
            [r for e in entries for r in e["source_refs"]] + obs_refs,
            "source_ref_id",
        )

        payload_text = canonical_json(package)
        if len(payload_text) > max_chars:
            # Never drop decisive exceptions or the requested aspects. Drop
            # expanded source bodies first, then optional relations/events.
            if "expanded_sections" in package:
                package.pop("expanded_sections")
                warnings.append("CONTEXT_TRIMMED_EVIDENCE: expanded source bodies omitted")
                payload_text = canonical_json(package)
        budget_exceeded = len(payload_text) > max_chars
        if budget_exceeded:
            warnings.append("CONTEXT_BUDGET_EXCEEDED: required aspects kept, optional material"
                            " omitted")
        package["budget"] = {"max_chars": max_chars, "exceeded": budget_exceeded}

        obs_status = "OK"
        if withheld_observations:
            statuses = {row["status"] for row in withheld_observations}
            if "INVALID_SCOPE" in statuses:
                obs_status = "INVALID_SCOPE"
            elif len(withheld_observations) == total_observations:
                if statuses == {"BLOCKED"}:
                    obs_status = "BLOCKED"
                elif statuses == {"MISSING"}:
                    obs_status = "MISSING"
                else:
                    obs_status = "PARTIAL"
            else:
                # Some observations are still returned, but the requested
                # dimension is not complete.
                obs_status = "PARTIAL"

        policy_agg = "OK"
        if route.get("policy") and policy_status != "CURRENT":
            if policy_status == "INVALID_SCOPE":
                policy_agg = "INVALID_SCOPE"
            elif policy_status == "BLOCKED":
                policy_agg = "BLOCKED"
            elif policy_status == "MISSING":
                policy_agg = "MISSING"
            else:
                policy_agg = "PARTIAL"

        status = Status.OK
        aspect_statuses = {e["resolution"] for e in entries}
        if ("INVALID_SCOPE" in aspect_statuses or obs_status == "INVALID_SCOPE"
                or policy_agg == "INVALID_SCOPE"):
            status = Status.INVALID_SCOPE
        elif ("BLOCKED" in aspect_statuses or obs_status == "BLOCKED"
              or policy_agg == "BLOCKED"):
            status = Status.BLOCKED
        elif ("MISSING" in aspect_statuses or obs_status == "MISSING"
              or policy_agg == "MISSING"):
            status = Status.MISSING
        elif missing or obs_status == "PARTIAL" or policy_agg == "PARTIAL":
            status = Status.PARTIAL
        elif timecode_missing:
            status = Status.PARTIAL
        # Budget truncation is diagnostic only; it must never rewrite a more
        # severe access/scope state to PARTIAL.
        if budget_exceeded and status == Status.OK:
            status = Status.PARTIAL

        return self._envelope(
            req,
            status,
            path,
            result={
                "intent": intent,
                "package": package,
                "package_chars": len(canonical_json(package)),
            },
            authority=state_primary["authority"] if state_primary else None,
            state={
                "aspect": state_primary["name"] if state_primary else None,
                "value": state_primary["value"] if state_primary else None,
                "resolution": state_primary["resolution"] if state_primary else None,
                "verification": state_primary["verification"] if state_primary else None,
                "freshness": state_primary["freshness"] if state_primary else None,
                "is_latest_verified": state_primary["is_latest_verified"] if state_primary else False,
            },
            evidence=evidence,
            missing=missing,
            warnings=warnings,
            provider_io=provider_io,
        )

    def _policy_for(self, obj: dict) -> tuple[dict | None, str]:
        rels = self._store.get_verified_relations(
            obj["object_id"], ["GOVERNED_BY"], obj["scope"])
        if not rels:
            return None, "UNKNOWN"
        statuses: list[str] = []
        for rel in rels:
            principle = self._store.get_object(rel["to_id"])
            if not principle:
                statuses.append("MISSING")
                continue
            entry = self._read_aspect(principle, "constraint", obj["scope"], "strict_latest")
            if entry["resolution"] == "CURRENT" and entry["value"] is not None:
                return {
                    "principle_object_id": principle["object_id"],
                    "display_name": principle["display_name"],
                    "constraint": entry["value"],
                    "source_refs": entry["source_refs"],
                }, "CURRENT"
            statuses.append(entry["resolution"])
        if "INVALID_SCOPE" in statuses:
            return None, "INVALID_SCOPE"
        if "BLOCKED" in statuses:
            return None, "BLOCKED"
        if "MISSING" in statuses:
            return None, "MISSING"
        return None, "PARTIAL"

    # ------------------------------------------------------- 6. event evaluation
    TRUSTED_INGEST_CALLERS = {"system:test-harness", "system:ingestion"}
    TRUSTED_USER_CALLERS = {"user:owner"}

    def evaluate_event(self, event_id: str, caller: str | None = None,
                       scope: str = "global") -> dict:
        req = {"operation": "evaluate_event", "event_id": event_id,
               "caller": caller, "scope": scope}
        path = ["scope:validate"]
        _, err = self._validate_scope(scope)
        if err:
            return self._error(req, path, err["code"], err["message"], detail=err.get("detail"))

        # Caller identity is supplied by the trusted local test harness, never
        # by an event field or a client-controlled parameter.
        if not caller or not isinstance(caller, str):
            return self._error(req, path, Status.AUTHORITY_DENIED,
                               "trusted caller identity is required")
        if caller not in (self.TRUSTED_USER_CALLERS | self.TRUSTED_INGEST_CALLERS):
            return self._error(req, path, Status.AUTHORITY_DENIED,
                               "caller is not a trusted principal")

        path.append("event:load")
        event = self._store.get_event(event_id)
        if event is None:
            return self._envelope(
                req, Status.NOT_FOUND, path + ["event:none"],
                error={"code": Status.NOT_FOUND, "message": "event not found"},
            )

        event_type = event["event_type"]
        if event_type == "USER_DECISION":
            if caller != event["authority_owner"]:
                return self._error(req, path, Status.AUTHORITY_DENIED,
                                   "caller does not own this user decision event")
        elif caller not in self.TRUSTED_INGEST_CALLERS:
            return self._error(req, path, Status.AUTHORITY_DENIED,
                               "only a trusted ingestion principal may process this event")

        object_id = event["object_id"]
        if not object_id:
            return self._error(req, path, Status.INVALID_SCOPE,
                               "event has no target object_id")
        target = self._store.get_object(object_id)
        if target is None:
            return self._envelope(
                req, Status.NOT_FOUND, path + ["object:none"],
                error={"code": Status.NOT_FOUND,
                       "message": "event target object was not found"},
            )
        scope_err = self._check_object_scope(target, scope)
        if scope_err:
            return self._error(req, path, scope_err["code"], scope_err["message"],
                               detail=scope_err.get("detail"))
        effective_scope = target["scope"]

        path.append("event:source_access")
        if event["source_ref_id"]:
            source = self._store.get_source_ref(event["source_ref_id"])
            if source is None:
                return self._error(req, path, Status.NOT_CHECKED,
                                   "event source reference is missing")
            if source["status"] in {"BLOCKED", "MISSING"}:
                return self._error(
                    req, path, source["status"],
                    "event source is not readable in this scope; event is not evaluated",
                )

        # Cached outcomes are re-authorized above (caller/scope/target/source)
        # before any historical outcome is disclosed. The raw payload is never
        # echoed, even for processed events.
        if event["processed_at"] and event["outcome"]:
            outcome = _j(event["outcome"])
            return self._envelope(
                req, Status.OK, path + ["event:cached"],
                result={"event": self._event_public(event), "outcome": outcome,
                        "idempotent": True},
                authority=None,
            )

        path.append("event:validity")
        if event["occurred_at"] > utcnow():
            return self._error(req, path, Status.INVALID_SCOPE,
                               "event occurred in the future")

        payload = _j(event["payload"]) or {}
        fact = payload.get("fact", payload)
        fingerprint = sha256_text(canonical_json(fact))
        path.append("event:fingerprint")

        duplicate = self._store.conn.execute(
            "SELECT * FROM known_facts WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if duplicate:
            outcome = {
                "is_new_material_delta": False,
                "action": "NONE",
                "reasons": ["DUPLICATE_FACT"],
                "decision_changed": False,
                "requires_user_decision": False,
                "impact_scope": [],
                "matched_fact": dict(duplicate)["first_event_id"],
            }
            with self._store.tx():
                self._mark_event(event_id, outcome)
            return self._envelope(
                req, Status.OK, path + ["delta:duplicate", "decision:unchanged"],
                result={"event": self._event_public(event), "outcome": outcome, "idempotent": False},
                warnings=["DUPLICATE_EVENT_NO_RE_DECISION"],
            )

        # Validate the whole decision before any write. Rejections are not
        # recorded, so they cannot poison known_facts for a later legal event.
        aspect = payload.get("aspect", "decision")
        binding = None
        current = None
        expected = None
        if event_type == "USER_DECISION":
            path.append("authority:check")
            binding = self._store.get_binding(object_id, aspect, effective_scope)
            if binding is None:
                return self._error(req, path + ["decision:rejected"],
                                   Status.AUTHORITY_DENIED,
                                   "no AuthorityBinding covers this aspect")
            if binding["owner"] != event["authority_owner"]:
                return self._error(
                    req, path + ["decision:rejected"], Status.AUTHORITY_DENIED,
                    "event authority_owner does not hold this Authority",
                    detail={"required_owner": binding["owner"]},
                )
            if binding["write_policy"] != "USER_ONLY":
                return self._error(
                    req, path + ["decision:rejected"], Status.AUTHORITY_DENIED,
                    "AuthorityBinding write_policy does not permit a user decision write",
                    detail={"write_policy": binding["write_policy"],
                            "required": "USER_ONLY"},
                )
            current = self._store.get_aspect(object_id, aspect)
            raw_expected = payload.get("expected_revision")
            expected = self._parse_expected_revision(raw_expected)
            path.append("version:check")
            if expected is None:
                return self._error(
                    req, path + ["decision:rejected"], Status.VERSION_CONFLICT,
                    "expected_revision must be a non-negative integer",
                    detail={"received": raw_expected},
                )
            if current is None:
                if expected != 0:
                    return self._error(
                        req, path + ["decision:rejected"], Status.VERSION_CONFLICT,
                        "expected_revision must be 0 when no Current exists",
                        detail={"expected": expected, "actual": None},
                    )
            elif expected != current["revision"]:
                return self._error(
                    req, path + ["decision:rejected"], Status.VERSION_CONFLICT,
                    "expected revision does not match stored revision",
                    detail={"expected": expected, "actual": current["revision"]},
                )

        path.append("delta:new_material")
        try:
            with self._store.tx():
                # Re-check inside the transaction so a concurrent writer cannot
                # slip between validation and update.
                if event_type == "USER_DECISION":
                    binding_now = self._store.get_binding(
                        object_id, aspect, effective_scope)
                    if (binding_now is None
                            or binding_now["owner"] != event["authority_owner"]
                            or binding_now["write_policy"] != "USER_ONLY"):
                        raise _EventRejected(
                            Status.AUTHORITY_DENIED,
                            "AuthorityBinding no longer permits this user decision write")
                    current_now = self._store.get_aspect(object_id, aspect)
                    if current_now is None:
                        if expected != 0:
                            raise _EventRejected(
                                Status.VERSION_CONFLICT,
                                "expected_revision must be 0 when no Current exists")
                    elif expected != current_now["revision"]:
                        raise _EventRejected(
                            Status.VERSION_CONFLICT,
                            "expected revision does not match stored revision")
                    applied = self._apply_decision(
                        event, object_id, aspect, payload, effective_scope)
                    outcome = {
                        "is_new_material_delta": True,
                        "action": "APPLIED",
                        "reasons": ["AUTHORIZED_USER_DECISION"],
                        "decision_changed": True,
                        "requires_user_decision": False,
                        "impact_scope": [aspect],
                        "previous_revision": current_now["revision"] if current_now else None,
                        "new_revision": applied["new_revision"],
                        "superseded": current_now["version_id"] if current_now else None,
                    }
                    self._record_known_fact(fingerprint, fact, event_id)
                    self._mark_event(event_id, outcome)
                    response = self._envelope(
                        req, Status.OK,
                        path + ["decision:applied", "aspect:new_version", "event:recorded"],
                        result={"event": self._event_public(event), "outcome": outcome, "new_aspect": applied},
                        authority={
                            "owner": binding["owner"],
                            "scope": binding["scope"],
                            "write_policy": binding["write_policy"],
                        },
                        warnings=["LOCAL_TEST_WRITE_ONLY: no Google Drive or production"
                                  " write occurred"],
                    )
                elif event_type == "OFFICIAL_REQUIREMENT":
                    affected = payload.get("affected_aspects", [])
                    outcome = {
                        "is_new_material_delta": True,
                        "action": "RE_EVALUATE",
                        "reasons": ["NEW_OFFICIAL_FACT"],
                        "decision_changed": False,
                        "requires_user_decision": True,
                        "impact_scope": affected,
                        "bounded_reevaluation": {"aspects": affected, "max_depth": 1},
                        "note": "an official fact is not a user decision; the target"
                                " state is unchanged",
                    }
                    self._record_known_fact(fingerprint, fact, event_id)
                    self._mark_event(event_id, outcome)
                    path.extend(["impact:bounded", "decision:unchanged_requires_user"])
                    response = self._envelope(
                        req, Status.OK, path,
                        result={"event": self._event_public(event), "outcome": outcome},
                        warnings=["OFFICIAL_FACT_DOES_NOT_OVERWRITE_USER_DECISION"],
                    )
                else:
                    outcome = {
                        "is_new_material_delta": True,
                        "action": "RECORDED",
                        "reasons": [f"UNHANDLED_EVENT_TYPE:{event_type}"],
                        "decision_changed": False,
                        "requires_user_decision": False,
                        "impact_scope": [],
                    }
                    self._record_known_fact(fingerprint, fact, event_id)
                    self._mark_event(event_id, outcome)
                    response = self._envelope(
                        req, Status.OK, path,
                        result={"event": self._event_public(event), "outcome": outcome})
        except _EventRejected as exc:
            return self._error(req, path + ["decision:rejected"], exc.status,
                               str(exc))
        return response

    def _parse_expected_revision(self, value: Any) -> int | None:
        """Strict non-negative integer; bool/float/negative/other are invalid."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.isascii() and value.isdigit():
            return int(value)
        return None

    def _event_public(self, event: dict) -> dict:
        """Allowlisted event metadata; never the raw payload/fact."""
        return {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            "occurred_at": event["occurred_at"],
            "object_id": event["object_id"],
            "authority_owner": event["authority_owner"],
            "processed_at": event["processed_at"],
            "has_outcome": bool(event["outcome"]),
        }

    def _record_known_fact(self, fingerprint: str, fact: dict, event_id: str) -> None:
        self._store.conn.execute(
            "INSERT OR IGNORE INTO known_facts(fingerprint, fact, first_event_id, first_seen_at)"
            " VALUES (?,?,?,?)",
            (fingerprint, canonical_json(fact), event_id, utcnow()),
        )

    def _apply_decision(self, event: dict, object_id: str, aspect: str,
                        payload: dict, scope: str) -> dict:
        current = self._store.get_aspect(object_id, aspect)
        new_revision = (current["revision"] + 1) if current else 1
        merged = dict(_j(current["payload"])) if current else {}
        merged.update(payload.get("changes", {}))
        merged["last_decision_event_id"] = event["event_id"]
        merged["decided_at"] = event["occurred_at"]
        if current:
            self._store.conn.execute(
                "UPDATE aspects SET is_current = 0, valid_to = ? WHERE version_id = ?",
                (event["occurred_at"], current["version_id"]),
            )
        self._store.add_aspect(
            version_id=f"{object_id}:{aspect}:r{new_revision}",
            object_id=object_id,
            name=aspect,
            payload=merged,
            schema_version=current["schema_version"] if current else "1",
            lifecycle="CURRENT",
            verification="VERIFIED",
            revision=new_revision,
            is_current=True,
            valid_from=event["occurred_at"],
            valid_to=None,
            source_ref_id=event["source_ref_id"] or (current["source_ref_id"] if current else None),
        )
        return {"new_revision": new_revision, "value": merged}

    def _mark_event(self, event_id: str, outcome: dict) -> None:
        self._store.conn.execute(
            "UPDATE events SET processed_at = ?, outcome = ? WHERE event_id = ?",
            (utcnow(), canonical_json(outcome), event_id),
        )

    # ----------------------------------------------------- 7. projection guard
    def refresh_projection(
        self,
        source_system: str,
        native_id: str,
        expected_revision: str,
        projection_id: str | None = None,
        caller: str | None = None,
    ) -> dict:
        req = {"operation": "refresh_projection", "source_system": source_system,
               "native_id": native_id, "expected_revision": expected_revision,
               "caller": caller}
        if caller not in self.TRUSTED_INGEST_CALLERS:
            return self._error(req, ["authority:check"], Status.AUTHORITY_DENIED,
                               "only a trusted ingestion principal may rebuild projections")
        if not expected_revision:
            return self._error(req, ["version:check"], Status.VERSION_CONFLICT,
                               "expected_revision is required")
        path = ["source:head"]
        with self._store.tx():
            head = self._store.get_source_head(source_system, native_id)
            if head is None:
                return self._envelope(
                    req, Status.NOT_CHECKED, path + ["source:no_head"],
                    error={"code": Status.NOT_CHECKED,
                           "message": "no observed source head"},
                )
            path.append("version:compare")
            if head["latest_revision"] != expected_revision:
                return self._envelope(
                    req, Status.VERSION_CONFLICT, path + ["version:mismatch"],
                    error={
                        "code": Status.VERSION_CONFLICT,
                        "message": "projection rebuild refused: expected revision is not the"
                        " observed source head",
                        "detail": {"expected": expected_revision,
                                   "observed_head": head["latest_revision"]},
                    },
                )
            pid = projection_id or f"{source_system}:{native_id}"
            self._store.mark_projection(pid, expected_revision, "FRESH")
        return self._envelope(req, Status.OK, path + ["projection:rebuilt"],
                              result={"projection_id": pid,
                                      "built_revision": expected_revision})
