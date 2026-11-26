"""Concise / Detailed context delivery for Nexus read operations.

The mode boundary is deliberate: filtering happens *inside the tool*, before
anything reaches a model context.  CONCISE returns only the facts, relations,
authority, boundaries and evidence pointers needed for the question.  DETAILED
expands a previously returned fact or observation to its full payload,
SourceRefs, revision and section fingerprints.

No schema change and no new source access: both modes read the same projection
through the existing read operations.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import utcnow
from .errors import Status

CONCISE = "CONCISE"
DETAILED = "DETAILED"
MODES = (CONCISE, DETAILED)
CONTRACT_VERSION = "nexus-core-p1/0.1"

# Facet vocabulary.  These are question-domain hints, not per-question rules:
# the same table serves every mission/creator question.
FACET_HINTS: dict[str, dict[str, list[str]]] = {
    "MISSION": {
        "decision": ["决定", "回国", "行程", "12-08", "选择", "重启"],
        "mission_status": ["状态", "学校", "沟通", "active", "ec", "scenario", "mission"],
        "notice_state": ["通知", "邮件", "院系", "申请", "批准", "拒绝", "absence"],
        "recent_replies": ["回复", "wu", "selviah", "10-28", "答复"],
        "open_questions": ["未决", "评分", "brief", "rubric", "未知", "还没定"],
        "next_action": ["接下来", "下一步", "做什么", "行动", "怎么办"],
        "paths": ["路径", "b1", "b2", "捆绑", "分开"],
        "historical_delivery": ["历史", "10-18", "发送", "发件"],
        "anti_loop": ["重复", "催办", "风险", "unknown", "最坏"],
        "authority_model": ["authority", "权限", "谁决定", "owner", "正式来源"],
        "philosophy": ["理念", "原则", "验收", "真实贡献"],
    },
    "CREATOR": {
        "profile": ["职业", "招聘", "作品集", "起点", "经历", "公司", "工资", "求职", "机会"],
        "commercial": ["品牌", "广告", "商单", "赞助", "合作", "变现", "接单", "询单"],
        "economics": ["收入", "成本", "利润", "钱", "报价", "投流", "税", "万", "¥", "收益", "赚"],
        "equipment": ["设备", "相机", "拍摄", "灯光", "收音", "麦克风", "机械臂", "3d打印", "工作室"],
        "ai_workflow": ["ai", "codex", "动效", "剪辑", "工作流", "chatcut", "b-roll", "asr",
                        "字幕", "typeless", "自动化", "必火", "抓取"],
        "tradeoff": ["取舍", "代价", "值得", "参考", "建议", "风险", "全职", "坑", "效果",
                     "投流", "有效", "证明", "对照"],
        "evidence_boundary": ["证据", "来源", "核实", "可信", "url", "时间码", "后台",
                              "合同", "票据", "披露"],
        "operations": ["运营", "投流", "数据", "平台", "涨粉"],
        "six_dimensions": ["维度", "知识线"],
    },
    "CREATIVEPLAN": {
        "first_video": ["第一期", "第一条", "主题", "拍摄", "排期", "视频"],
        "creative_purpose": ["目的", "方向", "意图"],
        "style_quality": ["风格", "质量", "质感"],
        "research_candidates": ["研究", "候选", "参考"],
        "release_boundaries": ["发布", "边界", "限制", "公开"],
    },
}

CREATOR_OBSERVATION_DIMENSIONS = {
    "profile": {"career"},
    "commercial": {"commercial", "disclosure"},
    "economics": {"commercial", "cost"},
    "equipment": {"equipment"},
    "ai_workflow": {"ai_workflow"},
    "tradeoff": {"tradeoff", "operations"},
    "operations": {"operations"},
    "evidence_boundary": {"evidence_boundary"},
    "six_dimensions": {"operations"},
}

OBS_TO_ASPECT = {
    "career": "profile",
    "commercial": "commercial",
    "disclosure": "commercial",
    "cost": "economics",
    "equipment": "equipment",
    "ai_workflow": "ai_workflow",
    "tradeoff": "tradeoff",
    "operations": "tradeoff",
    "evidence_boundary": "evidence_boundary",
}


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clip(text: Any, limit: int = 320) -> Any:
    if not isinstance(text, str):
        return text
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_.\-]*", text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.add(run)
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
    return tokens


def _score(text: str, question_tokens: set[str], hint_terms: list[str]) -> float:
    lowered = (text or "").lower()
    overlap = len(_tokens(text) & question_tokens)
    hits = sum(1 for term in hint_terms if term.lower() in lowered)
    return overlap + 3.0 * hits


class _PackError(Exception):
    """Typed failure that must be returned as a Nexus status, not swallowed."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _normalise_kind(obj: dict, aspect_names: list[str] | None = None) -> str:
    """Map the stored object type to a routing kind.

    The store uses mixed spellings: ``Creator``/``Project`` in fixtures and
    ``CREATOR``/``MISSION`` in the G0 projection.  A Project carrying mission
    aspects is the DLC mission, not a generic project.
    """
    stored = str(obj.get("type") or "").strip().lower()
    names = set(aspect_names or [])
    if stored == "creator":
        return "CREATOR"
    if stored == "mission":
        return "MISSION"
    if stored == "project" and ({"decision", "status", "mission_status"} & names):
        return "MISSION"
    return stored.upper() if stored else "UNKNOWN"


