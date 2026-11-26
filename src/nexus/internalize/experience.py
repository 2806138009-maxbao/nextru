"""Controlled experience candidates from execution feedback.

Feedback enters as receipts (what was surfaced, how the run was judged). A proposer (any
model) may suggest candidate lessons; this module only *gates and stores* them. Hard rules:

* a candidate must cite receipts that exist in the ledger, and at least one of them must show
  the failure mode it claims to address;
* it must state when it applies and when it does not;
* it can only derive from knowledge records that exist and are CURRENT;
* it is stored lifecycle=DRAFT, verification=UNVERIFIED, write_policy=PROPOSAL_ONLY. It never
  creates or changes a principle or an authority binding;
* it cannot be promoted without a HumanGate object; there is no code path that supplies one;
* a lesson that tries to cancel, waive or override a principle is rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..db import Store, utcnow

FAILURE_MODES = {"SURFACE_MISS", "ADOPTION_MISS", "OVER_APPLICATION", "PRESSURE_YIELD", "STATE_MISUSE"}
OVERRIDE_PATTERNS = re.compile(r"(无需遵守|不再适用|取消.{0,6}(原则|规定)|覆盖.{0,6}(原则|规定)|可以忽略.{0,6}(原则|规定)|废除)")
MAX_LESSON = 400


@dataclass(frozen=True)
class HumanGate:
    approved_by: str
    approved_at: str
    reference: str


class ReceiptLedger:
    """Append-only JSONL of run receipts (the evidence a candidate must cite)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, receipt: dict) -> str:
        rid = receipt.get("receipt_id") or "r-" + hashlib.sha256(
            json.dumps(receipt, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
        receipt = {**receipt, "receipt_id": rid}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        return rid

    def all(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        out = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["receipt_id"]] = r
        return out


class ExperienceStore:
    def __init__(self, store: Store, scope: str, ledger: ReceiptLedger):
        self.store, self.scope, self.ledger = store, scope, ledger

    def _knowledge_state(self, item_id: str) -> str | None:
        a = self.store.get_aspect(f"know/{item_id}", "principle")
        return a["lifecycle"] if a else None

    def propose(self, cand: dict[str, Any], proposer: str) -> dict[str, Any]:
        reasons: list[str] = []
        for field in ("title", "lesson", "applies_when", "not_when", "failure_mode",
                      "evidence_receipts", "derived_from_items"):
            if not cand.get(field):
                reasons.append(f"missing {field}")
        if reasons:
            return {"status": "REJECTED", "reasons": reasons}
        if cand["failure_mode"] not in FAILURE_MODES:
            reasons.append(f"unknown failure_mode {cand['failure_mode']!r}")
        if len(cand["lesson"]) > MAX_LESSON:
            reasons.append("lesson too long")
        if OVERRIDE_PATTERNS.search(cand["lesson"]) or OVERRIDE_PATTERNS.search(cand["not_when"]):
            reasons.append("lesson tries to cancel, waive or override a principle")
        ledger = self.ledger.all()
        missing = [r for r in cand["evidence_receipts"] if r not in ledger]
        if missing:
            reasons.append(f"unknown receipts {missing}")
        else:
            modes = {m for r in cand["evidence_receipts"] for m in ledger[r].get("failure_modes", [])}
            if cand["failure_mode"] not in modes:
                reasons.append("no cited receipt shows the claimed failure mode")
        for item in cand["derived_from_items"]:
            state = self._knowledge_state(item)
            if state != "CURRENT":
                reasons.append(f"derived_from {item} is {state or 'unknown'}, not CURRENT")
        if reasons:
            return {"status": "REJECTED", "reasons": reasons}

        key = hashlib.sha256(json.dumps([cand["failure_mode"], sorted(cand["derived_from_items"]),
                                         re.sub(r"\s+", "", cand["title"])],
                                        ensure_ascii=False).encode()).hexdigest()[:8]
        oid = f"exp/E-{key}"
        existing = self.store.get_object(oid)
        payload = {"id": f"E-{key}", "kind": "EXPERIENCE", "title": cand["title"],
                   "statement": cand["lesson"], "applies_when": cand["applies_when"],
                   "not_when": cand["not_when"], "why": f"来自 {len(cand['evidence_receipts'])} 条运行回执",
                   "triggers": cand.get("triggers", []), "failure_mode": cand["failure_mode"],
                   "derived_from_items": cand["derived_from_items"],
                   "evidence_receipts": sorted(set(cand["evidence_receipts"])), "proposed_by": proposer,
                   "source": {"doc": "receipt-ledger", "anchor": ",".join(sorted(set(cand["evidence_receipts"]))[:3])}}
        with self.store.tx():
            if existing is None:
                self.store.add_object(oid, "experience", cand["title"], self.scope)
                rev = 1
            else:
                prev = self.store.get_aspect(oid, "experience")
                merged = sorted(set(_json(prev)["evidence_receipts"]) | set(payload["evidence_receipts"]))
                payload["evidence_receipts"] = merged
                rev = prev["revision"] + 1
                self.store.conn.execute("UPDATE aspects SET is_current = 0 WHERE object_id = ? AND name = ?",
                                        (oid, "experience"))
            self.store.add_aspect(f"{oid}#experience#{rev}", oid, "experience", payload, "t009.v1",
                                  "DRAFT", "UNVERIFIED", rev, True, None, None, None)
            if existing is None:
                self.store.add_authority(f"auth/{oid}", "object", oid, None, "founder", self.scope,
                                         "NEXUS:experience-candidates", utcnow(), "PROPOSAL_ONLY", "NATIVE")
        return {"status": "ACCEPTED_AS_CANDIDATE", "object_id": oid, "merged": existing is not None}

    def promote(self, object_id: str, human_gate: HumanGate | None = None) -> dict[str, Any]:
        if not isinstance(human_gate, HumanGate):
            raise PermissionError("promotion needs an explicit HumanGate; candidates stay UNVERIFIED")
        prev = self.store.get_aspect(object_id, "experience")
        if prev is None:
            raise KeyError(object_id)
        payload = _json(prev)
        payload["human_gate"] = {"approved_by": human_gate.approved_by, "approved_at": human_gate.approved_at,
                                 "reference": human_gate.reference}
        with self.store.tx():
            self.store.conn.execute("UPDATE aspects SET is_current = 0 WHERE object_id = ? AND name = ?",
                                    (object_id, "experience"))
            self.store.add_aspect(f"{object_id}#experience#{prev['revision'] + 1}", object_id, "experience",
                                  payload, "t009.v1", "CURRENT", "VERIFIED", prev["revision"] + 1, True,
                                  utcnow(), None, None)
        return {"status": "PROMOTED", "object_id": object_id}

    def retire(self, object_id: str, reason: str) -> None:
        prev = self.store.get_aspect(object_id, "experience")
        if prev is None:
            raise KeyError(object_id)
        payload = {**_json(prev), "retired_reason": reason}
        with self.store.tx():
            self.store.conn.execute("UPDATE aspects SET is_current = 0 WHERE object_id = ? AND name = ?",
                                    (object_id, "experience"))
            self.store.add_aspect(f"{object_id}#experience#{prev['revision'] + 1}", object_id, "experience",
                                  payload, "t009.v1", "HISTORICAL", "UNVERIFIED", prev["revision"] + 1, True,
                                  None, utcnow(), None)


def _json(aspect: dict) -> dict:
    p = aspect["payload"]
    return json.loads(p) if isinstance(p, str) else p
