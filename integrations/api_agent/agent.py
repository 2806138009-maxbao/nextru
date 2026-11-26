"""Minimal independent API agent: fetch Nexus context over HTTP, call any OpenAI-compatible chat API.

No agent framework, no runtime, standard library only. The model endpoint is configuration:

    NEXUS_CONTEXT_URL   http://127.0.0.1:8791
    AGENT_BASE_URL      e.g. http://127.0.0.1:1234/v1  (any OpenAI-compatible /chat/completions)
    AGENT_MODEL         model id
    AGENT_API_KEY       optional bearer token (read from the environment only, never printed)

    python integrations/api_agent/agent.py --task "..." [--no-context] [--dry-run]

--dry-run prints the exact request it would send. The result dict carries per-phase timing and the
provider's package size, so clients can be compared like for like.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from nexus.context_provider import render  # noqa: E402

SYSTEM = "你是工作室创始人的项目协作助手。请完成用户任务，直接给出给用户的完整回复。"


def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def build_messages(task: str, ctx_url: str | None) -> tuple[list[dict], dict | None, float]:
    ctx, ms = None, 0.0
    system = SYSTEM
    if ctx_url:
        t0 = time.perf_counter()
        ctx = _post(ctx_url.rstrip("/") + "/context", {"task": task, "client": "api-agent"},
                    {"Content-Type": "application/json"}, 10)
        ms = (time.perf_counter() - t0) * 1000
        system = SYSTEM + "\n\n" + render(ctx)
    return [{"role": "system", "content": system}, {"role": "user", "content": task}], ctx, ms


def run(task: str, ctx_url: str | None, base_url: str, model: str, api_key: str | None = None,
        dry_run: bool = False) -> dict:
    messages, ctx, ctx_ms = build_messages(task, ctx_url)
    body = {"model": model, "messages": messages, "temperature": 0}
    out = {"context_ms": round(ctx_ms, 1), "context_ids": [i["id"] for i in (ctx or {}).get("items", [])],
           "context_size": (ctx or {}).get("size"), "request": body if dry_run else None}
    if dry_run:
        return out
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    t0 = time.perf_counter()
    resp = _post(base_url.rstrip("/") + "/chat/completions", body, headers, 300)
    out.update(model_ms=round((time.perf_counter() - t0) * 1000, 1),
               answer=resp["choices"][0]["message"]["content"], usage=resp.get("usage"))
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--no-context", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    ctx_url = None if a.no_context else os.environ["NEXUS_CONTEXT_URL"]
    res = run(a.task, ctx_url, os.environ.get("AGENT_BASE_URL", ""), os.environ.get("AGENT_MODEL", ""),
              os.environ.get("AGENT_API_KEY"), a.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