def _resolve_target(core, question: str, scope: str, object_id: str | None) -> dict:
    if object_id:
        env = core.resolve_object(object_id, scope)
        status = env.get("execution", {}).get("status")
        result = env.get("result") or {}
        obj = result.get("object")
        if status not in {Status.OK, Status.PARTIAL} or not obj:
            detail = (env.get("error") or {}).get("message") or f"object not found in scope: {object_id}"
            raise _PackError(status or Status.NOT_FOUND, detail)
    else:
        env = core.resolve_object(question or "", scope)
        status = env.get("execution", {}).get("status")
        result = env.get("result") or {}
        if status not in {Status.OK, Status.PARTIAL}:
            detail = (env.get("error") or {}).get("message") or "object resolution failed"
            raise _PackError(status or Status.NOT_FOUND, detail)
        if result.get("resolution") == "RESOLVED" and result.get("object"):
            obj = result["object"]
        else:
            candidates = [c for c in (result.get("candidates") or [])
                          if c.get("scope") == scope]
            if not candidates:
                raise _PackError(Status.NOT_FOUND,
                                 "no object matched the question inside the checked scope")
            # Text relevance is a discovery signal only; the pack is marked
            # CANDIDATE so it cannot be mistaken for an Authority decision.
            obj = dict(candidates[0], _resolution="CANDIDATE")
    if scope == "global" and obj.get("scope") != "global":
        # Never turn a caller's global request into a scoped read merely
        # because the target object happens to carry that scope.
        raise _PackError(Status.INVALID_SCOPE,
                         f"object {obj.get('object_id')!r} lives in scope "
                         f"{obj.get('scope')!r}; global is not an authorisation")
    if scope != "global" and obj.get("scope") != scope:
        raise _PackError(Status.INVALID_SCOPE,
                         f"object {obj.get('object_id')!r} lives in scope "
                         f"{obj.get('scope')!r}, not {scope!r}")
    return obj


def _aspect_names(core, object_id: str) -> list[str]:
    return [a["name"] for a in core._store.get_aspects(object_id, current_only=True)]


def _ref_index(package: dict, evidence: list[dict]) -> dict[str, dict]:
    return {r["source_ref_id"]: r for r in evidence if r and r.get("source_ref_id")}


def _anchors(entry: dict, refs: dict[str, dict]) -> list[str]:
    out = []
    for rid in entry.get("source_ref_ids") or []:
        ref = refs.get(rid)
        if ref and ref.get("anchor"):
            out.append(ref["anchor"])
    return sorted(set(out))


def _entry_payload(entry: dict) -> tuple[dict, bool]:
    """Return the readable payload and whether it is a historical value."""
    payload = entry.get("value")
    if payload is not None:
        return payload, False
    historical = entry.get("historical_value")
    if historical is not None:
        return historical, True
    return {}, False


def _entry_authority(entry: dict, payload: dict) -> dict | None:
    """Authority resolved by Core is authoritative; payload text is a fallback."""
    authority = entry.get("authority")
    if isinstance(authority, dict) and authority.get("owner"):
        return authority
    owner = payload.get("authority_owner") if isinstance(payload, dict) else None
    if owner:
        return {"owner": owner, "binding_id": None, "source_of_truth": None,
                "write_policy": None, "scope": None}
    return None


def _source_entries(refs: list[dict]) -> list[dict]:
    """One pointer per (native_id, revision); never merge revisions."""
    sources: dict[tuple[str, str], dict] = {}
    for ref in refs:
        native = ref.get("native_id") or ref.get("source_ref_id")
        revision = ref.get("revision")
        key = (str(native), str(revision))
        entry = sources.setdefault(key, {
            "source": native,
            "revision": revision,
            "source_ref_count": 0,
            "anchor_count": 0,
        })
        entry["source_ref_count"] += 1
        if ref.get("anchor"):
            entry["anchor_count"] += 1
    return list(sources.values())


