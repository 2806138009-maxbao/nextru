"""Identity card: what a document is and where it sits, attached to every document read.

Relations are not only for reasoning; they tell an agent what it is looking at before it reads the
text. The card gathers, from structure Nexus already holds, a few short facts:

- what:    kind (title tag / declared object type), self-declared currency, or "Nexus-native"
- where:   folder path (Drive structure) or the collection/entity a native record belongs to
- domain:  the Pointer Manifest domain it falls under, that domain's index, current owner and rule
- lineage: what it supersedes / is superseded by
- links:   documents it links to and documents that cite it (counts + a few names)
- about:   what it is declared to be about / based on

Everything is a pointer to existing structure; the card adds no claim of its own. Set
NEXUS_IDENTITY_CARD=0 to switch it off (used for ablation runs).
"""
from __future__ import annotations

import json
import os

from .resolve import Domains, STRATEGY_MEANING, _edges, _name, _status

MAX_NAMES = 4


def enabled() -> bool:
    return os.environ.get("NEXUS_IDENTITY_CARD", "1") != "0"


def _short(name: str | None, n: int = 60) -> str | None:
    if not name:
        return name
    return name if len(name) <= n else name[:n] + "…"


def _path(conn, scope: str, oid: str) -> list[str]:
    out, seen, cur = [], {oid}, oid
    while True:
        ups = _edges(conn, scope, cur, "PART_OF") or _edges(conn, scope, cur, "BELONGS_TO")
        if not ups or ups[0] in seen:
            break
        cur = ups[0]
        seen.add(cur)
        out.append(_name(conn, cur) or cur)
    return list(reversed(out))


def _names(conn, ids: list[str]) -> dict:
    ids = list(dict.fromkeys(ids))
    return {"count": len(ids), "examples": [{"object_id": i, "name": _short(_name(conn, i))} for i in ids[:MAX_NAMES]]}


def identity_card(conn, scope: str, oid: str, domains: Domains | None = None) -> dict | None:
    if not enabled():
        return None
    row = conn.execute("SELECT type, display_name FROM objects WHERE object_id = ? AND scope = ?", (oid, scope)).fetchone()
    if row is None:
        return None
    otype = row[0]
    card: dict = {}
    st = _status(conn, oid)
    if otype in ("native_document", "nexus_native_document"):
        card["what"] = "Nexus-native document (Nexus is its source of truth; versioned)"
    else:
        kinds = list(dict.fromkeys(([st["object_type"].upper()] if st.get("object_type") else [])
                                   + [t for t in (st.get("title_tags") or []) if not any(ch.isdigit() for ch in t)]))[:2]
        what = " / ".join(kinds) or otype
        cur = st.get("currency")
        card["what"] = what + (f" | declares itself {cur}" if cur and cur != "UNDECLARED" else
                               " | no status declared")
        decl = st.get("declared") or []
        status = next((d for d in decl if d["field"] in ("status", "状态", "mission_state", "plan_state")), None)
        if status:
            card["declared"] = _short(f"{status['field']}: {status['text']}", 120)
    path = _path(conn, scope, oid)
    if path:
        card["where"] = " › ".join(_short(p, 40) for p in path)
    doms = domains or Domains(conn, scope)
    dom, _basis = doms.of_object(oid)
    if dom:
        owners = _edges(conn, scope, dom["domain"], "OWNS_CURRENT", "in")
        card["domain"] = {"key": dom["key"], "index": _short(_name(conn, dom["index"])),
                          "current_owner": _short(_name(conn, owners[0])) if owners else "not in Nexus (NOT_CHECKED)",
                          "rule": STRATEGY_MEANING.get(dom["strategy"] or "")}
    lineage = {}
    by = _edges(conn, scope, oid, "SUPERSEDES", "in")
    if by:
        lineage["superseded_by"] = [{"object_id": b, "name": _short(_name(conn, b))} for b in by]
    sup = _edges(conn, scope, oid, "SUPERSEDES")
    if sup:
        lineage["supersedes"] = [{"object_id": b, "name": _short(_name(conn, b))} for b in sup]
    if lineage:
        card["lineage"] = lineage
    out_links = _edges(conn, scope, oid, "LINKS_TO")
    in_links = _edges(conn, scope, oid, "LINKS_TO", "in")
    if out_links:
        card["links_to"] = _names(conn, out_links)
    if in_links:
        card["cited_by"] = _names(conn, in_links)
    about = []
    for t in ("about", "ABOUT", "BASED_ON", "DEPENDS_ON", "GOVERNED_BY"):
        about += [(t, x) for x in _edges(conn, scope, oid, t)]
    if about:
        card["about"] = [{"type": t, "object_id": x, "name": _short(_name(conn, x))} for t, x in about[:MAX_NAMES]]
    card["hint"] = "nexus_resolve(object_id) for the full owner / freshness / conflict resolution"
    return card
