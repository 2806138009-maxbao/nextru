# Nextru

A personal knowledge base for AI agents, served over [MCP](https://modelcontextprotocol.io).

Nextru keeps what you know: documents, course or project registers, notes and files of any type.
Every piece keeps where it came from, which version it is and how it relates to the rest. Agents
such as ChatGPT, Claude or a local model then search and read it, and, if you allow it, write
back, with every change undoable.

- **Sources, not summaries.** Every paragraph has a stable anchor (`h2.3-a1b2c3d4`) and points to
  the exact source revision. Answers can cite what a document said at that revision.
- **Files of any type.** Originals are stored as is, under their SHA-256, written once. PDF pages
  and text files become searchable page by page. A PDF page or an image can be handed to a
  vision model with `nexus_file_view`.
- **Relations.** Documents, files, people, courses and topics are objects, and typed relations
  connect them. Relations an agent adds stay `UNVERIFIED`, with cited evidence, until you accept them.
- **Undoable writes.** Native documents keep every revision. Agents edit by compare-and-swap on
  the revision they read; restore and archive undo any change.
- **Small core.** SQLite plus the Python standard library. MCP, PDF/image handling, Sheets import
  and embeddings are optional extras.

> Status: early (0.1). It runs one person's daily knowledge base, but the interfaces may still
> change. See [What is verified](#what-is-verified).

## Quick start

Python 3.11+ with SQLite 3.34+ (for the FTS5 trigram tokenizer; check with `python -c "import sqlite3; print(sqlite3.sqlite_version)"`). Tested on SQLite 3.37 and 3.5x.

```sh
git clone https://github.com/<owner>/nextru && cd nextru
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[all]"           # MCP server + PDF/image files + Sheets import
```

Store a file and search it:

```sh
python -m nexus.files --db data/kb.sqlite --scope personal:me put notes.pdf
python -m nexus.files --db data/kb.sqlite --scope personal:me render file/<id> --page 2 --out p2.png
```

Connect an MCP client over stdio (Claude Desktop, Codex, Cursor and others). Example client config:

```json
{
  "mcpServers": {
    "nextru": {
      "command": "/path/to/nextru/.venv/bin/python",
      "args": ["-m", "nexus.mcp_server", "--db", "/path/to/data/kb.sqlite",
               "--scope", "personal:me", "--agent-name", "claude"]
    }
  }
}
```

Leave out `--agent-name` for a read-only server. Everything is in one scope, which is fixed when
the server starts: agents don't choose it.

A synthetic demo database (no personal data) for trying the read tools:

```sh
python scripts/build_db.py --db var/demo.sqlite
python -m nexus.mcp_server --db var/demo.sqlite --scope project:DLC-01
```

## MCP tools

Read (always available):

| Tool | What it does |
|---|---|
| `nexus_search` | Full-text search (SQLite FTS5 trigram, CJK-aware; optional embedding channel) over documents, file pages, registers and objects |
| `nexus_read` | Read a document or file text by anchor, paged, at a given revision |
| `nexus_related` | Everything related to an object, strongest first |
| `nexus_resolve` | Turn a name, code or link into an object id |
| `nexus_trace` | Source, revision and anchor behind any item |
| `nexus_learning_context` | One-call context for a course topic (register, receipts, state) |
| `nexus_file_info` | What a stored file is: type, size, pages, SHA-256, sources, relations |
| `nexus_file_view` | A PDF page or an image, returned as an image the model can look at |

Write (only with `--agent-name <name>`; the writer is recorded as `agent:<name>`):

| Tool | What it does |
|---|---|
| `nexus_create_document`, `nexus_edit_document` | Native documents; every edit is a new revision |
| `nexus_restore_revision`, `nexus_archive_document` | Undo an edit or a create |
| `nexus_create_object` | A person, place, course, topic, ... (needs evidence) |
| `nexus_add_relation`, `nexus_remove_relation` | Typed relations; an agent's relation is `UNVERIFIED` and cites evidence |
| `nexus_file_put` | Store a file attached in a chat (ChatGPT `openai/fileParams`) |
| `nexus_file_put_base64` | Store a small file sent as base64 |
| `nexus_register_add_row`, `nexus_register_edit` | Rows and fields of a course register |

## Getting data in

| Source | Command |
|---|---|
| Any file (PDF, image, text, anything) | `python -m nexus.files ... put <path>` |
| A Drive file (read-only) | `python -m nexus.files ... drive <file-id> --token-dir <dir>` |
| A Google Doc (read-only) | `python -m nexus.ingest --db ... --scope ... --doc <id> --token-dir <dir>` |
| A Google Sheet register | `python -m nexus.ingest.register --db ... --scope ... --course <code> --sheet <id> --token-dir <dir>` |
| An Obsidian vault (two-way) | `python -m nexus.obsidian --db ... --scope ... --vault <dir> --state <file> run` |
| A chat attachment | `nexus_file_put` from the agent |

Drive access is read-only. `--token-dir` holds your own OAuth `client_secret.json` and `token.json`;
both are git-ignored and never leave your machine.

## How data is kept

- **One SQLite file** (`--db`) holds objects, sources, sections, revisions, relations and the
  search index. Revisions, sealed sources and file rows are append-only, enforced by triggers.
- **File originals** live next to it in `<db>.files/<sha[:2]>/<sha>`, read-only, verified with
  `python -m nexus.files ... verify`.
- **Archive** (optional, `--archive <dir>`): the exact bytes of every ingested source revision,
  write-once, with a manifest (`python -m nexus.archive verify`).
- **Backup**: `python -m nexus.native --db ... backup <file>` makes an online, integrity-checked copy.
  [`deploy/backup/nexus-backup`](deploy/backup/nexus-backup) is a daily job that also mirrors the
  file originals; [`deploy/backup/restore-drill.py`](deploy/backup/restore-drill.py) restores a
  backup into a scratch folder and checks it.

## Running on a server

The HTTP transport (`--transport http`) binds to loopback only and refuses any other address.
**There is no authentication in the server itself.** Expose it only through something that
authenticates: an SSH tunnel, an OAuth proxy, Tailscale, or a provider tunnel such as OpenAI's
Secure MCP Tunnel. [`deploy/`](deploy/) has hardened systemd units (dedicated user,
`ProtectSystem=strict`, minimal writable paths). See [SECURITY.md](SECURITY.md).

## What is verified

Verified, with tests or recorded runs:

- 368 automated tests (unittest; `python scripts/run_tests.py`), passing on Windows (Python 3.14)
  and Linux (Python 3.12), including a fresh-virtualenv install from PyPI.
- Files: PDF pages searchable by page, page and image rendering, write-once storage,
  hash verification and tamper detection, one file with many sources.
- In daily use on a small VPS behind the loopback-only HTTP transport, with ChatGPT, Claude
  and Grok as clients.

Not verified yet:

- How every MCP client handles `nexus_file_put` attachments and `nexus_file_view` images (it
  differs between clients; ChatGPT has not been tested end to end).
- Link suggestions for very short titles (course topic labels) are weak: the right candidate
  ranks first but often stays below the display threshold.
- Images have no searchable text beyond their name.
- Concurrency beyond a few clients on one SQLite file.

## Layout

```
src/nexus/        core (db, core, search, native docs, files, relations, MCP server, ingest)
tests/            unittest suite (synthetic data only)
deploy/           systemd units and backup / restore scripts
integrations/     Claude Code hook, a minimal API agent, an agent skill
experiments/      the MCP file-transfer probe used to design nexus.files
docs/             agent surface, context modes, archive, research evidence
```

## License

[Apache-2.0](LICENSE)