def _compact_authority(authority: dict | None) -> dict | None:
    if not authority:
        return None
    return {key: authority.get(key) for key in ("owner", "binding_id")
            if authority.get(key) is not None}


def _resolution_status(entry: dict) -> str:
    resolution = entry.get("resolution") or "NOT_RESOLVED"
    if resolution == "CURRENT":
        return Status.OK
    if resolution in {"SNAPSHOT_ONLY", "HISTORICAL_ONLY"}:
        return Status.PARTIAL
    if resolution in {Status.BLOCKED, Status.MISSING, Status.INVALID_SCOPE,
                      Status.CONFLICTED, Status.NOT_CHECKED, Status.STALE}:
        return resolution
    return Status.PARTIAL


def _select_facets(kind: str, question: str, aspect_names: list[str],
                   available_dims: set[str] | None = None) -> list[str]:
    if kind == "MISSION":
        return list(aspect_names)
    hints = FACET_HINTS.get(kind, {})
    available_dims = available_dims or set(aspect_names)
    selected: list[str] = []
    for name, terms in hints.items():
        if name not in available_dims:
            continue
        if any(term.lower() in (question or "").lower() for term in terms):
            selected.append(name)
    # Boundaries are always in scope.  Other aspects enter only through the
    # question's facet hints; decisive exceptions are surfaced as short
    # cross-cutting strings instead of dragging in every related aspect.
    if "evidence_boundary" in available_dims and "evidence_boundary" not in selected:
        selected.append("evidence_boundary")
    if not selected:
        if kind == "CREATOR" and hints:
            return sorted(hints)
        return list(aspect_names) or sorted(available_dims)
    return selected


def _statement_from_payload(payload: dict) -> str:
    statement = payload.get("statement")
    if statement:
        return statement
    parts = []
    for key, value in payload.items():
        if key in {"as_of", "authority_owner", "additional_anchors"}:
            continue
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}")
        if len(parts) >= 4:
            break
    return "; ".join(parts)


def _key_fields(payload: dict, statement: str = "") -> dict:
    # Fields already rendered in the statement, as boundaries/unknowns, or too
    # low-signal for a first-pass context are omitted rather than duplicated.
    skip = {"statement", "as_of", "authority_owner", "additional_anchors",
            "items", "uncertainty", "not_creator_claim",
            "not_a_reason_to_repeat_reminders", "derived_from", "user_lock",
            "status_tokens", "decisive_exception"}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in skip:
            continue
        if isinstance(value, bool):
            if value is True:
                continue
            out[key] = value
            continue
        if value is None:
            out[key] = value
            continue
        if isinstance(value, (int, float)):
            rendered = {str(value)}
            if isinstance(value, int):
                rendered.add(f"{value:,}")
            if any(r in (statement or "") for r in rendered):
                continue
            out[key] = value
            continue
        if isinstance(value, str):
            if value and value in (statement or ""):
                continue
            out[key] = _clip(value, 220)
            continue
        if isinstance(value, list) and value and all(
                isinstance(x, (str, int, float, bool)) for x in value):
            if all(str(x) in (statement or "") for x in value):
                continue
            out[key] = [_clip(x, 100) for x in value[:6]]
    return out


def _boundaries(aspects: list[dict], questions: list[dict],
                relations: list[dict], selected_names: set[str] | None = None) -> tuple[list[str], list[str]]:
    boundaries: list[str] = []
    unknowns: list[str] = []
    selected_names = selected_names or set()

    def add(bucket: list[str], text: Any) -> None:
        if isinstance(text, str) and text.strip():
            text = _clip(text.strip(), 240)
            if text not in bucket:
                bucket.append(text)

    for entry in aspects:
        name = entry.get("name")
        payload = entry.get("value") or {}
        for key in ("uncertainty", "decisive_exception", "not_creator_claim"):
            if key == "decisive_exception" and name in selected_names:
                continue  # already carried on the fact itself
            if key == "uncertainty" and name == "evidence_boundary":
                continue  # the Chinese unknown below is the actionable form
            add(boundaries, payload.get(key))
        if payload.get("derived_report") or payload.get("original_emails_read") is False:
            add(boundaries, "派生报告，不是原始邮件；原始 EXU 邮件未独立打开")
        if payload.get("tool_chain_incomplete"):
            add(boundaries, "工具链与各环节前后耗时未完整披露")
        if payload.get("net_profit") is None and "net_profit" in payload:
            add(unknowns, "净利润未提供；收入/成本不能当净利")
        if payload.get("x_brief_obtained") is False:
            add(unknowns, "X brief 与官方 rubric 未取得")
        if payload.get("video_url") is None and "video_url" in payload:
            add(unknowns, "无官方视频 URL 或可核验时间码")
        if payload.get("camera_model_unverified"):
            add(unknowns, "相机型号未核实")
        if payload.get("contracts") is None and "contracts" in payload:
            add(unknowns, "合同/后台/票据未提供")
        for item in payload.get("items") or []:
            add(unknowns, item)
    for q in questions:
        payload = q.get("value") or {}
        for item in payload.get("items") or []:
            add(unknowns, item)
    for rel in relations:
        if not rel.get("effective", False):
            add(boundaries, f"关系 {rel.get('type')} 为 {rel.get('verification')}，不作为有效事实")
    if any((e.get("verification") or "CASE") != "VERIFIED" for e in aspects):
        add(boundaries, "CASE/INFERENCE 为作者自述或案例推断，不是独立核实")
    return boundaries, unknowns


