"""Import the Repository Pointer Manifest's machine block (Drive, FROZEN contract) as structure.

    python -m nexus.ingest.manifest --db <db> --scope <scope> [--doc doc/18XhBl1hYrw4L4cY] [--dry-run]

The Manifest (REP-CONTRACT-20260916-001) declares, in a JSON block between BEGIN_/END_REPOSITORY_
CONTRACT_JSON, the workspace's root pointers, its Domains / Shared Layers / subdomains with their
index_ref, current_owner_strategy and current_owner_ref, and a few route-critical objects. This
importer copies those declarations - nothing else - into Nexus:

- one ``domain`` entity per root object / subdomain (aliases: its key and ``domain:<KEY>``), whose
  description records kind, current_owner_strategy, manifest_state and any ref not in Nexus;
- edges, each quoting the Manifest paragraph that declares it:
    domain  -INDEX_REF->  index document
    owner document  -OWNS_CURRENT->  domain        (current_owner_ref)
    runtime owner   -RUNTIME_OWNER_REF->  domain
    domain  -GOVERNED_BY->  Global Semantic Spine  (authority_ref)
    subdomain  -BELONGS_TO->  parent domain
    Manifest  -<ROLE>->  document                  (root_entry_ref, semantic_standard_ref, ...)
    route-critical document  -<declared type>->  target  (BELONGS_TO / DEPENDS_ON / GOVERNED_BY ...)

The writer is ``import:pointer-manifest`` (not an agent): these are the contract's own declarations,
so they are stored VERIFIED with the Manifest as provenance - the same footing as the CORE REGISTER
import. Authority stays with the declaring documents (Source-Owned Authority); the entities are
pointers, not a second source of truth. Re-running is idempotent; refs whose document is not in
Nexus are reported as NOT_IN_NEXUS, never guessed.
"""
from __future__ import annotations

import argparse
import json
import sys

from ..db import connect, init_schema
from ..native import NativeDocs, NativeError, _norm_alias

ACTOR = "import:pointer-manifest"
MANIFEST_DOC = "doc/18XhBl1hYrw4L4cY"
TOP_REFS = ("root_entry_ref", "semantic_standard_ref", "methods_index_ref", "capability_registry_ref",
            "capability_hub_ref", "runtime_supervisor_ref")


def _paragraphs(conn, doc: str) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT statement, detail FROM observations WHERE object_id = ? AND"
                        " dimension = 'document_paragraph' ORDER BY observation_id", (doc,)).fetchall()
    return [(json.loads(d or "{}").get("anchor"), s or "") for s, d in rows]


def _block(paras: list[tuple[str, str]]) -> tuple[dict, int]:
    texts = [t for _, t in paras]
    start = next(i for i, t in enumerate(texts) if "BEGIN_REPOSITORY_CONTRACT_JSON" in t)
    end = next(i for i, t in enumerate(texts) if "END_REPOSITORY_CONTRACT_JSON" in t)
    return json.loads("\n".join(texts[start + 1:end])), start


