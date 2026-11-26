"""stdio MCP server exposing one read-only tool, ``nexus_context`` (Claude Code, Codex, any MCP client).

    python -m nexus.context_mcp --db var/t009/lumen.sqlite --scope research:lumen-synth

Claude Code registration (project scope, example):
    claude mcp add nexus-context -- python -X utf8 -m nexus.context_mcp --db <db> --scope <scope>
"""

from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer

from .context_provider import ContextProvider

DESCRIPTION = (
    "Return long-term knowledge relevant to a task: statements with status, verification, validity, "
    "Authority owner/write policy, source anchor and explicit applies_when / not_when. Call it with the "
    "user's task text before acting on anything that could touch past decisions, permissions or "
    "confidentiality. Read-only; it never authorizes an action. Follow the returned usage_guide.")


def build_server(db: str, scope: str, log: str | None = None) -> MCPServer:
    provider = ContextProvider(db, scope, log_path=log)
    server = MCPServer(name="nexus-context", instructions=DESCRIPTION)

    @server.tool(name="nexus_context", description=DESCRIPTION)
    def nexus_context(task: str, max_items: int = 3) -> dict:
        return provider.context(task, client="mcp", max_items=max_items)

    return server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m nexus.context_mcp")
    p.add_argument("--db", required=True)
    p.add_argument("--scope", required=True)
    p.add_argument("--log")
    a = p.parse_args(argv)
    build_server(a.db, a.scope, a.log).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