STATUS_RANK = {
    Status.OK: 0,
    Status.PARTIAL: 1,
    Status.NOT_CHECKED: 2,
    Status.STALE: 2,
    Status.UNKNOWN: 2,
    Status.CONFLICTED: 2,
    Status.MISSING: 3,
    Status.BLOCKED: 4,
    Status.INVALID_SCOPE: 5,
}


def _worst_status(statuses: list[str]) -> str:
    ranked = [STATUS_RANK.get(s, 2) for s in statuses if s]
    return max(statuses, key=lambda s: STATUS_RANK.get(s, 2)) if ranked else Status.OK


def _object_authority(core, object_id: str, scope: str) -> dict | None:
    binding = core._store.get_binding(object_id, None, scope)
    return core._authority_summary(binding) if binding else None


def build_concise(core, question: str, scope: str, object_id: str | None,
                  max_chars: int = 6000) -> dict:
    params = {"question": question, "object_id": object_id,
              "mode": CONCISE, "scope": scope}
    try:
        obj = _resolve_target(core, question, scope, object_id)
    except _PackError as exc:
        reason = "OBJECT_SCOPE_DENIED" if exc.status == Status.INVALID_SCOPE else "OBJECT_NOT_FOUND"
        return _pack_envelope("get_context_pack", params, exc.status, None,
                              [exc.message], missing=[{"reason": reason}])
    object_id = obj["object_id"]
    object_raw_type = obj.get("type")
    names = _aspect_names(core, object_id)
    if not names:
        return _pack_envelope("get_context_pack", params, Status.UNKNOWN, None,
                              ["object has no current aspects"])
    kind = _normalise_kind(obj, names)
    available_dims = set(names)
    if kind == "CREATOR":
        # Creator evidence can live on the authored video; select its facet
        # names up front so the primary call does not drag in unrelated
        # blocked aspects just to discover them.
        available_dims |= set(FACET_HINTS.get("CREATOR", {}))
    selected = _select_facets(kind, question, names, available_dims)
    selected_set = set(selected)
    requested_aspects = [name for name in selected if name in names]
    intent = {"MISSION": "current_decision", "CREATOR": "creator_overview"}.get(
        kind, "facet_bundle")
    aspects_arg = requested_aspects
    if not aspects_arg and kind != "CREATOR":
        aspects_arg = names
    env = core.get_context(intent=intent, object_id=object_id, aspects=aspects_arg,
                           scope=scope, depth=1, max_chars=60000)
    source_status = env["execution"]["status"]
    if source_status in {Status.NOT_FOUND, Status.INVALID_SCOPE}:
        return _pack_envelope("get_context_pack", params, source_status, None,
                              env.get("warnings") or [],
                              missing=env.get("missing") or [])
    warnings = list(env.get("warnings") or [])
    if source_status != Status.OK:
        warnings.append(f"PACK_STATUS_PROPAGATED: {source_status}")
    package = (env.get("result") or {}).get("package") or {}
    aspects = package.get("aspects") or {}
    observations = list(package.get("observations") or [])
    evidence = list(env.get("evidence") or [])
    internal_reads = 2
    related_statuses: list[str] = []

    relations: list[dict] = []
    if kind == "CREATOR":
        rel_env = core.traverse_relations(object_id, ["AUTHORED"], "out", 1, scope, True)
        internal_reads += 1
        edges = ((rel_env.get("result") or {}).get("edges") or [])
        for edge in edges:
            relations.append({
                "id": edge["relation_id"],
                "type": edge["relation_type"],
                "from": edge["from_id"],
                "to": edge["to_id"],
                "verification": edge["verification"],
                "effective": bool(edge.get("effective")),
            })
        if not relations:
            candidates = ((rel_env.get("result") or {}).get("excluded_candidates") or [])
            for edge in candidates:
                relations.append({
                    "id": edge["relation_id"],
                    "type": edge["relation_type"],
                    "from": edge["from_id"],
                    "to": edge["to_id"],
                    "verification": edge["verification"],
                    "effective": False,
                })
        # A creator object often only owns profile; the evidence lives on its
        # authored video.  Merge that video's observations instead of silently
        # returning only the local profile.  creator_overview omits the cost and
        # disclosure dimensions, so request those routes explicitly when the
        # selected facets need them.
        seen_observations = {obs.get("observation_id") for obs in observations}
        for edge in relations:
            if edge["type"] != "AUTHORED" or edge["from"] != object_id:
                continue
            if not edge["effective"]:
                # An UNVERIFIED AUTHORED edge is a candidate; never use it to
                # pull another object's facts into the pack.
                continue
            video_id = edge["to"]
            routes = ["creator_overview"]
            if "economics" in selected_set:
                routes.append("profit")
            if "commercial" in selected_set:
                routes.append("sponsorship")
            for route in routes:
                video_env = core.get_context(intent=route, object_id=video_id,
                                            aspects=[], scope=scope, depth=1, max_chars=60000)
                internal_reads += 1
                if video_env["execution"]["status"] in {Status.OK, Status.PARTIAL}:
                    video_pkg = (video_env.get("result") or {}).get("package") or {}
                    for obs in video_pkg.get("observations") or []:
                        oid = obs.get("observation_id")
                        if oid not in seen_observations:
                            observations.append(obs)
                            seen_observations.add(oid)
                    evidence.extend(video_env.get("evidence") or [])
                    related_statuses.append(video_env["execution"]["status"])
                    if video_env["execution"]["status"] != Status.OK:
                        warnings.append(
                            f"PACK_RELATED_STATUS: {video_id}/{route} "
                            f"{video_env['execution']['status']}")
                else:
                    related_statuses.append(video_env["execution"]["status"])
                    warnings.append(
                        f"PACK_RELATED_UNREADABLE: {video_id}/{route} "
                        f"{video_env['execution']['status']}")

    refs = _ref_index(package, evidence)
    for obs in observations:
        dim = obs.get("dimension")
        if dim:
            available_dims.add(OBS_TO_ASPECT.get(dim, dim))
    qtokens = _tokens(question or "")

    scored: list[tuple[float, str, dict]] = []
    for name in selected:
        entry = aspects.get(name)
        if not entry:
            continue
        resolution = entry.get("resolution")
        if resolution in {Status.BLOCKED, Status.MISSING, Status.INVALID_SCOPE}:
            continue
        payload, historical = _entry_payload(entry)
        statement = _statement_from_payload(payload)
        if not statement:
            continue
        authority = _entry_authority(entry, payload)
        text = f"{name} {statement} {_canon(payload)}"
        hints = FACET_HINTS.get(kind, {}).get(name, [])
        fact = {
            "id": f"aspect:{object_id}/{name}",
            "kind": "aspect",
            "statement": statement,
            "stance": entry.get("verification"),
            "authority": _compact_authority(authority),
            "anchors": _anchors(entry, refs),
            "as_of": payload.get("as_of") or entry.get("valid_from"),
            "key": _key_fields(payload, statement),
        }
        if resolution != "CURRENT":
            fact["resolution"] = resolution
        if entry.get("freshness") and entry["freshness"] != "FRESH":
            fact["freshness"] = entry["freshness"]
        if entry.get("is_latest_verified") is False:
            fact["is_latest_verified"] = False
        if historical or resolution == "HISTORICAL_ONLY":
            fact["historical"] = True
            fact["lifecycle"] = entry.get("lifecycle")
        elif entry.get("lifecycle") and entry["lifecycle"] != "CURRENT":
            fact["lifecycle"] = entry["lifecycle"]
        if payload.get("decisive_exception"):
            fact["decisive_exception"] = payload["decisive_exception"]
        scored.append((_score(text, qtokens, hints), name, fact))

    aspect_ids = {f["id"].rsplit("/", 1)[-1] for f in
                  (item[2] for item in scored) if f["kind"] == "aspect"}
    for obs in observations:
        dim = obs.get("dimension")
        mapped = OBS_TO_ASPECT.get(dim, dim)
        if mapped not in selected_set:
            continue
        if mapped in aspect_ids:
            continue
        statement = obs.get("statement") or ""
        if not statement:
            continue
        obs_authority = _object_authority(core, obs.get("object_id"), scope)
        fact = {
            "id": obs["observation_id"],
            "kind": "observation",
            "statement": statement,
            "stance": obs.get("stance"),
            "resolution": "OBSERVATION",
            "authority": _compact_authority(obs_authority),
            "anchors": _anchors(obs, refs),
            "as_of": obs.get("created_at"),
        }
        scored.append((_score(statement, qtokens,
                              FACET_HINTS.get(kind, {}).get(mapped, [])),
                       obs["observation_id"], fact))

    scored.sort(key=lambda item: (-item[0], item[1]))
    facts = [fact for _, _, fact in scored]
    boundaries, unknowns = _boundaries(
        [{**entry, "name": name} for name, entry in aspects.items()],
        [], relations, selected_set)
    if any(f.get("stance") and f["stance"] != "VERIFIED" for f in facts):
        note = "CASE/INFERENCE 为作者自述或案例推断，不是独立核实"
        if note not in boundaries:
            boundaries.append(note)

    authorities: dict[str, dict] = {}
    for fact in facts:
        authority = fact.get("authority")
        if not authority:
            continue
        key = authority.get("binding_id") or authority.get("owner") or ""
        authorities[key] = _compact_authority(authority)
    if not authorities:
        object_authority = _object_authority(core, object_id, scope)
        if object_authority:
            authorities[object_authority.get("binding_id") or "object"] = _compact_authority(object_authority)

    critical = []
    for name, entry in aspects.items():
        if name in selected:
            continue
        payload, _ = _entry_payload(entry)
        if payload.get("decisive_exception"):
            critical.append({
                "aspect": name,
                "text": payload["decisive_exception"],
            })

    as_of_values = [f.get("as_of") for f in facts if f.get("as_of")]
    if not as_of_values:
        as_of_values = [e.get("valid_from") for e in aspects.values() if e.get("valid_from")]
    if not as_of_values:
        as_of_values = [obj.get("created_at")] if obj.get("created_at") else []

    fact_statuses = [_resolution_status(
        {"resolution": f.get("resolution", "CURRENT")})
        for f in facts if f.get("kind") == "aspect"]
    status = _worst_status([source_status, *related_statuses, *fact_statuses])
    if status == Status.OK and not facts:
        status = Status.PARTIAL
    if status != source_status:
        warnings.append(f"PACK_STATUS_ADJUSTED: source={source_status} pack={status}")

    pack = {
        "mode": CONCISE,
        "status": status,
        "source_status": source_status,
        "object": {"id": object_id, "type": object_raw_type,
                   "kind": kind, "name": obj.get("display_name"),
                   "scope": obj.get("scope"),
                   "resolution": obj.get("_resolution", "EXACT")},
        "as_of": max(as_of_values) if as_of_values else "",
        "facts": facts,
        "critical_exceptions": critical,
        "relations": relations,
        "authority": list(authorities.values()),
        "boundaries": boundaries,
        "unknowns": unknowns,
        "evidence": {"sources": _source_entries(list(refs.values())),
                     "source_bodies": "UNAVAILABLE"},
        "budget": {"max_chars": max_chars, "chars": 0, "exceeded": False, "dropped": []},
    }
    pack = _fit_budget(pack, max_chars)
    dropped = list(pack["budget"]["dropped"])
    if dropped:
        status = _worst_status([pack["status"], Status.PARTIAL])
        warnings.append("PACK_INCOMPLETE_BUDGET: " + ", ".join(dropped))
    if pack["budget"]["exceeded"]:
        status = _worst_status([status, Status.PARTIAL])
    pack["status"] = status
    _finalize_chars(pack)
    if pack["budget"]["chars"] > max_chars and not pack["budget"]["exceeded"]:
        pack["budget"]["exceeded"] = True
        pack["status"] = _worst_status([pack["status"], Status.PARTIAL])
        _finalize_chars(pack)
    if pack["status"] == Status.OK and not dropped and not pack["budget"]["exceeded"] and facts:
        reason = None
    elif dropped:
        reason = "BUDGET_DROPPED_FACTS"
    elif pack["budget"]["exceeded"]:
        reason = "BUDGET_EXCEEDED"
    elif pack["status"] != Status.OK:
        reason = f"SOURCE_STATUS_{pack['status']}"
    else:
        reason = "NO_FACTS"
    pack["completeness"] = {
        "complete": pack["status"] == Status.OK and not dropped
                    and not pack["budget"]["exceeded"] and bool(facts),
        "reason": reason,
        "dropped_facts": dropped,
        "source_status": source_status,
    }
    pack["budget"]["truncated"] = bool(dropped)
    for _ in range(3):
        _finalize_chars(pack)
        if pack["budget"]["chars"] <= max_chars or pack["budget"]["exceeded"]:
            break
        pack["budget"]["exceeded"] = True
        pack["status"] = _worst_status([pack["status"], Status.PARTIAL])
        pack["completeness"]["complete"] = False
        pack["completeness"]["reason"] = "BUDGET_EXCEEDED"
    _finalize_chars(pack)
    return _pack_envelope("get_context_pack", params, pack["status"], pack, warnings,
                          missing=env.get("missing") or [],
                          provider_io={"drive_api_calls": 0, "source_documents_read": 0,
                                       "source_chars_returned": 0, "full_source_reads": 0,
                                       "internal_core_reads": internal_reads})


