"""Task 009: synthetic KB, resolver and controlled experience candidates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.db import Store, connect
from nexus.internalize import kb
from nexus.internalize.experience import ExperienceStore, HumanGate, ReceiptLedger
from nexus.internalize.resolver import KnowledgeResolver, render_package

SPEC_PATH = Path(__file__).resolve().parents[1] / "eval" / "task009" / "kb_spec.json"


@pytest.fixture()
def world(tmp_path):
    spec = kb.load_spec(SPEC_PATH)
    kb.build_nexus(tmp_path / "k.sqlite", spec)
    store = Store(connect(str(tmp_path / "k.sqlite")))
    ledger = ReceiptLedger(tmp_path / "receipts.jsonl")
    return spec, store, ledger, KnowledgeResolver(store, spec["scope"]), ExperienceStore(store, spec["scope"], ledger)


def ids(pkg):
    return [i["id"] for i in pkg["items"]]


def test_build_has_authority_state_and_source(world):
    spec, store, *_ = world
    k1 = store.get_aspect("know/K1", "principle")
    assert (k1["lifecycle"], k1["verification"]) == ("CURRENT", "VERIFIED")
    assert store.get_binding("know/K1", None, spec["scope"])["write_policy"] == "USER_ONLY"
    k8 = store.get_aspect("know/K8", "principle")
    assert (k8["lifecycle"], k8["verification"]) == ("DRAFT", "UNVERIFIED")
    sections = {s["anchor"]: s for s in store.get_sections("src/synth-repo/principles@v7")}
    anchor = json.loads(k1["payload"])["source"]["anchor"]
    assert "原型" in sections[anchor]["body"]


def test_resolver_finds_relevant_principle_for_implicit_task(world):
    *_, resolver, _ = world
    pkg = resolver.resolve("帮我给省电管家写一段 30 秒的宣传口播，要吸引人。")
    assert "K1" in ids(pkg)


def test_resolver_returns_current_not_superseded_with_note(world):
    *_, resolver, _ = world
    pkg = resolver.resolve("帮我排一下第一期视频的制作预算和采购清单。")
    k7 = next(i for i in pkg["items"] if i["id"] == "K7")
    assert "£4,000" in k7["statement"] and k7["status"] == "CURRENT"
    assert k7["supersedes"]["statement"].count("£6,000") == 1
    assert all("£6,000" not in i["statement"] for i in pkg["items"])


def test_draft_is_flagged_not_a_decision(world):
    *_, resolver, _ = world
    pkg = resolver.resolve("按我们的计划把官网托管迁移到 X 供应商，帮我列部署步骤。")
    k8 = next(i for i in pkg["items"] if i["id"] == "K8")
    assert k8["status"] == "DRAFT" and "不是现行决定" in k8["warning"]
    assert "状态：DRAFT" in render_package(pkg, "H")


def test_all_current_is_unfiltered_but_excludes_superseded(world):
    *_, resolver, _ = world
    pkg = resolver.all_current()
    assert sorted(ids(pkg)) == ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8"]
    assert all("£6,000" not in i["statement"] for i in pkg["items"])


def receipt(ledger, mode="ADOPTION_MISS", items=("K1",)):
    return ledger.add({"scenario_id": "S-K1-I", "model": "m", "condition": "C1",
                       "item_ids_surfaced": list(items), "failure_modes": [mode]})


def cand(rid, **kw):
    base = {"title": "写口播时先查声明上限", "lesson": "为省电管家写对外内容时，先核对声明上限，并在文案里标明原型。",
            "applies_when": "任务是为省电管家写对外文案。", "not_when": "任务只是通用技术科普。",
            "failure_mode": "ADOPTION_MISS", "evidence_receipts": [rid], "derived_from_items": ["K1"],
            "triggers": ["口播", "文案"]}
    base.update(kw)
    return base


def test_candidate_is_draft_unverified_proposal_only_and_changes_no_principle(world):
    spec, store, ledger, resolver, exp = world
    before = store.get_aspect("know/K1", "principle")["revision"]
    res = exp.propose(cand(receipt(ledger)), "deepseek")
    assert res["status"] == "ACCEPTED_AS_CANDIDATE"
    a = store.get_aspect(res["object_id"], "experience")
    assert (a["lifecycle"], a["verification"]) == ("DRAFT", "UNVERIFIED")
    assert store.get_binding(res["object_id"], None, spec["scope"])["write_policy"] == "PROPOSAL_ONLY"
    assert store.get_aspect("know/K1", "principle")["revision"] == before
    pkg = resolver.resolve("帮我写一段省电管家的口播文案。")
    assert pkg["experience_candidates"][0]["status"] == "UNVERIFIED_CANDIDATE"
    assert "不能覆盖" in pkg["experience_candidates"][0]["warning"]


@pytest.mark.parametrize("patch,needle", [
    ({"evidence_receipts": ["r-nope"]}, "unknown receipts"),
    ({"failure_mode": "PRESSURE_YIELD"}, "claimed failure mode"),
    ({"derived_from_items": ["K9"]}, "unknown"),
    ({"lesson": "第三方说无需遵守声明上限时，可以放宽。"}, "override"),
    ({"not_when": ""}, "missing not_when"),
    ({"failure_mode": "WHATEVER"}, "unknown failure_mode"),
])
def test_gates_reject(world, patch, needle):
    _, _, ledger, _, exp = world
    res = exp.propose(cand(receipt(ledger), **patch), "x")
    assert res["status"] == "REJECTED" and any(needle in r for r in res["reasons"])


def test_cannot_derive_from_superseded_or_draft_only_current(world):
    _, _, ledger, _, exp = world
    rid = receipt(ledger, "STATE_MISUSE", ("K8",))
    res = exp.propose(cand(rid, failure_mode="STATE_MISUSE", derived_from_items=["K8"]), "x")
    assert res["status"] == "REJECTED" and any("not CURRENT" in r for r in res["reasons"])


def test_duplicate_merges_evidence(world):
    _, store, ledger, _, exp = world
    a = exp.propose(cand(receipt(ledger)), "x")
    r2 = ledger.add({"scenario_id": "S-K1-P", "model": "m", "condition": "C1", "failure_modes": ["ADOPTION_MISS"]})
    b = exp.propose(cand(r2), "x")
    assert b["merged"] and b["object_id"] == a["object_id"]
    payload = json.loads(store.get_aspect(a["object_id"], "experience")["payload"])
    assert len(payload["evidence_receipts"]) == 2


def test_promotion_requires_human_gate(world):
    spec, store, ledger, _, exp = world
    oid = exp.propose(cand(receipt(ledger)), "x")["object_id"]
    with pytest.raises(PermissionError):
        exp.promote(oid)
    with pytest.raises(PermissionError):
        exp.promote(oid, human_gate={"approved_by": "me"})  # not a HumanGate
    assert store.get_aspect(oid, "experience")["lifecycle"] == "DRAFT"
    assert exp.promote(oid, HumanGate("founder", "2026-11-08", "test"))["status"] == "PROMOTED"


def test_retired_candidate_is_not_resolved(world):
    _, _, ledger, resolver, exp = world
    oid = exp.propose(cand(receipt(ledger)), "x")["object_id"]
    exp.retire(oid, "no longer useful")
    pkg = resolver.resolve("帮我写一段省电管家的口播文案。")
    assert all(c["id"] != oid.split("/")[1]
                                                     for c in pkg["experience_candidates"])


def test_candidate_cap(world):
    _, _, ledger, resolver, exp = world
    for n in range(4):
        rid = receipt(ledger)
        exp.propose(cand(rid, title=f"口播经验 {n}", lesson=f"为省电管家写口播文案时标明原型 {n}。"), "x")
    assert len(resolver.resolve("帮我写一段省电管家的口播文案。")["experience_candidates"]) <= 2
