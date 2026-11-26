"""Phase-1 file-transfer probe: a tiny, self-contained MCP server (not Nexus).

It answers three questions about an MCP client such as ChatGPT, with evidence:

  1. Image return  - does the client accept an image a tool returns, and does the *model* see it?
     `probe_image` draws a fresh random nonce into a PNG. Only the server knows the nonce; if the
     model reads it back correctly, the image reached its vision input. Three encodings are tried:
     MCP ImageContent, an EmbeddedResource blob, and a ResourceLink / plain URL.
  2. File upload   - can the user hand a chat file to a tool? `probe_receive_file` declares
     `_meta["openai/fileParams"]` (ChatGPT Apps SDK) and accepts {download_url, file_id, ...};
     the server downloads it and records size / SHA-256 / sniffed type.
  3. Fallback      - `probe_receive_base64`: the model passes bytes inline (works for small files
     on any client; measures where it stops).

Every call is appended to <evidence-dir>/calls.jsonl: argument *shape* (keys, lengths, URL host),
never file contents or signed URL query strings. Received files go to <evidence-dir>/received/
(keep that directory out of Git).

    python experiments/file_probe/probe_server.py --port 8790 --evidence-dir var/probe
    python experiments/file_probe/probe_server.py --port 8790 --public-base https://x.trycloudflare.com

Synthetic data only: the probe never reads the Nexus database.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import ipaddress
import json
import secrets
import socket
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import (BlobResourceContents, EmbeddedResource, ImageContent, ResourceLink,
                       TextContent)
from pydantic import BaseModel, ConfigDict

MAX_DOWNLOAD = 50 * 1024 * 1024
NONCE_ALPHABET = "ACDEFHJKMNPRTUVWXY34679"   # no 0/O, 1/I/L, 2/Z, 5/S, 8/B look-alikes
SHAPES = [("red", (220, 40, 40)), ("blue", (40, 80, 220)), ("green", (30, 160, 60)),
          ("orange", (240, 140, 20)), ("purple", (140, 60, 180))]


class FileRef(BaseModel):
    """What ChatGPT is documented to pass for a file param; anything extra is kept and logged."""
    model_config = ConfigDict(extra="allow")
    download_url: str | None = None
    file_id: str | None = None
    name: str | None = None
    mime_type: str | None = None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sniff(data: bytes) -> str:
    sigs = [(b"%PDF-", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff", "image/jpeg"), (b"GIF8", "image/gif"), (b"PK\x03\x04", "application/zip"),
            (b"RIFF", "riff (webp/wav/avi)")]
    for sig, kind in sigs:
        if data.startswith(sig):
            return kind
    try:
        data[:4096].decode("utf-8")
        return "text (utf-8)"
    except UnicodeDecodeError:
        return "application/octet-stream"


def url_shape(url: str | None) -> dict | None:
    if not url:
        return None
    u = urllib.parse.urlsplit(url)
    return {"scheme": u.scheme, "host": u.hostname, "path_len": len(u.path), "has_query": bool(u.query),
            "length": len(url)}


def shape_of(value: Any) -> Any:
    """Argument shape for the log: keys, types, lengths; URLs reduced to scheme/host."""
    if isinstance(value, dict):
        return {k: (url_shape(v) if isinstance(v, str) and v.startswith(("http://", "https://"))
                    else shape_of(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [shape_of(v) for v in value[:5]] + (["..."] if len(value) > 5 else [])
    if isinstance(value, str):
        return {"str_len": len(value)} if len(value) > 80 else value
    return value


class Probe:
    def __init__(self, evidence_dir: Path, public_base: str | None, allow_private_download: bool):
        self.dir = evidence_dir
        self.received = evidence_dir / "received"
        self.received.mkdir(parents=True, exist_ok=True)
        self.public_base = public_base.rstrip("/") if public_base else None
        self.allow_private = allow_private_download
        self.images: dict[str, bytes] = {}      # token -> png, served at /probe/img/<token>.png
        self.lock = threading.Lock()

    def log(self, rec: dict) -> None:
        rec = {"at": now(), **rec}
        with self.lock, open(self.dir / "calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ---- image -------------------------------------------------------------------------------
    def make_image(self) -> tuple[bytes, dict]:
        from PIL import Image, ImageDraw, ImageFont
        nonce = "".join(secrets.choice(NONCE_ALPHABET) for _ in range(6))
        name, rgb = SHAPES[secrets.randbelow(len(SHAPES))]
        img = Image.new("RGB", (640, 360), (250, 250, 245))
        d = ImageDraw.Draw(img)
        d.ellipse((30, 30, 150, 150), fill=rgb)
        d.text((180, 120), nonce, fill=(20, 20, 20), font=ImageFont.load_default(size=96))
        d.text((180, 260), "nexus file probe", fill=(110, 110, 110), font=ImageFont.load_default(size=28))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png = buf.getvalue()
        return png, {"nonce": nonce, "circle_color": name, "bytes": len(png),
                     "sha256": hashlib.sha256(png).hexdigest()}

    def image_result(self, variant: str) -> list:
        png, truth = self.make_image()
        token = secrets.token_urlsafe(12)
        self.images[token] = png
        url = f"{self.public_base}/probe/img/{token}.png" if self.public_base else None
        self.log({"tool": "probe_image", "variant": variant, "truth": truth, "url_served": bool(url),
                  "token": token})
        note = TextContent(type="text", text=(
            f"Probe image ({variant}). It contains a 6-character code and a coloured circle. "
            "Describe what you see: the exact code and the circle colour. Do not guess: if you "
            "cannot see the image, say so."))
        b64 = base64.b64encode(png).decode()
        if variant == "image_content":
            return [note, ImageContent(type="image", data=b64, mime_type="image/png")]
        if variant == "embedded_resource":
            return [note, EmbeddedResource(type="resource", resource=BlobResourceContents(
                uri=f"probe://img/{token}.png", mime_type="image/png", blob=b64))]
        if not url:
            return [TextContent(type="text", text="resource_link/text_url need --public-base; not run.")]
        if variant == "resource_link":
            return [note, ResourceLink(type="resource_link", uri=url, name=f"{token}.png", mime_type="image/png")]
        return [note, TextContent(type="text", text=f"Image URL: {url}")]

    # ---- file intake -------------------------------------------------------------------------
    def store(self, data: bytes, how: str, declared: dict) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        (self.received / digest).write_bytes(data)
        out = {"status": "OK", "how": how, "bytes": len(data), "sha256": digest, "sniffed": sniff(data),
               "declared": declared}
        if out["sniffed"] == "application/pdf":
            out["pdf"] = pdf_facts(data)
        return out

    def check_host(self, url: str) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" and not self.allow_private:
            raise ValueError(f"only https download_url accepted, got {u.scheme}")
        if self.allow_private:
            return
        for info in socket.getaddrinfo(u.hostname, u.port or 443):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("download_url resolves to a private address; refused")

    def download(self, url: str) -> bytes:
        self.check_host(url)
        req = urllib.request.Request(url, headers={"User-Agent": "nexus-file-probe/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError(f"file larger than {MAX_DOWNLOAD} bytes; refused")
        return data


def pdf_facts(data: bytes) -> dict:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return {"status": "NOT_RUN", "reason": "PyMuPDF not installed"}
    with fitz.open(stream=data, filetype="pdf") as doc:
        first = doc[0].get_text()[:200] if doc.page_count else ""
        return {"pages": doc.page_count, "page1_text_chars": len(doc[0].get_text()) if doc.page_count else 0,
                "page1_head": first}


def build(probe: Probe) -> MCPServer:
    server = MCPServer(name="nexus-file-probe", instructions=(
        "Diagnostic server for file transfer. Use probe_image to fetch a test image and report the "
        "code you read in it. Use probe_receive_file when the user attaches a file. Use "
        "probe_receive_base64 only for small files when probe_receive_file is unavailable."))

    @server.custom_route("/health", methods=["GET"])
    async def health(request):
        from starlette.responses import JSONResponse
        return JSONResponse({"status": "OK", "server": "nexus-file-probe"})

    @server.custom_route("/probe/img/{name}", methods=["GET"])
    async def image(request):
        from starlette.responses import Response
        token = request.path_params["name"].removesuffix(".png")
        png = probe.images.get(token)
        probe.log({"http": "GET image", "token": token, "found": png is not None,
                   "user_agent": request.headers.get("user-agent", "")[:120]})
        if png is None:
            return Response(status_code=404)
        return Response(png, media_type="image/png")

    @server.tool(name="probe_image", description=(
        "Return a freshly generated test image containing a random 6-character code and a coloured "
        "circle. variant: image_content (MCP image block), embedded_resource (blob resource), "
        "resource_link (link to an HTTPS URL), text_url (URL as plain text). After calling, report "
        "the exact code and circle colour you see; if you cannot see an image, say so."),
        structured_output=False)
    def probe_image(variant: Literal["image_content", "embedded_resource", "resource_link",
                                     "text_url"] = "image_content") -> list:
        return probe.image_result(variant)

    @server.tool(name="probe_check_answer", description=(
        "Check the code you read from the last probe_image call. Pass exactly what you saw."))
    def probe_check_answer(code: str, circle_color: str = "") -> dict:
        last = None
        for line in (probe.dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()[::-1]:
            rec = json.loads(line)
            if rec.get("tool") == "probe_image":
                last = rec
                break
        if last is None:
            return {"status": "NO_IMAGE_YET"}
        truth = last["truth"]
        out = {"variant": last["variant"], "code_correct": code.strip().upper() == truth["nonce"],
               "color_correct": circle_color.strip().lower() == truth["circle_color"] if circle_color else None}
        probe.log({"tool": "probe_check_answer", "answer": {"code": code, "circle_color": circle_color},
                   "truth": truth, **out})
        return out

    @server.tool(name="probe_receive_file", description=(
        "Receive a file the user attached in the chat. Pass the file itself as `file`."),
        meta={"openai/fileParams": ["file"]})
    def probe_receive_file(file: FileRef | str) -> dict:
        raw = file.model_dump() if isinstance(file, FileRef) else file
        rec = {"tool": "probe_receive_file", "arg_type": type(file).__name__, "arg_shape": shape_of(raw)}
        try:
            url = file.download_url if isinstance(file, FileRef) else file
            if not url or not str(url).startswith(("http://", "https://")):
                out = {"status": "NO_DOWNLOAD_URL", "received": shape_of(raw)}
            else:
                declared = {k: v for k, v in (raw.items() if isinstance(raw, dict) else [])
                            if k in ("name", "mime_type", "file_id")}
                out = probe.store(probe.download(url), "download_url", declared)
        except Exception as e:  # report, don't crash: the failure mode is the evidence
            out = {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}
        probe.log({**rec, "result": {k: v for k, v in out.items() if k != "pdf"} | (
            {"pdf_pages": out["pdf"].get("pages")} if "pdf" in out else {})})
        return out

    @server.tool(name="probe_receive_base64", description=(
        "Fallback: receive a small file as base64 text. Give the filename and, if you know it, the "
        "SHA-256 of the original bytes."))
    def probe_receive_base64(filename: str, data_base64: str, sha256: str = "") -> dict:
        rec = {"tool": "probe_receive_base64", "filename": filename, "b64_chars": len(data_base64)}
        try:
            data = base64.b64decode(data_base64, validate=True)
            out = probe.store(data, "base64", {"name": filename})
            if sha256:
                out["sha256_match"] = sha256.lower() == out["sha256"]
        except Exception as e:
            out = {"status": "ERROR", "error": f"{type(e).__name__}: {e}"}
        probe.log({**rec, "result": {k: v for k, v in out.items() if k != "pdf"}})
        return out

    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--evidence-dir", default="var/probe")
    p.add_argument("--public-base", help="public https origin this server is reachable at (for URL variants)")
    p.add_argument("--allow-private-download", action="store_true",
                   help="self-test only: allow http and loopback download_url")
    p.add_argument("--allowed-host", action="append", default=[],
                   help="extra Host header to accept (a tunnel hostname); DNS-rebinding protection stays on")
    a = p.parse_args(argv)
    probe = Probe(Path(a.evidence_dir), a.public_base, a.allow_private_download)
    hosts = [h for h in a.allowed_host]
    if a.public_base:
        hosts.append(urllib.parse.urlsplit(a.public_base).netloc)
    security = None
    if hosts:  # the SDK's loopback default accepts only localhost Host headers, which blocks a tunnel
        from mcp.server.transport_security import TransportSecuritySettings
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *hosts],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", *(f"https://{h}" for h in hosts)])
    build(probe).run("streamable-http", host=a.host, port=a.port, transport_security=security)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