def _finalize_chars(pack: dict) -> int:
    """Set budget.chars to the actual serialized length, including itself."""
    for _ in range(6):
        measured = len(_canon(pack))
        if pack["budget"]["chars"] == measured:
            return measured
        pack["budget"]["chars"] = measured
    return len(_canon(pack))


def _fit_budget(pack: dict, max_chars: int) -> dict:
    draft = dict(pack)
    draft["facts"] = list(pack["facts"])
    draft["budget"] = {**pack["budget"], "chars": 0, "exceeded": False, "dropped": []}
    measured = _finalize_chars(draft)
    if measured <= max_chars:
        return draft
    protected = {"decision", "evidence_boundary", "anti_loop"}
    dropped: list[str] = []
    while True:
        removable = [i for i, fact in enumerate(draft["facts"])
                     if fact["id"].rsplit("/", 1)[-1] not in protected
                     and not fact.get("decisive_exception")]
        if not removable:
            break
        idx = removable[-1]  # lowest-ranked fact first
        dropped.append(draft["facts"].pop(idx)["id"])
        measured = _finalize_chars(draft)
        if measured <= max_chars:
            break
    draft["budget"]["dropped"] = dropped
    measured = _finalize_chars(draft)
    draft["budget"]["exceeded"] = measured > max_chars
    return draft


