"""Deterministic, fully simulated fixtures for the Nexus P1 prototype.

No Google Drive content is copied here. Every source ref uses a `SIM_*`
system and a `sim://` native id. The DLC-01, creator and long-document cases
are modelled on the *shape* described in the task book / P0 study, not on the
private contents of the real documents. Anchors that cannot exist (video
timecodes) are explicitly marked `NO_TIMECODE` rather than invented.
"""

from __future__ import annotations

import hashlib
import json

from .db import Store, canonical_json

SIM_DRIVE = "SIM_DRIVE"
SIM_EXTERNAL = "SIM_EXTERNAL"
SIM_VIDEO = "SIM_VIDEO"

SCOPES = [
    ("global", "root", "cross-scope queries only; no source reads"),
    ("project:DLC-01", "project", "simulated DLC-01 project"),
    ("external:exu", "external", "simulated official EXU communication"),
    ("course:DEMO0009", "course", "simulated course object"),
    ("creator:CREATOR-A", "creator", "de-identified creator dossier"),
    ("creative:first-video", "creative", "simulated creative living document"),
]

LONG_DOC_SECTIONS = [
    ("§1-purpose", "创作目的", 2600),
    ("§2-direction", "方向与边界", 2400),
    ("§3-first-video", "首期视频", 2600),
    ("§4-style", "风格与质量", 2400),
    ("§5-research", "创作者研究", 2200),
    ("§6-release", "发布与排期", 2200),
    ("§7-operations", "运营与维护", 2400),
    ("§8-appendix", "附录与历史", 1800),
]

_FILLER = (
    "本段为合成填充文本，不包含任何真实个人隐私或未公开资料；"
    "它用于模拟一份持续更新的创作路线长文中的背景说明，使全量读取与按需读取的差异可以被实际测量。"
)


