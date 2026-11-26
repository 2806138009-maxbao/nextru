"""Transport-agnostic operation dispatch used by the local HTTP API.

The default interface is deliberately read-only. State-changing test operations
are exposed only through a separate, explicitly enabled test interface, with a
server-supplied principal (never a client-supplied authority string).

Two boundaries live here rather than in Core, because they are about *what a
transport client may see*, not about what the store may hold:

* raw source bodies (`trace_evidence(expand=...)`) are **not** part of the read
  operations; the pack layer still expands sections internally to fingerprint
  them;
* every response is redacted of internal filesystem paths before it leaves
  (`LOCAL_PATH_REDACTED`), so no client can enumerate the machine layout even if
  a stored payload embeds it.
"""

from __future__ import annotations

from typing import Any

from .core import NexusCore
from .errors import Status
from .modes import CONCISE, DETAILED, MODES, build_concise, build_detailed
from .redaction import scrub_local_paths
from .research import DEFAULT_MAX_CHARS, build_research_evidence, research_error

READ_OPERATIONS = {
    "resolve_object": ("query", "scope"),
    # Discovery helpers added for cold-start navigation (Task 007): identity
    # catalog and relation vocabulary, no payloads and no edge endpoints.
    "list_objects": ("scope", "object_type", "limit"),
    "list_relation_types": ("scope",),
    # Task 008: full-text discovery (ids, anchors, snippets; never full bodies).
    "search": ("query", "scope", "kinds", "limit"),
    "get_current_state": ("object_id", "aspect", "scope", "as_of", "mode"),
    "traverse_relations": ("object_id", "relation_types", "direction", "max_depth",
                           "scope", "include_candidates"),
    # `expand` is intentionally absent: the raw section body is an internal
    # read used by the context packs, never an Agent-facing tool parameter.
    "trace_evidence": ("item_id", "scope"),
    "get_context": ("intent", "object_id", "aspects", "scope", "depth", "max_chars"),
    "get_context_pack": ("question", "object_id", "scope", "mode", "item_ids", "max_chars"),
    # Read-only view of non-Current research material (DRAFT/UNVERIFIED).  It is
    # a separate operation precisely so the CONCISE Current-only rule stays
    # untouched: this path cannot feed a formal context pack.
    "get_research_evidence": ("object_id", "scope", "max_chars"),
}

TEST_OPERATIONS = {
    "evaluate_event": ("event_id", "scope"),
    "refresh_projection": ("source_system", "native_id", "expected_revision",
                           "projection_id"),
}

# Backwards-compatible name for callers that only need read operations.
OPERATIONS = READ_OPERATIONS


def _error(operation: str, params: dict, status: str, message: str,
           detail: dict | None = None) -> dict:
    return {
        "contract_version": "nexus-core-p1/0.1",
        "request": {"operation": operation, "params": params},
        "execution": {"status": status, "retrieval_path": [], "provider_io": {}},
        "error": {"code": status, "message": message, "detail": detail},
        "result": None, "authority": None, "state": None,
        "evidence": [], "missing": [], "conflicts": [], "warnings": [],
    }


def _call(core: NexusCore, operation: str, params: dict,
          allowed: tuple[str, ...]) -> dict:
    unknown = set(params) - set(allowed)
    if unknown:
        return _error(operation, params, Status.INVALID_SCOPE,
                      f"unknown parameter(s): {sorted(unknown)}",
                      {"allowed": sorted(allowed)})
    try:
        return getattr(core, operation)(**params)
    except TypeError as exc:
        return _error(operation, params, Status.INVALID_SCOPE,
                      f"invalid arguments for {operation}: {exc}",
                      {"allowed": sorted(allowed)})


