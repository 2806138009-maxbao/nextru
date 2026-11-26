"""Local, loopback-only JSON HTTP API for the Nexus P1 prototype.

This is a **test harness**, not a production-secure service. The default
``/query`` surface exposes only side-effect-free operations. State-changing test
operations are available only on ``/test/query`` when the server is explicitly
started with ``--enable-test-api``, and the caller principal is injected by the
server (never accepted from the client).

Uses only the Python standard library so the prototype runs without network
access to a package index. FastAPI/Pydantic were the task-book candidates and
remain a drop-in option (see docs/implementation-plan.md), but were NOT
installed in this environment (no package-index reachability), so no FastAPI
claim is made.

Run:
    python -m nexus.http_api --db var/nexus.db --port 8765
    curl -s http://127.0.0.1:8765/health
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .core import NexusCore
from .db import Store, connect
from .errors import Status
from .service import dispatch, dispatch_test


def make_handler(core: NexusCore, allow_test_writes: bool = False,
                 test_principal: str = "system:test-harness"):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "NexusP1/0.1"

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in {"/health", "/"}:
                self._send(200, dispatch(core, "health"))
            else:
                self._send(404, {"error": {"code": "NOT_FOUND",
                                           "message": "only GET /health is defined"}})

        def do_POST(self):  # noqa: N802
            if self.path not in {"/query", "/test/query"}:
                self._send(404, {"error": {"code": "NOT_FOUND",
                                           "message": "POST /query or /test/query only"}})
                return
            if self.path == "/test/query" and not allow_test_writes:
                self._send(403, {
                    "contract_version": "nexus-core-p1/0.1",
                    "execution": {"status": Status.AUTHORITY_DENIED},
                    "error": {"code": Status.AUTHORITY_DENIED,
                              "message": "test interface is disabled; start with"
                                         " --enable-test-api on loopback only"},
                })
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self._send(400, {"error": {"code": "INVALID_JSON",
                                           "message": str(exc)}})
                return
            operation = payload.get("operation", "")
            params = payload.get("params") or {}
            with lock:
                if self.path == "/test/query":
                    result = dispatch_test(core, operation, params, test_principal)
                else:
                    result = dispatch(core, operation, params)
            # Domain statuses are carried in the body (query status != HTTP
            # transport status); transport errors only for malformed requests.
            self._send(200, result)

        def log_message(self, fmt: str, *args):  # keep test output quiet
            if self.server.verbose:  # type: ignore[attr-defined]
                super().log_message(fmt, *args)

    return Handler


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8765,
                 verbose: bool = False, allow_test_writes: bool = False,
                 test_principal: str = "system:test-harness") -> ThreadingHTTPServer:
    if allow_test_writes and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("test-write API may only bind to loopback")
    conn = connect(db_path, check_same_thread=False)
    core = NexusCore(Store(conn))
    server = ThreadingHTTPServer(
        (host, port), make_handler(core, allow_test_writes, test_principal))
    server.daemon_threads = True
    server.allow_reuse_address = True
    server.verbose = verbose  # type: ignore[attr-defined]
    server.allow_test_writes = allow_test_writes  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus P1 local read-only API")
    parser.add_argument("--db", default=str(Path("var") / "nexus.db"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--enable-test-api", action="store_true",
                        help="enable /test/query state-changing test operations"
                             " (loopback only)")
    args = parser.parse_args()
    server = build_server(args.db, args.host, args.port, args.verbose,
                          allow_test_writes=args.enable_test_api)
    host, port = server.server_address[:2]
    print(f"Nexus P1 API listening on http://{host}:{port} (db={args.db})")
    if args.enable_test_api:
        print("WARNING: /test/query enabled for local tests only;"
              " this is not a production-safe service")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
