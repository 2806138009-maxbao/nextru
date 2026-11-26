"""Read-only Research Evidence access for non-Current research material.

Why this module exists
----------------------
`get_context_pack` CONCISE deliberately selects only *Current* aspects.  Machine
transcription (ASR) is stored as ``DRAFT / UNVERIFIED / is_current=0``, so a
CONCISE pack correctly reports ``UNKNOWN`` with 0 facts for it.  That boundary
must not be relaxed: a candidate must never leak into the formal context.

An Agent that explicitly studies raw research material still needs a bounded,
readable view of it.  ``get_research_evidence`` is that view:

* it reads only **non-Current** aspects (DRAFT / HISTORICAL / SUPERSEDED);
* it can never promote one to Fact / Current / VERIFIED (no write path exists);
* it returns segment text, timecodes, stance and a stable evidence identity;
* its default response withholds the raw source body, internal filesystem paths,
  model/runtime metadata and unrelated objects.

Design notes
------------
No schema change and no new source access: everything is read through the same
projection the Core already serves.  The response is a *delivery format* over
existing rows, exactly like CONCISE/DETAILED, so it cannot invent facts the
store does not hold.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .errors import Status
from .modes import _PackError, _canon, _resolve_target, _worst_status
from .redaction import scrub_local_paths

CONTRACT_VERSION = "nexus-core-p1/0.1"
RESEARCH_MODE = "RESEARCH_EVIDENCE"
DEFAULT_MAX_CHARS = 12000

# Lifecycles that are research material rather than established facts.  A row
# marked CURRENT is never served here; it belongs to the formal context path.
RESEARCH_LIFECYCLES = frozenset({"DRAFT", "HISTORICAL", "SUPERSEDED"})

# Segment fields copied out of an observation's free-form `detail` JSON.  This
# is an allowlist: an importer can store anything in `detail`, and only these
# fields ever reach an Agent.
SEGMENT_DETAIL_FIELDS = (
    "segment_id",
    "clip_local_start_s",
    "clip_local_end_s",
    "source_start_s",
    "source_end_s",
    "text_review_status",
    "confidence_exp_logprob",
)

# Metadata copied out of the aspect payload.  Explicitly excludes `audio`,
# `model`, `runtime`, `decode_params`, `gpu` and the raw `segments` bodies.
PAYLOAD_META_FIELDS = (
    "source_file_id",
    "source_file_sha256",
    "source_revision",
    "source_video_offset_s",
    "coverage",
    "transcript_status",
)
PAYLOAD_DATA_FIELDS = (
    "schema_version",
    "segment_count",
    "detected_language",
    "language_probability",
)

_ANCHOR_TIME = re.compile(r"source_s:([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?)")


def _j(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _aspect_authority(core, object_id: str, name: str, scope: str) -> dict | None:
    binding = core._store.get_binding(object_id, name, scope)
    if not binding:
        return None
    summary = core._authority_summary(binding)
    if not summary:
        return None
    return {
        "binding_id": summary.get("binding_id"),
        "owner": summary.get("owner"),
        "write_policy": summary.get("write_policy"),
        "migration_state": summary.get("migration_state"),
        "source_of_truth": summary.get("source_of_truth"),
        "authority_level": summary.get("authority_level"),
    }


def _reading_access(ref: dict | None, scope: str) -> tuple[dict, str | None]:
    """Access decision for one research reading; never serve a mismatched scope."""
    if ref is None:
        return ({"status": "MISSING", "access_scope": None,
                 "reason": "research aspect has no readable SourceRef"}, "UNKNOWN")
    access = {
        "scope": scope,
        "access_scope": ref["access_scope"],
        "status": ref["status"],
    }
    if ref["access_scope"] != scope:
        access["reason"] = "SourceRef belongs to another scope; no content served"
        return access, Status.INVALID_SCOPE
    if ref["status"] in {"BLOCKED", "MISSING"}:
        access["reason"] = "SourceRef is not readable; cached content is not served"
        return access, ref["status"]
    return access, None


def _content_identity(ref: dict | None, sections: list[dict]) -> dict:
    """Separate content SHA-256 from the Drive native revision.

    ``source_refs.content_sha256`` is the hash of the *source document bytes*.
    It is not a Drive revision ID and must never be used to decide freshness.
    When the full body is stored, the hash is re-computed from that body so the
    response proves the round trip instead of trusting a column.
    """
    declared = ref["content_sha256"] if ref else None
    verified_from = None
    byte_length = None
    for section in sections:
        body = section.get("body") or ""
        digest = _sha256_text(body)
        if declared and digest == declared:
            verified_from = section.get("anchor")
            byte_length = len(body.encode("utf-8"))
            break
    return {
        "content_sha256": declared,
        "sha256_scope": "SOURCE_DOCUMENT_BYTES",
        "reverified_from_section": verified_from,
        "byte_length": byte_length,
        "note": "内容 SHA-256 标识源文件字节；它不是 Drive native revision",
    }


def _native_revision(core, ref: dict | None) -> dict:
    if ref is None:
        return {"value": None, "status": "UNCHECKED", "source_head": None,
                "note": "no SourceRef"}
    head = core._store.get_source_head(ref["source_system"], ref["native_id"])
    head_rev = head["latest_revision"] if head else None
    if ref["revision"]:
        status = "RECORDED"
    else:
        status = "NOT_CHECKED"
    return {
        "value": ref["revision"],
        "status": status,
        "source_head": head_rev,
        "freshness": core._freshness(ref)[0],
        "note": "Drive native revision；未记录时为 NOT_CHECKED，不能由内容 SHA-256 推导",
    }


def _segments(store, object_id: str, source_ref_id: str | None,
              sections_by_anchor: dict[str, dict]) -> tuple[list[dict], dict]:
    """One segment per observation that cites this research SourceRef."""
    rows = store.get_observations(object_id)
    segments: list[dict] = []
    section_matched = 0
    section_mismatched = 0
    for row in rows:
        ref_ids = _j(row["source_ref_ids"]) or []
        if source_ref_id and source_ref_id not in ref_ids:
            continue
        if not source_ref_id and ref_ids:
            continue
        detail = _j(row["detail"]) or {}
        anchor = detail.get("anchor")
        match = _ANCHOR_TIME.search(anchor or "")
        start = _as_float(detail.get("source_start_s"))
        end = _as_float(detail.get("source_end_s"))
        if start is None and match:
            start = float(match.group(1))
        if end is None and match:
            end = float(match.group(2))
        segment: dict[str, Any] = {
            "id": row["observation_id"],
            "anchor": anchor,
            "text": row["statement"],
            "stance": row["stance"],
            "source_start_s": start,
            "source_end_s": end,
        }
        for field in SEGMENT_DETAIL_FIELDS:
            if field in detail and field not in segment:
                segment[field] = detail[field]
        section = sections_by_anchor.get(anchor or "")
        if section is not None:
            stored = (section.get("body") or "").strip()
            if stored == (row["statement"] or "").strip():
                segment["text_matches_section"] = True
                section_matched += 1
            else:
                segment["text_matches_section"] = False
                section_mismatched += 1
        segments.append(segment)
    segments.sort(key=lambda s: (
        s.get("source_start_s") is None,
        s.get("source_start_s") if s.get("source_start_s") is not None else 0.0,
        s["id"],
    ))
    for index, segment in enumerate(segments, start=1):
        segment["index"] = index
    coverage = {
        "section_text_matched": section_matched,
        "section_text_mismatched": section_mismatched,
    }
    return segments, coverage


def _payload_meta(payload: Any) -> dict:
    """Allowlisted payload metadata; never the raw document or its paths."""
    if not isinstance(payload, dict):
        return {}
    meta: dict[str, Any] = {}
    for field in PAYLOAD_META_FIELDS:
        if field in payload:
            meta[field] = payload[field]
    data = payload.get("data")
    if isinstance(data, dict):
        inner = {field: data[field] for field in PAYLOAD_DATA_FIELDS if field in data}
        if inner:
            meta["data"] = inner
    return meta


def _reading(core, obj: dict, aspect: dict, scope: str) -> dict:
    store = core._store
    object_id = obj["object_id"]
    source_ref_id = aspect["source_ref_id"]
    ref = store.get_source_ref(source_ref_id) if source_ref_id else None
    sections = store.get_sections(source_ref_id) if source_ref_id else []
    sections_by_anchor = {s["anchor"]: s for s in sections}
    access, blocked = _reading_access(ref, scope)

    reading: dict[str, Any] = {
        "aspect": {
            "stable_id": f"aspect:{object_id}/{aspect['name']}",
            "name": aspect["name"],
            "version_id": aspect["version_id"],
            "revision": aspect["revision"],
            "lifecycle": aspect["lifecycle"],
            "verification": aspect["verification"],
            "is_current": bool(aspect["is_current"]),
            "schema_version": aspect["schema_version"],
            "updated_at": aspect["updated_at"],
            "authority": _aspect_authority(core, object_id, aspect["name"], scope),
            "promotion": "NONE",
        },
        "identity": {
            "source_ref_id": source_ref_id,
            "source_system": ref["source_system"] if ref else None,
            "native_id": ref["native_id"] if ref else None,
            "native_revision": _native_revision(core, ref),
            "content": _content_identity(ref, sections),
            "anchor": ref["anchor"] if ref else None,
        },
        "access": access,
        "segments": [],
        "coverage": {
            "segment_count": 0,
            "segments_returned": 0,
            "complete": False,
            "sections_available": len(sections),
        },
    }

    if blocked:
        # A blocked/missing/foreign-scope source is not served from cache: only
        # the fact that a SourceRef exists is disclosed, not its native id, its
        # anchor or the content digest.
        reading["identity"] = {
            "source_ref_id": source_ref_id,
            "source_system": ref["source_system"] if ref else None,
            "withheld": ["native_id", "native_revision", "content", "anchor"],
            "reason": blocked,
        }
        authority = reading["aspect"].get("authority")
        if isinstance(authority, dict):
            authority.pop("source_of_truth", None)
        reading["status"] = blocked
        reading["coverage"]["complete"] = False
        reading["coverage"]["reason"] = blocked
        return reading

    segments, text_coverage = _segments(store, object_id, source_ref_id,
                                        sections_by_anchor)
    payload = _j(aspect["payload"])
    payload_meta = _payload_meta(payload)
    payload_count = None
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload_count = payload["data"].get("segment_count")
    elif isinstance(payload, dict):
        payload_count = payload.get("segment_count")
    payload_count = payload_count if isinstance(payload_count, int) else None

    starts = [s["source_start_s"] for s in segments if s.get("source_start_s") is not None]
    ends = [s["source_end_s"] for s in segments if s.get("source_end_s") is not None]
    coverage = {
        "segment_count": len(segments),
        "segments_returned": len(segments),
        "payload_segment_count": payload_count,
        "payload_segment_count_match": (
            payload_count is None or payload_count == len(segments)
        ),
        "first_source_start_s": min(starts) if starts else None,
        "last_source_end_s": max(ends) if ends else None,
        "sections_available": len(sections),
        **text_coverage,
    }
    coverage["complete"] = (
        bool(segments)
        and coverage["payload_segment_count_match"]
        and coverage["section_text_mismatched"] == 0
    )
    if not segments:
        coverage["reason"] = "NO_SEGMENT_OBSERVATIONS"
        reading["status"] = Status.UNKNOWN
    elif coverage["section_text_mismatched"]:
        coverage["reason"] = "SECTION_TEXT_MISMATCH"
        reading["status"] = Status.PARTIAL
    elif not coverage["payload_segment_count_match"]:
        coverage["reason"] = "SEGMENT_COUNT_MISMATCH"
        reading["status"] = Status.PARTIAL
    else:
        reading["status"] = Status.OK
    reading["segments"] = segments
    reading["coverage"] = coverage
    reading["payload_meta"] = payload_meta
    return reading


def _envelope(params: dict, status: str, result: Any, warnings: list[str],
              missing: list[Any] | None = None, error: dict | None = None,
              provider_io: dict | None = None) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "request": {"operation": "get_research_evidence", "params": params},
        "execution": {
            "status": status,
            "retrieval_path": ["scope:validate", "object:resolve",
                               "aspect:non_current", "evidence:research"],
            "provider_io": provider_io or {
                "drive_api_calls": 0,
                "source_documents_read": 0,
                "source_chars_returned": 0,
                "full_source_reads": 0,
            },
        },
        "result": result,
        "warnings": warnings,
        "missing": missing or [],
        "error": error,
    }


def _fail(params: dict, status: str, message: str, *, detail: dict | None = None,
          warnings: list[str] | None = None) -> dict:
    return _envelope(params, status, None, warnings or [],
                     error={"code": status, "message": message, "detail": detail})


def research_error(params: dict, status: str, message: str,
                   detail: dict | None = None) -> dict:
    """Public error envelope so parameter validation matches success shape."""
    return _fail(params, status, message, detail=detail)


def _finalize_chars(pack: dict) -> int:
    for _ in range(6):
        measured = len(_canon(pack))
        if pack["budget"]["chars"] == measured:
            return measured
        pack["budget"]["chars"] = measured
    return len(_canon(pack))


def _trim_to_budget(pack: dict, max_chars: int) -> None:
    """Drop segments from the end (last reading first) until the pack fits `max_chars`.

    Same result as popping one segment at a time and re-measuring, but the number to drop is
    found by binary search: re-serialising the whole pack after every pop took minutes on a
    document with thousands of paragraphs. Each dropped segment is replaced by its (shorter) id
    in `dropped_segments`, so the size falls as more are dropped.
    """
    readings = pack["readings"]
    original = [list(r.get("segments") or []) for r in readings]
    coverage = [dict(r["coverage"]) if isinstance(r.get("coverage"), dict) else None for r in readings]
    total = sum(len(s) for s in original)

    def apply(drop: int) -> int:
        left, dropped = drop, []
        for i in reversed(range(len(readings))):
            segs, reading = original[i], readings[i]
            take = min(left, len(segs))
            left -= take
            if "segments" in reading or segs:
                reading["segments"] = segs[:len(segs) - take]
            if coverage[i] is not None:
                reading["coverage"] = dict(coverage[i])
            if take:
                dropped.extend(s["id"] for s in reversed(segs[len(segs) - take:]))
                reading["coverage"]["segments_returned"] = len(reading["segments"])
                reading["coverage"]["complete"] = False
        pack["budget"]["dropped_segments"] = dropped
        return _finalize_chars(pack)

    if total == 0:
        return
    if apply(total) > max_chars:
        return                      # does not fit even with every segment dropped
    lo, hi = 1, total
    while lo < hi:
        mid = (lo + hi) // 2
        if apply(mid) <= max_chars:
            hi = mid
        else:
            lo = mid + 1
    apply(lo)


def build_research_evidence(core, object_id: str, scope: str,
                            max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """Read non-Current research evidence for one object, inside one scope."""
    params = {"object_id": object_id, "scope": scope, "max_chars": max_chars}
    if not object_id:
        return _fail(params, Status.INVALID_SCOPE, "object_id is required")

    try:
        obj = _resolve_target(core, "", scope, object_id)
    except _PackError as exc:
        reason = ("OBJECT_SCOPE_DENIED" if exc.status == Status.INVALID_SCOPE
                  else "OBJECT_NOT_FOUND")
        return _fail(params, exc.status, exc.message, detail={"reason": reason})

    store = core._store
    all_aspects = store.get_aspects(object_id, current_only=False)
    research = [a for a in all_aspects if a["lifecycle"] in RESEARCH_LIFECYCLES]
    if not research:
        return _fail(
            params, Status.NOT_FOUND,
            "object has no non-Current (research) aspect",
            detail={"reason": "NO_RESEARCH_EVIDENCE",
                    "checked_lifecycles": sorted(RESEARCH_LIFECYCLES),
                    "stored_aspects": [a["name"] for a in all_aspects]},
            warnings=["RESEARCH_EVIDENCE_ABSENT: nothing to read outside Current"],
        )

    research.sort(key=lambda a: (a["name"], a["revision"]))
    readings = [_reading(core, obj, aspect, scope) for aspect in research]
    statuses = [r.get("status", Status.PARTIAL) for r in readings]
    status = _worst_status(statuses)

    boundaries = [
        "机器转录未人工校对：lifecycle=DRAFT / verification=UNVERIFIED / stance=TO_TEST",
        "本读取不构成 Fact、不走 Current，也不会被提升为 VERIFIED",
        "内容 SHA-256 标识源文件字节，与 Drive native revision 是两种身份",
        "默认响应不含原始 JSON 正文、内部文件路径、模型/运行环境细节",
        "研究资料不进入 CONCISE 的 Current-only 上下文",
        "来源文件或 revision 未核验时，不得据此声称最新版本",
    ]

    pack: dict[str, Any] = {
        "mode": RESEARCH_MODE,
        "status": status,
        "object": {"id": obj["object_id"], "type": obj.get("type"),
                   "name": obj.get("display_name"), "scope": obj.get("scope")},
        "semantics": {
            "retrieval": RESEARCH_MODE,
            "is_fact": False,
            "is_current": False,
            "is_verified": False,
            "promotion": "FORBIDDEN",
            "note": "研究资料读取：不是 Fact，不是 Current，不是 VERIFIED",
        },
        "readings": readings,
        "evidence_boundary": boundaries,
        "redaction": {"applied": False, "redactions": 0, "fields": []},
        "budget": {"max_chars": max_chars, "chars": 0, "exceeded": False,
                   "truncated": False, "dropped_segments": []},
    }

    scrubbed, redactions, fields = scrub_local_paths({"readings": pack["readings"]})
    pack["readings"] = scrubbed["readings"]
    pack["redaction"] = {
        "applied": redactions > 0,
        "redactions": redactions,
        "fields": fields,
    }

    if _finalize_chars(pack) > max_chars:
        _trim_to_budget(pack, max_chars)
    if pack["budget"]["dropped_segments"]:
        pack["budget"]["truncated"] = True
        pack["status"] = _worst_status([pack["status"], Status.PARTIAL])
        _finalize_chars(pack)
    pack["budget"]["exceeded"] = pack["budget"]["chars"] > max_chars
    if pack["budget"]["exceeded"]:
        pack["status"] = _worst_status([pack["status"], Status.PARTIAL])
        _finalize_chars(pack)

    warnings: list[str] = []
    for reading in readings:
        native = reading["identity"].get("native_revision")
        if native is None:
            continue
        if native["status"] != "RECORDED":
            warnings.append(
                "NATIVE_REVISION_NOT_RECORDED: "
                f"{reading['aspect']['stable_id']} has no recorded native revision;"
                " content SHA-256 is not a revision")
            break
    if any(r["aspect"]["lifecycle"] == "DRAFT" for r in readings):
        warnings.append("RESEARCH_DRAFT_NOT_PROMOTED: DRAFT/UNVERIFIED content was"
                        " returned as research material only")

    provider_io = {
        "drive_api_calls": 0,
        "source_documents_read": 0,
        "source_chars_returned": 0,
        "full_source_reads": 0,
        "internal_core_reads": 2 + len(readings),
    }
    return _envelope(params, pack["status"], pack, warnings, provider_io=provider_io)
