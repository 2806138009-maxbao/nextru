"""Nexus MCP server over one DB: read tools, plus write/undo tools with --agent-name.

    python -m nexus.mcp_server --db var/t008/corpus.sqlite --scope research:creative-pilot
    python -m nexus.mcp_server --db ... --scope ... --transport http --port 8765

HTTP (Streamable HTTP at /mcp, health at /health) binds to loopback only: there is no
authentication yet, so remote use goes through an SSH tunnel (ssh -L). A non-loopback --host is
refused.

The scope is fixed by whoever starts the server, so an agent never has to guess
it (Task 006 runners spent many calls probing scopes). Every tool call goes
through ``service.dispatch``: read-only, scope-checked, path-redacted. stdio
transport also removes the shell-quoting and console-encoding failures seen with
the hand-written HTTP clients.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import functools
import time
import ipaddress
import json
import os
import re
import sqlite3
import threading
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict

from .agent_tools import INSTRUCTIONS, TOOL_DESCRIPTIONS, WRITE_INSTRUCTIONS, AgentTools
from .calls import log_call, result_size, summarize_args
from .compact import READ_NOTE, SEARCH_NOTE, compact_read, compact_search
from .core import NexusCore
from .db import Store, connect, init_schema


MAX_BASE64_CHARS = 2_000_000   # ~1.5 MB of bytes; larger files go through nexus_file_put

FILE_DESCRIPTIONS = {
    "nexus_file_info": (
        "What a stored file is (object_id file/...): name, type, size, pages, SHA-256, every source it "
        "came from (Drive id, chat upload), and its relations. Its extracted text (PDF pages, text "
        "files) is already in nexus_search and nexus_read: anchors h<n>.* are on page n."),
    "nexus_file_view": (
        "See a stored file: returns one PDF page (page is 1-based) or the image itself as an image "
        "you can look at. Use it for figures, tables, handwriting, scans and photos, which have no "
        "text, or to check text against the page. max_px caps the long side (default 1600)."),
    "nexus_file_put": (
        "Store a file the user attached in this chat (any type) in Nexus. Pass the attachment as "
        "`file`. The original is kept unchanged under its SHA-256; PDF and text become searchable "
        "page by page; images and other files are kept with their metadata and can be viewed with "
        "nexus_file_view. Returns the object_id and suggested_links: link the file to what it "
        "belongs with using nexus_add_relation."),
    "nexus_file_put_base64": (
        "Store a small file (up to ~1.5 MB) given as base64 text, when attaching it is not possible. "
        "Same result as nexus_file_put."),
}


class FileRef(BaseModel):
    """A chat attachment as ChatGPT passes it (openai/fileParams); extra keys are kept."""
    model_config = ConfigDict(extra="allow")
    download_url: str | None = None
    file_id: str | None = None
    name: str | None = None
    mime_type: str | None = None


def _file_call(fn) -> dict:
    from .files import FileError
    try:
        return fn()
    except FileError as e:
        return e.as_dict()


def build_server(db_path: str, scope: str, token_dir: str | None = None,
                 result_format: str = "full", agent_name: str | None = None,
                 files_dir: str | None = None) -> MCPServer:
    """`result_format="compact"` returns nexus_read / nexus_search as minified compact-1 JSON text
    (see nexus.compact); "full" keeps the original dict results."""
    compact = result_format == "compact"
    conn = connect(db_path, check_same_thread=False)
    init_schema(conn)
    live = None
    if token_dir:  # read-only Drive metadata check for nexus_read's source.live
        from .freshness import DriveVersionChecker
        live = DriveVersionChecker(token_dir)
    tools = AgentTools(NexusCore(Store(conn)), scope, live_check=live)
    instructions = INSTRUCTIONS + f"\n\nDefault scope: {scope}"
    if agent_name:
        instructions += "\n\n" + WRITE_INSTRUCTIONS
    server = MCPServer(name="nexus", instructions=instructions)

    # One SQLite connection is shared by every tool. Over HTTP, requests can run in parallel
    # threads; a lock keeps one tool call (and its transaction) on the connection at a time.
    lock = threading.RLock()
    register = server.tool
    actor = f"agent:{agent_name}" if agent_name else "reader"

    def locked_tool(*args, **kwargs):
        deco = register(*args, **kwargs)
        name = kwargs.get("name")

        def apply(fn):
            @functools.wraps(fn)
            def call(*a, **k):
                # every call is timed into <db>.calls.jsonl (nexus.calls): lock wait + run time
                start = time.time()
                t0 = time.perf_counter()
                with lock:
                    t1 = time.perf_counter()
                    status = None
                    try:
                        out = fn(*a, **k)
                        if isinstance(out, dict):
                            status = out.get("status")
                        elif isinstance(out, str):
                            m = re.search(r'"status":"([A-Z_]+)"', out[:400])
                            status = m.group(1) if m else None
                        return out
                    except Exception as e:
                        status, out = f"EXCEPTION:{type(e).__name__}", None
                        raise
                    finally:
                        t2 = time.perf_counter()
                        log_call(db_path, {
                            "at": _dt.datetime.fromtimestamp(start, _dt.timezone.utc).isoformat(timespec="milliseconds")
                                  .replace("+00:00", "Z"),
                            "tool": name or fn.__name__, "actor": actor, "pid": os.getpid(),
                            "lock_wait_ms": round((t1 - t0) * 1000, 1), "elapsed_ms": round((t2 - t1) * 1000, 1),
                            "result_chars": result_size(out), "status": status, "args": summarize_args(k)})
            deco(call)
            return fn
        return apply
    server.tool = locked_tool

    @server.custom_route("/health", methods=["GET"])
    async def health(request):
        from starlette.responses import JSONResponse
        # A fresh read-only connection: FTS5 caches its index structure per connection, so a
        # quick_check on the long-lived tool connection reports a false "malformed inverted
        # index" after another process (DSH session, CLI, backup restore) has written.
        check = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            q = lambda sql: check.execute(sql).fetchone()[0]  # noqa: E731
            body = {"status": "OK", "scope": scope, "db": Path(db_path).name,
                    "objects": q("SELECT COUNT(*) FROM objects"),
                    "native_documents": q("SELECT COUNT(*) FROM native_documents"),
                    "integrity": q("PRAGMA quick_check"),
                    "writes_enabled": bool(agent_name),
                    "commit": os.environ.get("NEXUS_COMMIT")}
        finally:
            check.close()
        return JSONResponse(body, status_code=200 if body["integrity"] == "ok" else 503)

    def text(result: dict) -> str:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    def nexus_search(query: str = "", limit: int = 8, queries: list[str] | None = None) -> dict:
        out = tools.nexus_search(query=query, limit=limit, queries=queries)
        return text(compact_search(out)) if compact else out

    def nexus_read(object_id: str, anchors: list[str] | None = None, offset: int = 0,
                   max_chars: int = 8000, revision: int | None = None, view: str = "full") -> dict:
        out = tools.nexus_read(object_id=object_id, anchors=anchors, offset=offset,
                               max_chars=max_chars, revision=revision, view=view)
        if compact and out.get("kind") == "nexus_native_document":
            return text(out)
        return text(compact_read(out)) if compact else out

    for fn in (nexus_search, nexus_read):
        desc = TOOL_DESCRIPTIONS[fn.__name__]
        if compact:
            fn.__annotations__["return"] = "str"
            desc += " Results use " + (SEARCH_NOTE if fn is nexus_search else READ_NOTE)
        # structured_output=False: one text block only. A str return would otherwise also be sent
        # as structuredContent {"result": ...}, the same data twice on the wire.
        server.tool(name=fn.__name__, description=desc, structured_output=False)(fn)

    @server.tool(name="nexus_related", description=TOOL_DESCRIPTIONS["nexus_related"])
    def nexus_related(object_id: str, include_candidates: bool = True,
                      max_depth: int = 1, expand_facts: bool = False, max_facts: int = 6) -> dict:
        return tools.nexus_related(object_id=object_id, include_candidates=include_candidates,
                                   expand_facts=expand_facts, max_facts=max_facts,
                                   max_depth=max_depth)

    @server.tool(name="nexus_learning_context", description=TOOL_DESCRIPTIONS["nexus_learning_context"])
    def nexus_learning_context(topic: str = "", course: str | None = None, max_receipts: int = 5) -> dict:
        return tools.nexus_learning_context(topic=topic, course=course, max_receipts=max_receipts)

    @server.tool(name="nexus_resolve", description=TOOL_DESCRIPTIONS["nexus_resolve"])
    def nexus_resolve(target: str) -> dict:
        return tools.nexus_resolve(target=target)

    @server.tool(name="nexus_trace", description=TOOL_DESCRIPTIONS["nexus_trace"])
    def nexus_trace(item_id: str) -> dict:
        return tools.nexus_trace(item_id=item_id)

    # ---- files (nexus.files): stored originals; their text is already in search / read ----------
    from mcp.types import ImageContent, TextContent

    from .files import FileError, FileStore, Source, default_files_dir, fetch_url
    files = FileStore(Store(conn), files_dir or default_files_dir(db_path))

    def in_scope(object_id: str) -> dict:
        info = files.info(object_id)
        if info["scope"] != scope:
            raise FileError("NOT_FOUND", f"no stored file {object_id!r} in this scope")
        return info

    @server.tool(name="nexus_file_info", description=FILE_DESCRIPTIONS["nexus_file_info"])
    def nexus_file_info(object_id: str) -> dict:
        try:
            return {"status": "OK", **in_scope(object_id)}
        except FileError as e:
            return e.as_dict()

    @server.tool(name="nexus_file_view", description=FILE_DESCRIPTIONS["nexus_file_view"],
                 structured_output=False)
    def nexus_file_view(object_id: str, page: int = 1, max_px: int = 1600) -> list:
        try:
            info = in_scope(object_id)
            img, mime, facts = files.render(info["object_id"], page, max_px)
        except FileError as e:
            return [TextContent(type="text", text=json.dumps(e.as_dict(), ensure_ascii=False))]
        except ImportError as e:
            return [TextContent(type="text", text=json.dumps(
                {"status": "NOT_RUN", "error": f"rendering needs pymupdf and pillow: {e}"}))]
        head = {"status": "OK", "object_id": info["object_id"], "name": info["name"], "mime": info["mime"],
                **facts, "image_bytes": len(img)}
        return [TextContent(type="text", text=json.dumps(head, ensure_ascii=False)),
                ImageContent(type="image", data=base64.b64encode(img).decode(), mime_type=mime)]

    if agent_name:
        # Direct, undoable writes (user's choice, 2026-11-09): no confirmation step; every write is
        # a new revision or a reversible flag. The writer identity (agent:<name>) is fixed by whoever
        # starts the server, not chosen by the agent. Drive mirrors stay read-only.
        # Every call is timed server-side and logged to <db>.writes.jsonl (python -m nexus.native timing).
        from .native import NativeDocs, NativeError, log_write
        proposer = f"agent:{agent_name}"
        native = NativeDocs(conn, agent_writes=True)

        def timed(tool: str, fn, **meta) -> dict:
            t = time.perf_counter()
            try:
                out = fn()
            except NativeError as e:
                out = e.as_dict()
            out["elapsed_ms"] = round((time.perf_counter() - t) * 1000, 1)
            log_write(db_path, {"tool": tool, "actor": proposer, "status": out.get("status"),
                                "elapsed_ms": out["elapsed_ms"], "proposal_id": out.get("proposal_id"),
                                **meta})
            return out

        def tool(fn):
            return server.tool(name=fn.__name__, description=TOOL_DESCRIPTIONS[fn.__name__])(fn)

        from .linking import SUGGEST_NOTE, suggest as suggest_links

        def with_links(out: dict) -> dict:
            # The server finds what the new text most likely belongs with (titles, a few ms), so the
            # agent can link it in the same turn instead of searching for it (2026-11-10).
            if out.get("status") == "OK" and out.get("doc_id"):
                try:
                    links = suggest_links(conn, out["doc_id"], scope)
                except Exception as e:          # suggestions must never fail a write
                    out["suggested_links_error"] = f"{type(e).__name__}: {e}"
                else:
                    if links:
                        out["suggested_links"] = links
                        out["suggested_links_note"] = SUGGEST_NOTE
            return out

        @tool
        def nexus_create_document(title: str, children: list, message: str = "") -> dict:
            spec = {"title": title, "children": children}
            return timed("nexus_create_document",
                         lambda: with_links(native.create(scope, spec, proposer, message or None)),
                         chars=len(json.dumps(children, ensure_ascii=False)))

        @tool
        def nexus_edit_document(doc_id: str, expected_revision: int, ops: list[dict],
                                message: str = "", title: str | None = None) -> dict:
            return timed("nexus_edit_document",
                         lambda: with_links(native.edit(doc_id, expected_revision, ops, proposer,
                                                        message or None, title, scope=scope)),
                         doc_id=doc_id, ops=len(ops or []))

        @tool
        def nexus_add_relation(from_id: str, relation_type: str, to_id: str,
                               evidence: list | None = None, note: str = "") -> dict:
            return timed("nexus_add_relation",
                         lambda: native.add_relation(scope, from_id, relation_type, to_id, proposer,
                                                     evidence, note or None))

        @tool
        def nexus_create_object(type: str, name: str, evidence: list, aliases: list[str] | None = None,
                                description: str = "", note: str = "") -> dict:
            return timed("nexus_create_object",
                         lambda: native.create_object(scope, type, name, proposer, aliases,
                                                      description or None, evidence, note or None))

        @tool
        def nexus_restore_revision(doc_id: str, to_revision: int, expected_revision: int) -> dict:
            return timed("nexus_restore_revision",
                         lambda: native.restore(doc_id, to_revision, expected_revision, proposer),
                         doc_id=doc_id)

        @tool
        def nexus_archive_document(doc_id: str, archived: bool = True) -> dict:
            return timed("nexus_archive_document", lambda: native.archive(doc_id, archived, proposer),
                         doc_id=doc_id)

        @tool
        def nexus_remove_relation(relation_id: str) -> dict:
            return timed("nexus_remove_relation", lambda: native.remove_relation(relation_id, proposer))

        def put_result(out: dict) -> dict:
            if out.get("status") == "OK":
                try:
                    links = suggest_links(conn, out["object_id"], scope)
                except Exception as e:          # suggestions must never fail a write
                    out["suggested_links_error"] = f"{type(e).__name__}: {e}"
                else:
                    if links:
                        out["suggested_links"] = links
                        out["suggested_links_note"] = SUGGEST_NOTE
            return out

        @server.tool(name="nexus_file_put", description=FILE_DESCRIPTIONS["nexus_file_put"],
                     meta={"openai/fileParams": ["file"]})
        def nexus_file_put(file: FileRef, name: str = "") -> dict:
            def run():
                if not file.download_url:
                    raise FileError("NO_DOWNLOAD_URL", "the file argument carried no download_url; "
                                    "use nexus_file_put_base64 for a small file")
                data = fetch_url(file.download_url)
                return put_result(files.put(data, name or file.name or file.file_id or "upload", scope, proposer,
                                            Source("chat-upload", file.file_id or "unknown"), file.mime_type))
            return timed("nexus_file_put", lambda: _file_call(run), file_id=file.file_id)

        @server.tool(name="nexus_file_put_base64", description=FILE_DESCRIPTIONS["nexus_file_put_base64"])
        def nexus_file_put_base64(filename: str, data_base64: str) -> dict:
            def run():
                if len(data_base64) > MAX_BASE64_CHARS:
                    raise FileError("TOO_LARGE", f"base64 input over {MAX_BASE64_CHARS} characters; "
                                    "attach the file and use nexus_file_put")
                try:
                    data = base64.b64decode(data_base64, validate=True)
                except ValueError as e:
                    raise FileError("INVALID_BASE64", str(e)) from None
                return put_result(files.put(data, filename, scope, proposer, Source("base64", filename)))
            return timed("nexus_file_put_base64", lambda: _file_call(run), chars=len(data_base64))

        from . import register_edit as regedit

        @tool
        def nexus_register_add_row(course: str, type: str, row_id: str, fields: dict, label: str = "") -> dict:
            return timed("nexus_register_add_row",
                         lambda: regedit.add_row(native, course, type, row_id, proposer, fields, label or None),
                         course=course, type=type)

        @tool
        def nexus_register_edit(object_id: str, aspect: str, expected_revision: int, set_fields: dict,
                                unset: list[str] | None = None) -> dict:
            return timed("nexus_register_edit",
                         lambda: regedit.edit_fields(native, object_id, aspect, proposer,
                                                     expected_revision=expected_revision, set_fields=set_fields,
                                                     unset=unset),
                         object_id=object_id, aspect=aspect)

    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.mcp_server")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--token-dir", help="Drive OAuth dir (client_secret.json + token.json) for the "
                                       "read-only live version check; omit to report NOT_CHECKED")
    p.add_argument("--format", choices=("full", "compact"), default="full",
                   help="result presentation of nexus_read / nexus_search (default full)")
    p.add_argument("--agent-name", help="enable direct, undoable write tools for agent:<name> "
                                         "(create/edit/relate + restore/archive/remove); calls are "
                                         "timed into <db>.writes.jsonl")
    p.add_argument("--files-dir", help="where stored file originals live (default: <db>.files)")
    p.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    p.add_argument("--host", default="127.0.0.1", help="http only; loopback addresses only")
    p.add_argument("--port", type=int, default=8765, help="http only (default 8765)")
    args = p.parse_args(argv)
    server = build_server(args.db, args.scope, args.token_dir, args.format, args.agent_name,
                          args.files_dir)
    if args.transport == "stdio":
        server.run("stdio")
        return 0
    if not is_loopback(args.host):
        p.error(f"--host {args.host} is not a loopback address; there is no authentication yet, "
                "so HTTP is served on 127.0.0.1 only (use an SSH tunnel for remote access)")
    server.run("streamable-http", host=args.host, port=args.port)
    return 0


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
