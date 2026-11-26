"""Places: the one answer to "where does this object belong", for browsing (web net), people and agents.

Every object gets at most one parent place, and through it a domain. The tree is the Drive-style
hierarchy Nexus already declares, read in this order:

1. a domain entity sits under the domain it BELONGS_TO (subdomain -> domain); top-level domains are roots;
2. a domain's own folder (the folder its index sits in) IS the domain - it is folded into it
   (`same_as`), so "EXU" the domain and "EXU" the Drive folder are one place;
3. anything that declares BELONGS_TO a domain sits directly under that domain (Pointer Manifest);
4. otherwise its PART_OF parent (Drive folder, native collection, course register ...), VERIFIED first;
   a parent that is a domain folder is read as the domain itself;
5. a course register entity (reg/...) sits where its register document sits (document -REGISTER_OF-> course);
6. an attachment sits under what it is attached to, by a declared structural relation: evidence under the
   object that is EVIDENCED_BY it, a learner receipt under the knowledge it is EVIDENCE_FOR, a mission's
   acceptance / decision / path under the mission (HAS_ACCEPTANCE / HAS_DECISION / HAS_PATH);
7. nothing else: the object has no place yet (parent None). It is never homed by guess - same rule as
   resolve.py (Manifest acceptance test E). Consumers show these as "not filed yet".

Cross-domain: a relation whose two ends resolve to different domains (`domain_of`) crosses domains.
"""
from __future__ import annotations

import re

from .resolve import Domains, _edges

_ATTACH_IN = ("EVIDENCED_BY", "HAS_ACCEPTANCE", "HAS_DECISION", "HAS_PATH")
_ATTACH_OUT = ("EVIDENCE_FOR",)
_INDEX_DIR = re.compile(r"^\s*00(?:\b|_)|START HERE", re.I)


def _domain_folder(conn, scope: str, d: dict) -> str | None:
    """The folder a domain's index sits in; an index kept in a 00 / 00_ / START HERE sub-folder means its parent."""
    idx = d.get("index")
    if not idx:
        return None
    folder = (_edges(conn, scope, idx, "PART_OF") or [None])[0]
    name = conn.execute("SELECT display_name FROM objects WHERE object_id = ?", (folder,)).fetchone() if folder else None
    if folder and name and _INDEX_DIR.search(name[0] or ""):
        folder = (_edges(conn, scope, folder, "PART_OF") or [folder])[0]
    return folder


def places(conn, scope: str) -> dict:
    """{"parent": {oid: parent or None}, "how": {oid: rule}, "same_as": {folder: domain},
    "domain": {oid: domain or None}, "domains": [{id, key, parent}]}"""
    doms = Domains(conn, scope)
    dom_ids = set(doms.by_id)
    same_as = {}
    for oid, d in doms.by_id.items():
        f = _domain_folder(conn, scope, d)
        if f:
            same_as[f] = oid
    objs = [r[0] for r in conn.execute("SELECT object_id FROM objects WHERE scope = ?", (scope,))]
    # all live relations once, grouped (one scan instead of one query per object)
    part, belongs, register_of, attach = {}, {}, {}, {}
    for a, b, t, v in conn.execute(
            "SELECT from_id, to_id, relation_type, verification FROM relations WHERE scope = ? AND expired_at IS NULL"
            " AND verification != 'DISPUTED' AND relation_type IN ('PART_OF', 'part_of', 'BELONGS_TO', 'REGISTER_OF'"
            + "".join(f", '{x}'" for x in _ATTACH_IN + _ATTACH_OUT) + ")", (scope,)):
        if a == b:
            continue
        if t in _ATTACH_IN:                      # owner -HAS_X / EVIDENCED_BY-> attachment
            attach.setdefault(b, []).append(a)
        elif t in _ATTACH_OUT:                   # attachment -EVIDENCE_FOR-> owner
            attach.setdefault(a, []).append(b)
        elif t in ("PART_OF", "part_of"):
            part.setdefault(a, []).append((v != "VERIFIED", b))
        elif t == "BELONGS_TO":
            belongs.setdefault(a, []).append(b)
        else:
            register_of.setdefault(b, []).append(a)
    lift = lambda p: same_as.get(p, p)   # a domain folder stands for its domain
    parent, how = {}, {}
    for o in objs:
        if o in dom_ids:
            up = next((b for b in belongs.get(o, []) if b in dom_ids), None)
            parent[o], how[o] = up, ("subdomain" if up else "domain")
        elif o in same_as:
            parent[o], how[o] = same_as[o], "domain_folder"
        elif any(b in dom_ids for b in belongs.get(o, [])):
            parent[o], how[o] = next(b for b in belongs[o] if b in dom_ids), "declared"
        elif o in part:
            parent[o], how[o] = lift(sorted(part[o])[0][1]), "part_of"
        elif o in register_of:
            docs = register_of[o]
            up = next((lift(sorted(part[d])[0][1]) for d in docs if d in part), None)
            parent[o], how[o] = up, ("register" if up else "unfiled")
        elif o in attach:
            parent[o], how[o] = lift(sorted(attach[o])[0]), "attached"
        else:
            parent[o], how[o] = None, "unfiled"
    # break cycles (a PART_OF loop would make an object its own ancestor): cut the edge that closes it
    for o in objs:
        seen, cur = set(), o
        while cur is not None and cur not in seen:
            seen.add(cur)
            nxt = parent.get(cur)
            if nxt in seen:
                parent[cur], how[cur] = None, "cycle"
                break
            cur = nxt
    domain = {}
    for o in objs:
        cur, steps = o, 0
        while cur is not None and cur not in dom_ids and steps < 200:
            cur, steps = parent.get(cur), steps + 1
        domain[o] = cur if cur in dom_ids else None
    return {"parent": parent, "how": how, "same_as": same_as, "domain": domain,
            "domains": [{"id": k, "key": d["key"], "parent": parent.get(k)} for k, d in doms.by_id.items()]}


def path(pl: dict, oid: str, name=lambda x: x) -> list[str]:
    """Place path from the domain down to (not including) the object."""
    out, cur, seen = [], pl["parent"].get(oid), {oid}
    while cur is not None and cur not in seen:
        seen.add(cur)
        out.append(name(cur))
        cur = pl["parent"].get(cur)
    return list(reversed(out))
