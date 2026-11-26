"""Loopback JSON endpoint for API agents (stdlib only).

    python -m nexus.context_http --db var/t009/lumen.sqlite --scope research:lumen-synth --port 8791
    POST /context  {"task": "...", "client": "my-agent", "max_items": 3}
    GET  /health

Loopback only. Read-only: there is no write route. Not a production service.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .context_provider import CONTRACT, ContextProvider


def build_server(provider: ContextProvider, host: str = "127.0.0.1", port: int = 8791) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("context endpoint binds to loopback only")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in {"/health", "/"}:
                self._send(200, {"contract": CONTRACT, "scope": provider.scope, "read_only": True})
            else:
                self._send(404, {"error": "GET /health or POST /context"})

        def do_POST(self):  # noqa: N802
            if self.path != "/context":
                return self._send(404, {"error": "POST /context only"})
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"contract": CONTRACT, "status": "INVALID_REQUEST", "error": "bad JSON"})
            resp = provider.context(req.get("task", ""), client=str(req.get("client", "http")),
                                    max_items=req.get("max_items"))
            self._send(400 if resp.get("status") == "INVALID_REQUEST" else 200, resp)

        def log_message(self, *a):  # quiet
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.context_http")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--port", type=int, default=8791)
    p.add_argument("--log")
    a = p.parse_args(argv)
    server = build_server(ContextProvider(a.db, a.scope, log_path=a.log), port=a.port)
    print(f"nexus-context listening on http://127.0.0.1:{a.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
