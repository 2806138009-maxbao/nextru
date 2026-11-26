"""Every MCP tool call, timed: ``<db>.calls.jsonl`` (one JSON line per call, reads and writes).

Server time is what Nexus itself spends; the gaps between calls are the agent (thinking, writing,
deciding what to call next). Logged per call: tool, actor, start, lock wait, run time, response size,
status, and a short argument summary (strings cut to 60 characters; document bodies, ops and
evidence counted, not copied; URLs reduced to scheme and host, so a signed download link and its
token never reach the log). Logging never breaks a tool call.

    python -m nexus.calls --db main.sqlite [--since 2026-11-10T17:00] [--gap 180]

prints the calls grouped into episodes (a pause longer than --gap seconds starts a new one): per
episode the number of calls, wall time, server time, the longest pause and the tool sequence.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

BULKY = {"children", "ops", "evidence", "anchors", "queries", "aliases"}


def log_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".calls.jsonl")


def _url_safe(value: str) -> str:
    """'https://host/path?sig=...' -> 'https://host/…'. Paths and queries can carry tokens."""
    if not value.startswith(("http://", "https://")):
        return value
    u = urllib.parse.urlsplit(value)
    return f"{u.scheme}://{u.hostname or ''}" + ("/…" if (u.path not in ("", "/") or u.query) else "")


def _short(value: str) -> str:
    value = _url_safe(value)
    return value if len(value) <= 60 else value[:60] + "…"


def summarize_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in kwargs.items():
        if hasattr(v, "model_dump"):            # pydantic argument (e.g. a chat attachment)
            v = v.model_dump(exclude_none=True)
            out[k] = {kk: _short(vv) if isinstance(vv, str) else type(vv).__name__ for kk, vv in v.items()}
            continue
        if v is None or v == "" or v == []:
            continue
        if k in BULKY or isinstance(v, (list, dict)):
            out[k] = f"<{len(v)}>" if hasattr(v, "__len__") else "<…>"
            if k == "children":
                out["chars"] = len(json.dumps(v, ensure_ascii=False))
        elif isinstance(v, str):
            out[k] = _short(v)
        elif isinstance(v, (int, float, bool)):
            out[k] = v
        else:                                   # never log an object's repr: it can hold anything
            out[k] = f"<{type(v).__name__}>"
    return out


def result_size(out: Any) -> int:
    try:
        return len(out) if isinstance(out, str) else len(json.dumps(out, ensure_ascii=False, default=str))
    except Exception:
        return -1


def log_call(db_path: str | Path, record: dict) -> None:
    try:
        with open(log_path(db_path), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass                                   # the log must never break a tool call


def _ts(s: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def episodes(records: list[dict], gap_s: float = 180) -> list[list[dict]]:
    out: list[list[dict]] = []
    last = None
    for r in sorted(records, key=lambda r: r["at"]):
        t = _ts(r["at"])
        if last is None or (t - last).total_seconds() > gap_s or (out and out[-1][-1].get("actor") != r.get("actor")):
            out.append([])
        out[-1].append(r)
        last = t
    return out


def report(records: list[dict], gap_s: float = 180) -> str:
    lines = []
    for ep in episodes(records, gap_s):
        t0, t1 = _ts(ep[0]["at"]), _ts(ep[-1]["at"])
        server = sum(r.get("elapsed_ms", 0) + r.get("lock_wait_ms", 0) for r in ep) / 1000
        pauses = [(_ts(b["at"]) - _ts(a["at"])).total_seconds() for a, b in zip(ep, ep[1:])]
        wall = (t1 - t0).total_seconds()
        lines.append(f"{ep[0]['at'][:19]}  {ep[0].get('actor')}  {len(ep)} calls  wall {wall:.0f}s  "
                     f"server {server:.1f}s  longest pause {max(pauses, default=0):.0f}s")
        for a, r in zip([None] + ep[:-1], ep):
            gap = (_ts(r["at"]) - _ts(a["at"])).total_seconds() if a else 0
            args = ", ".join(f"{k}={v}" for k, v in (r.get("args") or {}).items())
            lines.append(f"    +{gap:5.0f}s  {r['tool']:<24} {r.get('elapsed_ms', 0):7.1f} ms  "
                         f"{r.get('result_chars', 0):6d} ch  {r.get('status') or ''}  {args}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nexus.calls")
    ap.add_argument("--db", required=True)
    ap.add_argument("--since", default="")
    ap.add_argument("--gap", type=float, default=180, help="seconds of silence that end an episode")
    a = ap.parse_args(argv)
    p = log_path(a.db)
    if not p.exists():
        print(f"no call log yet at {p}")
        return 0
    recs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("at", "") >= a.since:
            recs.append(r)
    print(report(recs, a.gap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
