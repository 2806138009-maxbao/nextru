"""How much owner attention an agent's relation or entity needs.

Nothing waits for the owner: every agent write takes effect at once and can be undone. The tier
only says how far the edge may be leaned on - decided from the relation type, the endpoints and the
evidence, not the type alone (the same type can be harmless filing in one place and authority in
another):

- AUTO   - plain filing (PART_OF / about / RECORDED_IN / LISTED_IN) whose evidence sits in one of the
           two endpoints and where neither endpoint carries decisions, obligations or authority.
           Stored VERIFIED, decided by ``policy:auto``; the agent stays the recorded author.
- QUIET  - an ordinary candidate: UNVERIFIED, a usable lead, never presented as confirmed.
- GATE   - an agent's interpretation that could change a decision, an obligation or who holds
           authority. UNVERIFIED and marked check_source: before acting on it (sending something,
           changing a plan, telling the user it is settled) an agent reads the quoted source
           paragraph and relies on that, not on the edge. The owner is not asked at write time
           (owner rule 2026-11-10: check at use, not at write); they object when one is wrong.

Unknown relation types default to QUIET, and to GATE when they touch an authority-bearing entity.
"""
from __future__ import annotations

AUTO = "AUTO"
QUIET = "QUIET"
GATE = "GATE"
POLICY_ACTOR = "policy:auto"

# LINKS_TO = the source text contains a hyperlink to the other document (a literal fact)
FILING_TYPES = {"PART_OF", "BELONGS_TO", "ABOUT", "RECORDED_IN", "LISTED_IN", "LINKS_TO"}
# source refs that record Drive folder metadata (the folder a file sits in, a folder's parent)
FOLDER_REF = "src/google-drive-folder/"
STRUCTURAL_TYPES = {"folder", "document"}
# descriptive: says where something is written, who said or received it, what it rests on
DESCRIPTIVE_TYPES = FILING_TYPES | {"BASED_ON", "EVIDENCED_BY", "STATED_BY", "ADDRESSED_TO", "MENTIONS",
                                    "TRACKS", "USES", "SIMILAR_TO", "SEE_ALSO", "ORIGINATED_FROM"}
# can change what has been decided, what is owed, or whose word counts
GATE_TYPES = {"HAS_DECISION", "DECIDES", "ASSESSMENT_OWNER", "OWNED_BY", "AUTHORITY", "APPROVED_BY",
              "SUPERSEDED_BY", "SUPERSEDES", "CONFLICTS_WITH", "CONSTRAINS", "GOVERNED_BY", "REQUIRES",
              "OBLIGATES", "BLOCKS", "DEPENDS_ON", "COST_OF", "RESOLVES", "TIME_PRESSURE_ON", "ASSUMES",
              "HAS_ACCEPTANCE", "INVALIDATES", "OVERRIDES"}
# entity types that carry decisions, rules, obligations or execution authority
AUTHORITY_TYPES = {"decision", "principle", "requirement", "obligation", "mission", "acceptance",
                   "authority", "policy", "rule"}


def _type_of(conn, oid: str) -> str:
    if oid.startswith("ent/"):
        return oid.split("/")[1]
    r = conn.execute("SELECT type FROM objects WHERE object_id = ?", (oid,)).fetchone()
    return (r[0] if r else "") or ""


def _native_key(oid: str) -> str | None:
    """The part of an id that appears in its evidence refs (ndoc/x -> x, doc/<prefix> -> prefix)."""
    if oid.startswith("ndoc/") or oid.startswith("doc/"):
        return oid.split("/", 1)[1]
    return None


def classify_relation(conn, from_id: str, relation_type: str, to_id: str, evidence_refs: list[str]) -> dict:
    t = (relation_type or "").strip().upper()
    types = {_type_of(conn, from_id), _type_of(conn, to_id)}
    authority = sorted(types & AUTHORITY_TYPES)
    reasons: list[str] = []
    if t in GATE_TYPES:
        return {"tier": GATE, "reasons": [f"{relation_type} can change a decision, obligation or authority"]}
    if t in FILING_TYPES:
        keys = [k for k in (_native_key(from_id), _native_key(to_id)) if k]
        in_endpoint = any(k in ref for k in keys for ref in evidence_refs)
        if types <= STRUCTURAL_TYPES and any(ref.startswith(FOLDER_REF) for ref in evidence_refs):
            in_endpoint = True        # Drive's own folder metadata is the evidence
        if authority:
            reasons.append(f"endpoint is a {'/'.join(authority)}")
        if not in_endpoint:
            reasons.append("evidence is not in either endpoint")
        if not reasons:
            return {"tier": AUTO, "reasons": ["plain filing, evidence in the endpoint itself"]}
        return {"tier": QUIET, "reasons": reasons}
    if t in DESCRIPTIVE_TYPES:
        return {"tier": QUIET, "reasons": ["descriptive link"]}
    if authority:
        return {"tier": GATE, "reasons": [f"{relation_type} touches a {'/'.join(authority)}"]}
    return {"tier": QUIET, "reasons": ["ordinary candidate"]}


def classify_object(type_: str, evidence_refs: list[str] | None = None) -> dict:
    if type_ == "folder" and any(ref.startswith(FOLDER_REF) for ref in (evidence_refs or [])):
        return {"tier": AUTO, "reasons": ["mirrors a Drive folder, evidence is its metadata"]}
    if type_ in AUTHORITY_TYPES:
        return {"tier": GATE, "reasons": [f"a {type_} summarised by an agent can be mistaken for the owner's"]}
    return {"tier": QUIET, "reasons": ["ordinary entity"]}