def _filler(chars: int) -> str:
    unit = _FILLER
    repeats = max(1, chars // len(unit) + 1)
    return (unit * repeats)[:chars]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- DLC-01
def build_dlc(store: Store) -> None:
    store.add_scope("project:DLC-01", "project", "simulated DLC-01 project")
    store.add_scope("external:exu", "external", "simulated official communication")
    store.add_scope("course:DEMO0009", "course", "simulated course object")

    store.add_object("project/DLC-01", "Project", "DLC-01 模拟项目", "project:DLC-01")
    store.add_object("principle/P-01", "Principle", "最小闭环优先原则", "project:DLC-01")
    store.add_object("discussion/DLC-01-LEGACY-2026-09", "HistoricalDiscussion",
                     "2026-09 旧讨论（非决定）", "project:DLC-01")
    store.add_object("external/EXU-LAB-2026-10-27", "OfficialCommunication",
                     "EXU 实验室开放通知（模拟）", "external:exu")
    store.add_object("course/DEMO0009", "Course", "DEMO0009（模拟课程对象）", "course:DEMO0009")
    store.add_object("project/DLC-01-SIBLING", "Project", "DLC-01 相邻项目", "project:DLC-01")

    store.add_source_ref("src/dlc-mission-r1", SIM_DRIVE, "sim-dlc-mission", "r1",
                         "Drive/Projects/DLC-01/Mission", "§2 决定",
                         "project:DLC-01")
    store.add_source_ref("src/dlc-principle-r1", SIM_DRIVE, "sim-dlc-principle", "r1",
                         "Drive/Projects/DLC-01/Principles", "§1 原则",
                         "project:DLC-01")
    store.add_source_ref("src/dlc-legacy-r1", SIM_DRIVE, "sim-dlc-legacy", "r1",
                         "Drive/Archive/DLC-01/Discussion-2026-09", "§4 讨论",
                         "project:DLC-01")
    store.add_source_ref("src/exu-email-r1", SIM_EXTERNAL, "sim-exu-email-2026-10-27", "r1",
                         "sim://mail/exu-lab-2026-10-27", "message/body",
                         "external:exu")
    store.set_source_head(SIM_DRIVE, "sim-dlc-mission", "r1", "2026-10-28T09:00:00Z")
    store.set_source_head(SIM_DRIVE, "sim-dlc-principle", "r1", "2026-10-28T09:00:00Z")
    store.set_source_head(SIM_DRIVE, "sim-dlc-legacy", "r1", "2026-10-28T09:00:00Z")
    store.set_source_head(SIM_EXTERNAL, "sim-exu-email-2026-10-27", "r1",
                          "2026-10-28T09:00:00Z")

    store.add_aspect(
        "project/DLC-01:decision:r1", "project/DLC-01", "decision",
        {
            "decision_id": "D1",
            "statement": "先完成模块 A 与 B 的最小闭环，暂不采购新硬件",
            "status": "ACCEPTED",
            "decided_by": "user:owner",
            "decided_at": "2026-10-20",
            "scope_note": "适用于 DLC-01 项目本层，不外推到其他项目",
        },
        "1", "CURRENT", "VERIFIED", 1, True, "2026-10-20T00:00:00Z", None,
        "src/dlc-mission-r1",
    )
    store.add_aspect(
        "project/DLC-01:learner_state:r1", "project/DLC-01", "learner_state",
        {
            "current_module": "模块 A",
            "progress": "第 2 周 / 共 4 周",
            "last_checkpoint": "完成实验 1 报告",
            "next_checkpoint": "2026-11-02",
            "reported_by": "user:owner（自述）",
        },
        "1", "CURRENT", "CASE", 1, True, "2026-10-28T00:00:00Z", None,
        "src/dlc-mission-r1",
    )
    store.add_aspect(
        "project/DLC-01:status:r1", "project/DLC-01", "status",
        {"phase": "IN_PROGRESS", "blocker": None},
        "1", "CURRENT", "VERIFIED", 1, True, "2026-10-28T00:00:00Z", None,
        "src/dlc-mission-r1",
    )
    store.add_aspect(
        "principle/P-01:constraint:r1", "principle/P-01", "constraint",
        {
            "principle": "先建立可复现的最小闭环，再扩展工具链",
            "applies_to": ["project/DLC-01"],
            "prohibited": ["为追求工具完整度而延迟首个可用交付"],
        },
        "1", "CURRENT", "VERIFIED", 1, True, "2026-10-18T00:00:00Z", None,
        "src/dlc-principle-r1",
    )
    store.add_aspect(
        "discussion/DLC-01-LEGACY-2026-09:decision:r1",
        "discussion/DLC-01-LEGACY-2026-09", "decision",
        {
            "decision_id": None,
            "statement": "曾讨论“先采购新硬件再开始”",
            "status": "NOT_DECISION",
            "reason": "讨论未形成用户决定，且已被 D1 取代",
        },
        "1", "HISTORICAL", "VERIFIED", 1, True, "2026-09-15T00:00:00Z",
        "2026-10-20T00:00:00Z", "src/dlc-legacy-r1",
    )
    store.add_aspect(
        "external/EXU-LAB-2026-10-27:requirement:r1", "external/EXU-LAB-2026-10-27",
        "requirement",
        {
            "change": "实验室周三开放时间调整为 16:00-20:00",
            "effective_from": "2026-11-15",
            "source_kind": "official communication",
        },
        "1", "CURRENT", "VERIFIED", 1, True, "2026-10-27T00:00:00Z", None,
        "src/exu-email-r1",
    )

    store.add_authority("auth-decision", "aspect", "project/DLC-01", "decision",
                        "user:owner", "project:DLC-01", "sim-dlc-mission",
                        "2026-10-20T00:00:00Z", "USER_ONLY", "NATIVE")
    store.add_authority("auth-project", "object", "project/DLC-01", None,
                        "user:owner", "project:DLC-01", "sim-dlc-mission",
                        "2026-10-01T00:00:00Z", "USER_ONLY", "NATIVE")
    store.add_authority("auth-principle", "object", "principle/P-01", None,
                        "user:owner", "project:DLC-01", "sim-dlc-principle",
                        "2026-10-01T00:00:00Z", "USER_ONLY", "NATIVE")
    store.add_authority("auth-legacy", "object", "discussion/DLC-01-LEGACY-2026-09", None,
                        "user:owner", "project:DLC-01", "sim-dlc-legacy",
                        "2026-10-01T00:00:00Z", "READ_ONLY", "NATIVE")
    store.add_authority("auth-exu", "object", "external/EXU-LAB-2026-10-27", None,
                        "external:exu", "external:exu", "sim-exu-email",
                        "2026-10-27T00:00:00Z", "READ_ONLY", "NATIVE")
    store.add_authority("auth-course", "object", "course/DEMO0009", None,
                        "exu:demo0009", "course:DEMO0009", "sim-course",
                        "2026-09-01T00:00:00Z", "READ_ONLY", "NATIVE")

    store.add_relation("rel-governed", "project/DLC-01", "GOVERNED_BY",
                       "principle/P-01", "VERIFIED", "project:DLC-01",
                       ["src/dlc-principle-r1"], "2026-10-01T00:00:00Z")
    store.add_relation("rel-applies", "principle/P-01", "APPLIES_TO",
                       "project/DLC-01", "VERIFIED", "project:DLC-01",
                       ["src/dlc-principle-r1"], "2026-10-01T00:00:00Z")
    store.add_relation("rel-informs", "external/EXU-LAB-2026-10-27", "INFORMS",
                       "project/DLC-01", "VERIFIED", "project:DLC-01",
                       ["src/exu-email-r1"], "2026-10-27T00:00:00Z")
    store.add_relation("rel-depends", "project/DLC-01", "DEPENDS_ON",
                       "course/DEMO0009", "VERIFIED", "project:DLC-01",
                       ["src/dlc-mission-r1"], "2026-10-01T00:00:00Z")
    store.add_relation("rel-legacy", "project/DLC-01", "REFERENCES",
                       "discussion/DLC-01-LEGACY-2026-09", "VERIFIED", "project:DLC-01",
                       ["src/dlc-legacy-r1"], "2026-10-20T00:00:00Z")
    store.add_relation("rel-sibling-unverified", "project/DLC-01", "RELATED_TO",
                       "project/DLC-01-SIBLING", "UNVERIFIED", "project:DLC-01",
                       [], "2026-10-25T00:00:00Z")

    store.add_projection("proj-dlc-mission", SIM_DRIVE, "sim-dlc-mission", "r1", "FRESH")

    # --- T9 events ---------------------------------------------------------
    f1 = {"kind": "lab_schedule", "day": "Wednesday", "window": "14:00-18:00",
          "effective": "2026-11-01"}
    f2 = {"kind": "lab_schedule", "day": "Wednesday", "window": "16:00-20:00",
          "effective": "2026-11-15"}
    store.conn.execute(
        "INSERT OR IGNORE INTO known_facts(fingerprint, fact, first_event_id, first_seen_at)"
        " VALUES (?,?,?,?)",
        (_sha(canonical_json(f1)), canonical_json(f1), "ev-baseline",
         "2026-10-20T00:00:00Z"),
    )
    store.add_event("ev-baseline", "SYSTEM_OBSERVED", "2026-10-20T00:00:00Z",
                    "project/DLC-01", {"fact": f1}, "src/exu-email-r1",
                    "external:exu", "ev-baseline")
    store.conn.execute(
        "UPDATE events SET processed_at = ?, outcome = ? WHERE event_id = ?",
        ("2026-10-20T00:00:00Z",
         canonical_json({"is_new_material_delta": True, "action": "RECORDED",
                         "decision_changed": False}), "ev-baseline"),
    )
    store.add_event("ev-dup-email", "SYSTEM_OBSERVED", "2026-10-24T00:00:00Z",
                    "project/DLC-01", {"fact": f1}, "src/exu-email-r1",
                    "external:exu", "ev-dup-email")
    store.add_event("ev-official-new", "OFFICIAL_REQUIREMENT", "2026-10-27T00:00:00Z",
                    "project/DLC-01",
                    {"fact": f2, "affected_aspects": ["decision", "status"],
                     "summary": "实验室开放时间再次变更"},
                    "src/exu-email-r1", "external:exu", "ev-official-new")
    store.add_event("ev-user-decision", "USER_DECISION", "2026-10-28T00:00:00Z",
                    "project/DLC-01",
                    {"fact": {"kind": "decision", "decision_id": "D2"},
                     "aspect": "decision", "expected_revision": 1,
                     "changes": {
                         "decision_id": "D2",
                         "statement": "按新开放时间调整排期；模块 A/B 与不采购硬件的边界不变",
                         "supersedes": "D1",
                         "status": "ACCEPTED",
                     }},
                    "src/dlc-mission-r1", "user:owner", "ev-user-decision")
    store.add_event("ev-user-stale", "USER_DECISION", "2026-10-28T00:00:00Z",
                    "project/DLC-01",
                    {"fact": {"kind": "decision", "decision_id": "D3"},
                     "aspect": "decision", "expected_revision": 5,
                     "changes": {"decision_id": "D3"}},
                    "src/dlc-mission-r1", "user:owner", "ev-user-stale")
    store.add_event("ev-wrong-owner", "USER_DECISION", "2026-10-28T00:00:00Z",
                    "project/DLC-01",
                    {"fact": {"kind": "decision", "decision_id": "D4"},
                     "aspect": "decision", "expected_revision": 1,
                     "changes": {"decision_id": "D4"}},
                    "src/exu-email-r1", "external:exu", "ev-wrong-owner")


# ------------------------------------------------------------------- creator
def build_creator(store: Store) -> None:
    store.add_scope("creator:CREATOR-A", "creator", "de-identified creator dossier")
    store.add_object("creator/CREATOR-A", "Creator", "Creator-A（脱敏模拟）", "creator:CREATOR-A")
    store.add_object("video/CREATOR-A-V001", "Video", "Video-A-001（模拟单条视频）",
                     "creator:CREATOR-A")
    store.add_source_ref("src/video-a-001", SIM_VIDEO, "sim-video-a-001", "v1",
                         "sim://video/a-001", "NO_TIMECODE", "creator:CREATOR-A")
    store.set_source_head(SIM_VIDEO, "sim-video-a-001", "v1", "2026-10-28T09:00:00Z")
    store.add_relation("rel-authored", "creator/CREATOR-A", "AUTHORED",
                       "video/CREATOR-A-V001", "VERIFIED", "creator:CREATOR-A",
                       ["src/video-a-001"], "2026-08-13T00:00:00Z")
    store.add_projection("proj-video-a-001", SIM_VIDEO, "sim-video-a-001", "v1", "FRESH")

    store.add_authority("auth-creator", "object", "creator/CREATOR-A", None,
                        "user:owner", "creator:CREATOR-A", "sim-video-a-001",
                        "2026-08-13T00:00:00Z", "USER_ONLY", "NATIVE")
    store.add_authority("auth-video", "object", "video/CREATOR-A-V001", None,
                        "user:owner", "creator:CREATOR-A", "sim-video-a-001",
                        "2026-08-13T00:00:00Z", "READ_ONLY", "NATIVE")

    store.add_aspect(
        "video/CREATOR-A-V001:economics:r1", "video/CREATOR-A-V001", "economics",
        {
            "revenue_reported": "单条商单 4-6 万元（作者自述）",
            "cost_reported": "单条制作成本约 1.2 万元（含外包，作者自述）",
            "net_profit": "NOT_VERIFIED",
            "note": "收入报道减去自述成本不等于已核验净利润；未计税费、平台分成、人力与退款",
            "decisive_exception": "不得把收入直接当作净利润，也不得用两段自述相减生成已核验利润",
        },
        "1", "CURRENT", "CASE", 1, True, "2026-08-13T00:00:00Z", None,
        "src/video-a-001",
    )
    store.add_aspect(
        "creator/CREATOR-A:profile:r1", "creator/CREATOR-A", "profile",
        {
            "followers_reported": "约 20 万",
            "full_time_since": "2023-12",
            "first_ad": "约 ¥3,000，开始创作约 1.5 年后",
            "career_path": "先做剪辑接单，后转自营账号（模拟自述）",
        },
        "1", "CURRENT", "CASE", 1, True, "2026-08-13T00:00:00Z", None,
        "src/video-a-001",
    )

    observations = [
        ("obs-career", "career", "从剪辑接单逐步转向自营账号",
         "CASE", "作者自述职业路径，无第三方履历核验"),
        ("obs-sponsor-1", "commercial", "商单主要来自长期合作的品牌方",
         "CASE", "作者自述"),
        ("obs-sponsor-2", "commercial", "单条商单报价区间约 4-6 万元",
         "CASE", "作者自述，未见合同或平台流水"),
        ("obs-cost-1", "cost", "单条制作成本约 1.2 万元，含部分外包",
         "CASE", "作者自述，未拆分人力与设备折旧"),
        ("obs-device", "equipment", "主机为全画幅微单，配无线领夹麦与补光",
         "CASE", "作者在视频中自述"),
        ("obs-ai", "ai_workflow", "使用 AI 辅助脚本与粗剪，作者称提速约 3 倍",
         "CASE", "作者自述；未独立测量，不得当作已验证的普遍事实"),
        ("obs-ops", "operations", "每周更新 1-2 条，团队约 2 人",
         "CASE", "作者自述运营节奏"),
        ("obs-tradeoff", "tradeoff", "为保持真实感拒绝过度脚本化与夸张钩子",
         "CASE", "作者自述创作取舍"),
        ("obs-disclosure", "disclosure", "该视频含品牌赞助与产品推荐（模拟案例）",
         "CASE", "赞助不否定作者经验，但引用其商业建议时必须披露利益关系"),
        ("obs-published", "career", "该视频发布于 2026-08-13",
         "VERIFIED", "平台元数据（模拟），可独立核验"),
    ]
    for oid, dim, stmt, stance, detail in observations:
        store.add_observation(oid, "video/CREATOR-A-V001", dim, stmt, stance, detail,
                              ["src/video-a-001"])


# ---------------------------------------------------------------- long document
def build_creative(store: Store) -> dict:
    store.add_scope("creative:first-video", "creative", "simulated creative living document")
    store.add_object("creative/FIRST-VIDEO", "CreativePlan",
                     "原创自媒体创作｜方向 × 研究 × 作品路线（模拟投影）",
                     "creative:first-video")
    store.add_source_ref("src/longdoc-r7", SIM_DRIVE, "sim-creative-doc", "r7",
                         "Drive/Creator/LivingDoc", "nav", "creative:first-video",
                         status="OK")
    store.set_source_head(SIM_DRIVE, "sim-creative-doc", "r7", "2026-10-28T09:00:00Z")
    store.add_source_ref("src/longdoc-blocked", SIM_DRIVE, "sim-creative-doc-release", "r3",
                         "Drive/Creator/LivingDoc#release", "§6-release",
                         "creative:first-video", status="BLOCKED")
    store.add_projection("proj-creative", SIM_DRIVE, "sim-creative-doc", "r7", "FRESH")

    full_parts: list[str] = []
    # Section-scoped SourceRefs let the context layer expand exactly one
    # decisive section instead of the whole document.
    section_ref = {
        "§1-purpose": "src/longdoc-purpose",
        "§3-first-video": "src/longdoc-first-video",
        "§4-style": "src/longdoc-style",
        "§5-research": "src/longdoc-research",
    }
    for anchor, heading, chars in LONG_DOC_SECTIONS:
        body = f"{heading}\n" + _filler(chars)
        store.add_section("src/longdoc-r7", anchor, heading, body)
        full_parts.append(body)
        if anchor in section_ref:
            ref_id = section_ref[anchor]
            store.add_source_ref(ref_id, SIM_DRIVE, "sim-creative-doc", "r7",
                                 f"Drive/Creator/LivingDoc#{anchor}", anchor,
                                 "creative:first-video")
            store.add_section(ref_id, anchor, heading, body)
    full_text = "\n\n".join(full_parts)

    store.add_aspect(
        "creative/FIRST-VIDEO:first_video:r1", "creative/FIRST-VIDEO", "first_video",
        {
            "topic": "Nexus 天枢：为什么我要重做知识基础设施",
            "stage": "TOPIC_SELECTED",
            "script_status": "NOT_STARTED",
            "shot": False,
            "scheduled": False,
            "decisive_exception": "选题已定不等于已制作；脚本、拍摄、发布排期均未确认",
        },
        "1", "CURRENT", "CASE", 1, True, "2026-10-26T00:00:00Z", None,
        "src/longdoc-first-video",
    )
    store.add_aspect(
        "creative/FIRST-VIDEO:creative_purpose:r1", "creative/FIRST-VIDEO",
        "creative_purpose",
        {
            "purpose": "用真实项目过程表达长期主义，而不是做工具评测号",
            "non_goals": ["不为流量制造焦虑", "不虚构结果"],
        },
        "1", "CURRENT", "CASE", 1, True, "2026-10-26T00:00:00Z", None,
        "src/longdoc-purpose",
    )
    store.add_aspect(
        "creative/FIRST-VIDEO:style_quality:r1", "creative/FIRST-VIDEO", "style_quality",
        {
            "principle": "开头要具体、有张力，但必须真实",
            "boundary": {
                "must_keep": ["真实表达", "不制造虚假钩子"],
                "forbidden": ["夸大结果", "伪造数据", "标题党承诺"],
            },
            "decisive_exception": "吸引力不得以真实性为代价；若冲突，真实性优先",
        },
        "1", "CURRENT", "CASE", 1, True, "2026-10-26T00:00:00Z", None,
        "src/longdoc-style",
    )
    store.add_aspect(
        "creative/FIRST-VIDEO:research_candidates:r1", "creative/FIRST-VIDEO",
        "research_candidates",
        {
            "candidates": [
                {"name": "《人类最强编导》相关视频", "status": "NOT_VERIFIED",
                 "usable_as_evidence": False}
            ],
            "note": "尚未观看原片，不能把标题或二手描述当作已核验技法",
        },
        "1", "CURRENT", "TO_TEST", 1, True, "2026-10-26T00:00:00Z", None,
        "src/longdoc-research",
    )
    store.add_aspect(
        "creative/FIRST-VIDEO:release_boundaries:r1", "creative/FIRST-VIDEO",
        "release_boundaries",
        {"schedule": "无强制排期", "equipment": "不因首期采购昂贵设备"},
        "1", "CURRENT", "CASE", 1, True, "2026-10-26T00:00:00Z", None,
        "src/longdoc-blocked",
    )

    store.add_authority("auth-creative", "object", "creative/FIRST-VIDEO", None,
                        "user:owner", "creative:first-video", "sim-creative-doc",
                        "2026-10-01T00:00:00Z", "USER_ONLY", "NATIVE")
    return {"full_doc_chars": len(full_text), "full_doc_text": full_text,
            "section_chars": {a: len(f"{h}\n" + _filler(c)) for a, h, c in LONG_DOC_SECTIONS}}


def build_all(store: Store) -> dict:
    for scope, kind, desc in SCOPES:
        store.add_scope(scope, kind, desc)
    build_dlc(store)
    build_creator(store)
    stats = build_creative(store)
    return stats


def reset_and_build(db_path: str) -> dict:
    from .db import connect, init_schema

    conn = connect(db_path)
    init_schema(conn)
    store = Store(conn)
    # Clean rebuild: drop all data by recreating the file schema.
    for table in [
        "query_log", "projections", "known_facts", "events", "conflicts",
        "observations", "entity_aliases", "relations", "authority_bindings", "aspects",
        "section_aliases", "source_sections", "source_heads", "source_refs", "objects", "scopes",
    ]:
        conn.execute(f"DELETE FROM {table}")
    return build_all(store)