class _Importer:
    def __init__(self, conn, scope: str, doc: str, dry_run: bool):
        self.conn, self.scope, self.doc, self.dry = conn, scope, doc, dry_run
        self.n = NativeDocs(conn)
        self.paras = _paragraphs(conn, doc)
        self.data, self.start = _block(self.paras)
        self.stats = {"domains": 0, "edges": 0, "existing": 0, "not_in_nexus": [], "errors": []}

    def anchor(self, *needles: str, after: str | None = None) -> str | None:
        """First paragraph (after the one holding `after`) that contains every needle."""
        i = self.start
        if after:
            i = next((k for k in range(self.start, len(self.paras)) if after in self.paras[k][1]), i)
        for a, t in self.paras[i:]:
            if all(nd in t for nd in needles):
                return a
        return None

    def obj(self, drive_id: str | None) -> str | None:
        if not drive_id:
            return None
        oid = "doc/" + drive_id[:16]
        r = self.conn.execute("SELECT 1 FROM objects WHERE object_id = ? AND scope = ?", (oid, self.scope)).fetchone()
        return oid if r else None

    def entity(self, key: str, spec: dict, kind: str) -> str | None:
        hit = self.conn.execute("SELECT object_id FROM entity_aliases WHERE alias_norm = ? AND scope = ?",
                                (_norm_alias(f"domain:{key}"), self.scope)).fetchone()
        if hit:
            return hit[0]
        missing = [f"{f}={(spec.get(f) or {}).get('id')}" for f in ("index_ref", "current_owner_ref", "runtime_owner_ref")
                   if spec.get(f) and not self.obj(spec[f].get("id"))]
        desc = (f"Pointer Manifest v0.1 声明：kind={kind}；current_owner_strategy={spec.get('current_owner_strategy')}；"
                f"manifest_state={spec.get('manifest_state')}"
                + (f"；parent_domain={spec['parent_domain']}" if spec.get("parent_domain") else "")
                + (f"；不在 Nexus 的指针（NOT_IN_NEXUS）：{', '.join(missing)}" if missing else "")
                + "。这是路由指针，不是业务事实的来源；事实以 current_owner 指向的文档为准。")
        if self.dry:
            self.stats["domains"] += 1
            return f"ent/domain/<{key}>"
        o = self.n.create_object(self.scope, "domain", f"{key}｜{kind}", ACTOR,
                                 aliases=[key, f"domain:{key}"], description=desc,
                                 evidence=[{"object_id": self.doc, "anchor": self.anchor(f'"{key}"')}],
                                 note="Pointer Manifest import")
        self.stats["domains"] += 1
        return o["object_id"]

    def edge(self, a: str | None, t: str, b: str | None, anchor: str | None, label: str) -> None:
        if not a or not b:
            self.stats["not_in_nexus"].append(label)
            return
        if self.conn.execute("SELECT 1 FROM relations WHERE from_id=? AND relation_type=? AND to_id=?"
                             " AND expired_at IS NULL", (a, t, b)).fetchone():
            self.stats["existing"] += 1
            return
        if self.dry or a.startswith("ent/domain/<") or b.startswith("ent/domain/<"):
            self.stats["edges"] += 1
            return
        try:
            self.n.add_relation(self.scope, a, t, b, ACTOR,
                                evidence=[{"object_id": self.doc, "anchor": anchor}] if anchor else None,
                                note=f"Pointer Manifest: {label}")
            self.stats["edges"] += 1
        except NativeError as e:
            self.stats["errors"].append([label, e.code, e.message[:80]])

    def run(self) -> dict:
        d = self.data
        for f in TOP_REFS:
            ref = (d.get(f) or {}).get("id")
            self.edge(self.doc, f.upper(), self.obj(ref), self.anchor(ref, after=f'"{f}"') if ref else None,
                      f"{f}={ref}")
        spine = self.obj((d.get("semantic_standard_ref") or {}).get("id"))
        keys: dict[str, str] = {}
        groups = [(k, v, v.get("kind", "DOMAIN")) for k, v in d.get("root_objects", {}).items()]
        groups += [(k, v, "SUBDOMAIN") for k, v in d.get("subdomain_registry", {}).items()]
        for key, spec, kind in groups:
            ent = self.entity(key, spec, kind)
            keys[key] = ent
            for field, rtype, forward in (("index_ref", "INDEX_REF", True),
                                          ("current_owner_ref", "OWNS_CURRENT", False),
                                          ("runtime_owner_ref", "RUNTIME_OWNER_REF", False)):
                ref = (spec.get(field) or {}).get("id")
                if not ref:
                    continue
                doc = self.obj(ref)
                anc = self.anchor(f'"{field}"', ref, after=f'"{key}"')
                if forward:
                    self.edge(ent, rtype, doc, anc, f"{key}.{field}={ref}")
                else:
                    self.edge(doc, rtype, ent, anc, f"{key}.{field}={ref}")
            if spec.get("authority_ref") == "semantic_standard_ref":
                self.edge(ent, "GOVERNED_BY", spine, self.anchor('"authority_ref"', after=f'"{key}"'), f"{key}.authority_ref")
        for key, spec, kind in groups:
            if spec.get("parent_domain"):
                self.edge(keys[key], "BELONGS_TO", keys.get(spec["parent_domain"]),
                          self.anchor('"parent_domain"', after=f'"{key}"'), f"{key}.parent_domain")
        targets = dict(keys)
        targets[d.get("contract_id", "")] = self.doc
        targets["GLOBAL_SEMANTIC_SPINE"] = spine
        for key, spec in d.get("route_critical_objects", {}).items():
            doc = self.obj((spec.get("canonical_ref") or {}).get("id"))
            for rel in spec.get("relationships", []):
                self.edge(doc, rel["type"], targets.get(rel["target"]),
                          self.anchor(f'"{rel["target"]}"', after=f'"{key}"'), f"{key} {rel['type']} {rel['target']}")
        return self.stats


def import_manifest(conn, scope: str, doc: str = MANIFEST_DOC, dry_run: bool = False) -> dict:
    return _Importer(conn, scope, doc, dry_run).run()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.ingest.manifest")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--doc", default=MANIFEST_DOC)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    conn = connect(a.db)
    init_schema(conn)
    out = import_manifest(conn, a.scope, a.doc, a.dry_run)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    return 0 if not out["errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
