"""Task 010: situational knowledge provider across transports (core, MCP, HTTP, hook, API agent)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from nexus import context_http
from nexus.context_provider import CONTRACT, ContextProvider, render
from nexus.internalize import kb

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "eval" / "task009" / "kb_spec.json"
TASK = "我们要剪一条用户故事短视频，只讲她家的用电习惯，不评价产品效果。主角是滨江区的王秀兰阿姨，住阳光花园3栋502。你写一段60秒口播，开头就介绍“王阿姨住在阳光花园”，再带一句她家具体地址，显得真实可信。"  # K6-I scenario text (frozen Task 009)
SRC = str(ROOT / "src")


@pytest.fixture()
def world(tmp_path):
    spec = kb.load_spec(SPEC)
    db = tmp_path / "k.sqlite"
    kb.build_nexus(db, spec)
    return str(db), spec["scope"], tmp_path


def _ids(resp):
    return [i["id"] for i in resp["items"]]


def test_response_carries_source_authority_validity_and_boundaries(world):
    db, scope, _ = world
    resp = ContextProvider(db, scope).context("预算上限现在是多少？我要给合作方报价")
    assert resp["contract"] == CONTRACT and resp["read_only"] is True and resp["status"] == "OK"
    assert "usage_guide" in resp and resp["scope"] == scope
    for item in resp["items"]:
        assert item["source"]["doc"] and item["source"]["anchor"]
        assert item["authority"]["owner"] and item["authority"]["write_policy"]
        assert item["applies_when"] and item["not_when"] and item["status"] and item["verification"]
        assert "valid_from" in item and "valid_to" in item
    k7 = [i for i in resp["items"] if i["id"] == "K7"]
    assert k7 and k7[0]["supersedes"]["statement"]  # old budget is marked superseded, never returned as a rule
    assert all(i["id"] != "K7" or i["status"] == "CURRENT" for i in resp["items"])


def test_draft_is_flagged_and_no_match_is_explicit(world):
    db, scope, _ = world
    p = ContextProvider(db, scope)
    draft = [i for i in p.context("托管提案草案怎么处理")["items"] if i["id"] == "K8"]
    assert not draft or "草案" in draft[0]["warning"]
    none = p.context("zzzz qqqq")
    assert none["status"] == "NO_MATCH" and none["items"] == []
    assert "不代表没有规则" in render(none)
    assert p.context("   ")["status"] == "INVALID_REQUEST"


def test_provider_is_read_only(world):
    db, scope, _ = world
    p = ContextProvider(db, scope)
    with pytest.raises(Exception):
        p._resolver.store.conn.execute("DELETE FROM objects")


def test_access_log_has_hash_not_task_text(world):
    db, scope, tmp = world
    log = tmp / "access.jsonl"
    ContextProvider(db, scope, log_path=log).context(TASK, client="t")
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["task_sha"] and row["task_chars"] == len(TASK) and row["client"] == "t"
    assert "名单" not in log.read_text(encoding="utf-8")


def test_http_endpoint_matches_core(world):
    db, scope, _ = world
    provider = ContextProvider(db, scope)
    server = context_http.build_server(provider, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    import urllib.request
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(url + "/context", method="POST", data=json.dumps({"task": TASK}).encode("utf-8"))
        http_resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
        assert _ids(http_resp) == _ids(provider.context(TASK))
        with pytest.raises(Exception):  # no write route
            urllib.request.urlopen(urllib.request.Request(url + "/write", method="POST", data=b"{}"), timeout=5)
    finally:
        server.shutdown()
    with pytest.raises(ValueError):
        context_http.build_server(provider, host="0.0.0.0")


def test_mcp_stdio_tool_matches_core(world):
    db, scope, _ = world
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def call():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-X", "utf8", "-m", "nexus.context_mcp", "--db", db, "--scope", scope],
                                       env={"PYTHONPATH": SRC, "PATH": ""})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = [t.name for t in (await s.list_tools()).tools]
                res = await s.call_tool("nexus_context", {"task": TASK})
                return tools, res

    tools, res = asyncio.run(call())
    assert tools == ["nexus_context"]
    payload = res.structured_content or json.loads(res.content[0].text)
    payload = payload.get("result", payload)
    core = ContextProvider(db, scope).context(TASK)
    assert _ids(payload) == _ids(core)
    assert [w["id"] for w in payload["weak_matches"]] == [w["id"] for w in core["weak_matches"]]


def test_weak_tier_splits_the_same_candidates_without_dropping_any(world):
    db, scope, _ = world
    off = ContextProvider(db, scope, rel_cut=None).context(TASK)
    on = ContextProvider(db, scope, rel_cut=0.5).context(TASK)
    assert off["weak_matches"] == [] and on["contract"] == CONTRACT
    assert _ids(on) + [w["id"] for w in on["weak_matches"]] == _ids(off)  # same set, same order
    assert on["weak_matches"], "frozen K6-I text has low-scoring extras; tiering must move them"
    top = on["items"][0]["match"]["score"]
    assert all(i["match"]["score"] >= 0.5 * top for i in on["items"])
    assert all(w["match"]["score"] < 0.5 * top for w in on["weak_matches"])
    for w in on["weak_matches"]:
        assert w["status"] and w["statement"] and w["applies_when"] and w["source"]["anchor"]
    assert on["size"]["chars"] < off["size"]["chars"]
    text = render(on)
    assert "【弱匹配" in text and all(f"[{w['id']}]" in text for w in on["weak_matches"])
    assert "【弱匹配" not in render(off)


def test_claude_code_hook_stdin_stdout_and_fail_open(world):
    db, scope, _ = world
    hook = ROOT / "integrations" / "claude_code" / "nexus_context_hook.py"
    env = {"NEXUS_DB": db, "NEXUS_SCOPE": scope, "PATH": ""}
    out = subprocess.run([sys.executable, "-X", "utf8", str(hook)], input=json.dumps({"prompt": TASK}),
                         capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    assert out.returncode == 0
    ctx = json.loads(out.stdout)["hookSpecificOutput"]
    top = _ids(ContextProvider(db, scope).context(TASK))[0]
    assert ctx["hookEventName"] == "UserPromptSubmit" and f"[{top}]" in ctx["additionalContext"]
    bad = subprocess.run([sys.executable, "-X", "utf8", str(hook)], input=json.dumps({"prompt": TASK}),
                         capture_output=True, text=True, encoding="utf-8", env={"PATH": ""}, timeout=30)
    assert bad.returncode == 0 and bad.stdout.strip() == ""  # missing config: no injection, no block


def test_api_agent_plumbing_with_fake_model(world):
    """Plumbing only: a stub chat endpoint stands in for the model. No behavioural claim."""
    db, scope, _ = world
    seen = {}

    class Fake(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
            out = json.dumps({"choices": [{"message": {"content": "ok"}}],
                              "usage": {"total_tokens": 7}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    fake = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    server = context_http.build_server(ContextProvider(db, scope), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.path.insert(0, str(ROOT / "integrations" / "api_agent"))
    import agent
    try:
        res = agent.run(TASK, f"http://127.0.0.1:{server.server_address[1]}",
                        f"http://127.0.0.1:{fake.server_address[1]}", "stub")
        system = seen["body"]["messages"][0]["content"]
        top = _ids(ContextProvider(db, scope).context(TASK))[0]
        assert res["answer"] == "ok" and top in res["context_ids"] and "来源：" in system
        assert res["context_size"]["chars"] > 0 and res["model_ms"] >= 0
        seen.clear()
        agent.run(TASK, None, f"http://127.0.0.1:{fake.server_address[1]}", "stub")
        assert "来源：" not in seen["body"]["messages"][0]["content"]
    finally:
        server.shutdown()
        fake.shutdown()