def _ref_detail(ref: dict) -> dict:
    return {
        "source_ref_id": ref.get("source_ref_id"),
        "native_id": ref.get("native_id"),
        "revision": ref.get("revision"),
        "location": ref.get("location"),
        "anchor": ref.get("anchor"),
        "status": ref.get("status"),
        "access_scope": ref.get("access_scope"),
    }


def _sha_from_section(body: str) -> str | None:
    match = re.search(r"sha256=([0-9a-f]{64})", body or "")
    return match.group(1) if match else None


def build_detailed(core, object_id: str, scope: str, item_ids: list[str],
                   max_chars: int = 16000) -> dict:
    params = {"mode": DETAILED, "object_id": object_id, "scope": scope}
    try:
        obj = _resolve_target(core, "", scope, object_id)
    except _PackError as exc:
        reason = "OBJECT_SCOPE_DENIED" if exc.status == Status.INVALID_SCOPE else "OBJECT_NOT_FOUND"
        return _pack_envelope("get_context_pack", params, exc.status, None,
                              [exc.message], missing=[{"reason": reason}])
    items = []
    statuses: list[str] = []
    reads = 1
    related_ids: set[str] | None = None
    for item_id in item_ids:
        if item_id.startswith("aspect:"):
            body = item_id[len("aspect:"):]
            if "/" not in body:
                items.append({"id": item_id, "error": Status.INVALID_SCOPE,
                              "message": "aspect id must be aspect:<object_id>/<aspect>"})
                statuses.append(Status.INVALID_SCOPE)
                continue
            encoded_object, name = body.rsplit("/", 1)
            if encoded_object != object_id:
                items.append({
                    "id": item_id, "error": Status.INVALID_SCOPE,
                    "message": "aspect id encodes a different object_id",
                    "encoded_object_id": encoded_object,
                    "requested_object_id": object_id,
                })
                statuses.append(Status.INVALID_SCOPE)
                continue
            env = core.get_current_state(object_id, aspect=name, scope=scope)
            reads += 1
            status = env["execution"]["status"]
            if status not in {Status.OK, Status.PARTIAL}:
                items.append({"id": item_id, "error": status,
                              "message": "aspect could not be read"})
                statuses.append(status)
                continue
            entry = (env.get("result") or {}).get("aspects", [{}])[0]
            payload, historical = _entry_payload(entry)
            resolution = entry.get("resolution")
            authority = _entry_authority(entry, payload)
            items.append({
                "id": item_id,
                "kind": "aspect",
                "statement": _statement_from_payload(payload),
                "stance": entry.get("verification"),
                "resolution": resolution,
                "historical": historical or resolution == "HISTORICAL_ONLY",
                "lifecycle": entry.get("lifecycle"),
                "aspect_revision": entry.get("revision"),
                "as_of": payload.get("as_of") or entry.get("valid_from"),
                "authority": authority,
                "payload": payload,
                "source_refs": [_ref_detail(r) for r in entry.get("source_refs") or []],
                "content_availability": {
                    "payload": "AVAILABLE" if payload else "UNAVAILABLE",
                    "source_body": "UNAVAILABLE",
                    "reason": "the projection stores payload + SHA-256, not paragraph text",
                },
            })
            statuses.append(_resolution_status(entry))
        elif item_id.startswith(("obs/", "obs-")):
            env = core.trace_evidence(item_id, scope=scope, expand=True)
            reads += 1
            status = env["execution"]["status"]
            if status != Status.OK:
                items.append({"id": item_id, "error": status,
                              "message": "observation could not be traced"})
                statuses.append(status)
                continue
            observation = ((env.get("result") or {}).get("item") or {}).get("observation") or {}
            observed_object = observation.get("object_id")
            if observed_object != object_id:
                if related_ids is None:
                    rel_env = core.traverse_relations(object_id, ["AUTHORED"], "out",
                                                      1, scope, False)
                    reads += 1
                    related_ids = {
                        edge["to_id"] for edge in
                        ((rel_env.get("result") or {}).get("edges") or [])
                        if edge.get("effective")
                    }
                if observed_object not in related_ids:
                    items.append({
                        "id": item_id,
                        "error": Status.INVALID_SCOPE,
                        "message": "observation belongs to a different object and is not "
                                   "reachable through an effective AUTHORED relation",
                        "observed_object_id": observed_object,
                        "requested_object_id": object_id,
                    })
                    statuses.append(Status.INVALID_SCOPE)
                    continue
            sections = []
            for section in (env.get("result") or {}).get("sections") or []:
                sections.append({
                    "anchor": section.get("anchor"),
                    "content_available": False,
                    "content_sha256": _sha_from_section(section.get("body") or ""),
                    "note": "placeholder only; paragraph text is outside the database",
                })
            items.append({
                "id": item_id,
                "kind": "observation",
                "statement": observation.get("statement"),
                "detail": observation.get("detail"),
                "stance": observation.get("stance"),
                "sections": sections,
                "source_refs": [_ref_detail(r) for r in env.get("evidence") or []],
                "content_availability": {
                    "statement": "AVAILABLE",
                    "source_body": "UNAVAILABLE",
                },
            })
            statuses.append(Status.OK)
        else:
            items.append({"id": item_id, "error": Status.INVALID_SCOPE,
                          "message": "use aspect:<object_id>/<aspect> or obs-<id>"})
            statuses.append(Status.INVALID_SCOPE)
    status = _worst_status([Status.OK, *statuses])
    pack = {
        "mode": DETAILED,
        "status": status,
        "object": {"id": object_id, "type": obj.get("type"),
                   "name": obj.get("display_name"), "scope": obj.get("scope")},
        "items": items,
        "content_availability": {
            "source_bodies": "UNAVAILABLE",
            "reason": "this projection stores [REDACTED_LOCAL] placeholders plus SHA-256; "
                      "no paragraph text is in the database",
        },
        "authority_boundary": "CASE/INFERENCE 为作者自述或案例推断，不是独立核实",
        "evidence_policy": "full SourceRef metadata is returned here only; CONCISE keeps pointers",
        "budget": {"max_chars": max_chars, "chars": 0, "exceeded": False},
    }
    _finalize_chars(pack)
    pack["budget"]["exceeded"] = pack["budget"]["chars"] > max_chars
    if pack["budget"]["exceeded"]:
        pack["status"] = _worst_status([pack["status"], Status.PARTIAL])
        _finalize_chars(pack)
    return _pack_envelope("get_context_pack", params, pack["status"], pack, [],
                          provider_io={"drive_api_calls": 0, "source_documents_read": 0,
                                       "source_chars_returned": 0, "full_source_reads": 0,
                                       "internal_core_reads": reads})


def _pack_envelope(operation: str, params: dict, status: str, pack: Any,
                   warnings: list[str], missing: list[Any] | None = None,
                   provider_io: dict | None = None) -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "request": {"operation": operation, "params": params},
        "execution": {
            "status": status,
            "retrieval_path": ["context:pack", f"mode:{params.get('mode', CONCISE)}"],
            "provider_io": provider_io or {"drive_api_calls": 0,
                                           "source_documents_read": 0,
                                           "source_chars_returned": 0,
                                           "full_source_reads": 0},
            "generated_at": utcnow(),
        },
        "result": pack,
        "warnings": warnings,
        "missing": missing or [],
    }
