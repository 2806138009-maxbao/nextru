"""Local self-test of probe_server with a real MCP client (no ChatGPT, no network beyond loopback).

Proves the *server* side of every path: tool listing (incl. openai/fileParams meta), each image
encoding, file intake through a ChatGPT-shaped {download_url, file_id} object, base64 fallback.
It cannot prove what ChatGPT does with them; that is the manual run in RUNBOOK.md.

    python experiments/file_probe/selftest.py --out experiments/file_probe/evidence/selftest.json
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.server
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_pdf(path: Path) -> None:
    import fitz
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Probe PDF page {i + 1}: resistor R{i + 1} = {(i + 1) * 470} ohm")
    doc.save(path)


def content_summary(result) -> list[dict]:
    out = []
    for c in result.content:
        d = {"type": c.type}
        if c.type == "text":
            d["text"] = c.text[:160]
        elif c.type == "image":
            d.update(mimeType=c.mime_type, b64_chars=len(c.data))
        elif c.type == "resource":
            d.update(uri=str(c.resource.uri), mimeType=c.resource.mime_type, b64_chars=len(c.resource.blob))
        elif c.type == "resource_link":
            d.update(uri=str(c.uri), mimeType=c.mime_type)
        out.append(d)
    return out


async def run(url: str, file_url: str, pdf_bytes: bytes, small: bytes) -> dict:
    report: dict = {"checks": []}

    def check(name: str, ok: bool, detail) -> None:
        report["checks"].append({"name": name, "pass": bool(ok), "detail": detail})

    async with streamable_http_client(url) as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            meta = tools["probe_receive_file"].meta or {}
            check("tools listed", set(tools) >= {"probe_image", "probe_receive_file",
                                                 "probe_receive_base64", "probe_check_answer"}, sorted(tools))
            check("fileParams meta advertised", meta.get("openai/fileParams") == ["file"], meta)
            check("file param schema is object|string",
                  "file" in tools["probe_receive_file"].input_schema.get("properties", {}),
                  tools["probe_receive_file"].input_schema)

            for variant, want in [("image_content", "image"), ("embedded_resource", "resource"),
                                  ("resource_link", "resource_link"), ("text_url", "text")]:
                res = await s.call_tool("probe_image", {"variant": variant})
                summ = content_summary(res)
                check(f"probe_image[{variant}] returns {want}", any(c["type"] == want for c in summ)
                      and not res.is_error, summ)

            res = await s.call_tool("probe_receive_file", {"file": {
                "download_url": file_url, "file_id": "file-selftest", "name": "probe.pdf",
                "mime_type": "application/pdf"}})
            body = json.loads(res.content[0].text)
            check("receive_file via download_url: hash matches",
                  body.get("sha256") == hashlib.sha256(pdf_bytes).hexdigest(), body)
            check("receive_file: PDF parsed (3 pages)", body.get("pdf", {}).get("pages") == 3, body.get("pdf"))

            res = await s.call_tool("probe_receive_file", {"file": {"file_id": "file-no-url"}})
            check("receive_file without URL reports NO_DOWNLOAD_URL",
                  json.loads(res.content[0].text).get("status") == "NO_DOWNLOAD_URL", res.content[0].text[:200])

            b64 = base64.b64encode(small).decode()
            res = await s.call_tool("probe_receive_base64", {
                "filename": "small.bin", "data_base64": b64, "sha256": hashlib.sha256(small).hexdigest()})
            body = json.loads(res.content[0].text)
            check("receive_base64: sha256_match", body.get("sha256_match") is True, body)
    report["passed"] = sum(c["pass"] for c in report["checks"])
    report["total"] = len(report["checks"])
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "evidence" / "selftest.json"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="probe-selftest-"))
    pdf = tmp / "files" / "probe.pdf"
    pdf.parent.mkdir()
    make_pdf(pdf)
    small = bytes(range(256)) * 16

    # loopback file server standing in for ChatGPT's download_url host
    fport = free_port()
    handler = lambda *x, **k: http.server.SimpleHTTPRequestHandler(*x, directory=str(pdf.parent), **k)  # noqa: E731
    fsrv = http.server.ThreadingHTTPServer(("127.0.0.1", fport), handler)
    threading.Thread(target=fsrv.serve_forever, daemon=True).start()

    port = free_port()
    proc = subprocess.Popen([sys.executable, str(HERE / "probe_server.py"), "--port", str(port),
                             "--evidence-dir", str(tmp / "evidence"), "--allow-private-download",
                             "--public-base", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        report = asyncio.run(run(f"http://127.0.0.1:{port}/mcp", f"http://127.0.0.1:{fport}/probe.pdf",
                                 pdf.read_bytes(), small))
        import urllib.request  # the resource_link URL must actually serve the PNG
        calls = [json.loads(l) for l in (tmp / "evidence" / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        token = next(c["token"] for c in calls if c.get("variant") == "resource_link")
        png = urllib.request.urlopen(f"http://127.0.0.1:{port}/probe/img/{token}.png").read()
        ok = png.startswith(b"\x89PNG")
        report["checks"].append({"name": "resource_link URL serves the PNG", "pass": ok, "detail": len(png)})
        report["passed"] += ok
        report["total"] += 1
        report["evidence_log_records"] = len(calls)
    finally:
        proc.terminate()
        proc.wait(10)
        fsrv.shutdown()
    report["python"] = sys.version.split()[0]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "total": report["total"]}))
    for c in report["checks"]:
        print(("PASS " if c["pass"] else "FAIL ") + c["name"])
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