def _context_pack(core: NexusCore, params: dict) -> dict:
    """Validate and dispatch the CONCISE/DETAILED pack operation."""
    allowed = READ_OPERATIONS["get_context_pack"]
    unknown = set(params) - set(allowed)
    if unknown:
        return _error("get_context_pack", params, Status.INVALID_SCOPE,
                      f"unknown parameter(s): {sorted(unknown)}",
                      {"allowed": sorted(allowed)})
    mode = str(params.get("mode") or CONCISE).upper()
    if mode not in MODES:
        return _error("get_context_pack", params, Status.INVALID_SCOPE,
                      f"unknown mode {mode!r}", {"allowed_modes": list(MODES)})
    scope = params.get("scope") or "global"
    object_id = params.get("object_id")
    # Never infer a scoped object's scope as the caller's authorisation.  A
    # caller that omits scope has only global access; scoped objects must be
    # requested with the scope it was granted.
    try:
        max_chars = int(params.get("max_chars") or (6000 if mode == CONCISE else 16000))
    except (TypeError, ValueError):
        return _error("get_context_pack", params, Status.INVALID_SCOPE,
                      "max_chars must be an integer")
    max_chars = max(500, min(40000, max_chars))

    if mode == CONCISE:
        question = str(params.get("question") or "").strip()
        if not question and not object_id:
            return _error("get_context_pack", params, Status.INVALID_SCOPE,
                          "question is required when object_id is omitted")
        return build_concise(core, question, scope, object_id, max_chars)

    item_ids = params.get("item_ids")
    if not object_id:
        return _error("get_context_pack", params, Status.INVALID_SCOPE,
                      "object_id is required for DETAILED mode")
    if not item_ids or not isinstance(item_ids, list) or not all(
            isinstance(x, str) and x for x in item_ids):
        return _error("get_context_pack", params, Status.INVALID_SCOPE,
                      "item_ids must be a non-empty list of fact ids",
                      {"examples": [f"aspect:{object_id}/<aspect>", "obs/<observation_id>"]})
    return build_detailed(core, object_id, scope, item_ids, max_chars)


def _research_evidence(core: NexusCore, params: dict) -> dict:
    """Validate and dispatch the read-only research evidence operation."""
    allowed = READ_OPERATIONS["get_research_evidence"]
    unknown = set(params) - set(allowed)
    if unknown:
        return research_error(params, Status.INVALID_SCOPE,
                              f"unknown parameter(s): {sorted(unknown)}",
                              {"allowed": sorted(allowed)})
    object_id = params.get("object_id")
    if not isinstance(object_id, str) or not object_id:
        return research_error(params, Status.INVALID_SCOPE,
                              "object_id is required")
    # Scope is authorisation: an omitted scope is `global`, never inferred from
    # the object.  A scoped research object must be requested with its scope.
    scope = params.get("scope") or "global"
    try:
        max_chars = int(params.get("max_chars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        return research_error(params, Status.INVALID_SCOPE,
                              "max_chars must be an integer")
    max_chars = max(500, min(40000, max_chars))
    return build_research_evidence(core, object_id, scope, max_chars)


def _dispatch(core: NexusCore, operation: str,
              params: dict[str, Any] | None = None) -> dict:
    params = params or {}
    if operation == "health":
        return {
            "contract_version": "nexus-core-p1/0.1",
            "status": "ok",
            "mode": "read-only-local",
            "operations": sorted(READ_OPERATIONS),
            "test_operations_available_only_on_isolated_interface": sorted(TEST_OPERATIONS),
        }
    if operation in TEST_OPERATIONS:
        return _error(
            operation, params, Status.AUTHORITY_DENIED,
            "state-changing test operation is not available on the read-only interface",
            {"isolated_interface": "/test/query"},
        )
    if operation not in READ_OPERATIONS:
        return _error(operation, params, Status.INVALID_SCOPE,
                      f"unknown operation {operation!r}",
                      {"known_operations": sorted(READ_OPERATIONS)})
    if operation == "get_context_pack":
        return _context_pack(core, params)
    if operation == "get_research_evidence":
        return _research_evidence(core, params)
    return _call(core, operation, params, READ_OPERATIONS[operation])


def _egress(response: dict) -> dict:
    """Redact internal filesystem paths from anything a transport client sees."""
    cleaned, count, _fields = scrub_local_paths(response)
    if count:
        warnings = cleaned.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
            cleaned["warnings"] = warnings
        warnings.append(
            f"LOCAL_PATH_REDACTED: {count} internal path string(s) were replaced"
            " before this response left Core")
    return cleaned


def dispatch(core: NexusCore, operation: str,
             params: dict[str, Any] | None = None) -> dict:
    return _egress(_dispatch(core, operation, params))


def dispatch_test(core: NexusCore, operation: str, params: dict[str, Any] | None,
                  principal: str) -> dict:
    params = dict(params or {})
    if operation not in TEST_OPERATIONS:
        return _error(operation, params, Status.INVALID_SCOPE,
                      f"operation {operation!r} is not a test-write operation",
                      {"test_operations": sorted(TEST_OPERATIONS)})
    if "caller" in params or "authority_owner" in params:
        return _error(operation, params, Status.AUTHORITY_DENIED,
                      "caller/authority_owner must not be supplied by the client")
    allowed = TEST_OPERATIONS[operation]
    params["caller"] = principal
    return _egress(_call(core, operation, params, allowed + ("caller",)))
