# Nextru

**One knowledge ledger for all your AIs. They write it, you approve it, and every line cites its
source.**

Nextru is a personal knowledge store served over [MCP](https://modelcontextprotocol.io). ChatGPT,
Claude, Grok or a local model read from it and write to it. You stay the owner: you see who wrote
what, confirm or reject what an agent proposed, and undo any change.

## Why

If you use several AIs for real work (courses, projects, writing, research), each of them forgets
you, or remembers you in its own way:

- **Built-in assistant memory** is a summary the assistant wrote about you. You can't see all of
  it, can't cite it, and can't take it to another assistant.
- **Memory layers** have an LLM extract "facts" on every write. Extraction is lossy and can be
  wrong, and the original wording is gone when you need to check it.
- **RAG over your documents** finds similar passages, but can't say which version of which
  document they came from, or whether they still hold.
- **Note apps** are great for you, but an agent can only read files there. It can't safely
  write back.

## What Nextru does instead

- **Keeps the source, not a summary.** Every paragraph keeps a stable anchor (`h2.3-a1b2c3d4`) and
  the exact source revision, labelled `VERIFIED`, `UNVERIFIED`, `DRAFT` or `INFERENCE`.
  An answer can quote what a document said at revision 12, and say who claimed it.
- **Lets many agents write, safely.**
  - The server fixes each writer's identity (`agent:chatgpt`, `agent:claude`, ...), so an agent
    can't pose as another.
  - Edits are compare-and-swap on the revision the agent read, and every revision is kept.
  - A relation or new object from an agent is `UNVERIFIED`, cites evidence, and waits for you.
- **Holds documents, structured registers and files together.** PDFs are searchable and citable
  page by page. A page or an image can be handed to a vision model. Originals are stored once,
  under their SHA-256.
- **Keeps the LLM out of the write path.** Storing something costs no model calls: it's
  deterministic, works offline, and produces the same result every time.
- **Is yours to keep.** One SQLite file plus a folder of originals, with online backups, a
  restore drill and tamper evidence. Standard library only at the core.

## In daily use

The author's own instance, over 17 days (counts only):

| | |
|---|---|
| Clients writing to one store | 4: ChatGPT, Grok, DeepSeek, Claude |
| Tool calls | ~4,100 (reads 1,450, searches 1,020, relations 580, document writes 570) |
| Document revisions written by agents / by the owner | 617 / 13 |
| Relations | 2,886, of which 2,593 confirmed by the owner |
| Ingested | ~900 Drive documents → 104,000 anchored paragraphs, plus course registers and files |

The pattern behind these numbers is what Nextru is built for: **the agents do the writing, the
person does the approving.**

## How it differs

| | Nextru | Memory layers (e.g. Mem0) | Temporal graphs (e.g. Graphiti) | Markdown memory (e.g. Basic Memory) |
|---|---|---|---|---|
| What is stored | original text + anchors + revisions | LLM-extracted facts | LLM-extracted entities/edges over episodes | your Markdown notes |
| LLM call per write | no | yes | yes | no |
| Many agents, one owner | built in: fixed identities, proposals, CAS, undo | per user/agent memories | shared graph | shared files |
| "What did it say at version N?" | yes | no | episodes, not document versions | via git, if you use it |
| Files (PDF pages, images) | yes, page-level | no | no | files as notes |

Each of those projects does things Nextru doesn't (managed hosting, temporal fact invalidation,
a polished note-taking flow). Pick by what you need to trust.

## What it is not

- **Not a note app.** There's no editor for humans. Use Obsidian for that; Nextru syncs with a
  vault both ways.
- **Not a team wiki.** It's one person and their agents: no multi-user permissions.
- **Not "AI organizes everything for you".** It's deliberately conservative: agents propose, you
  decide.

> Status: early (0.1). It runs one person's daily knowledge base, but the interfaces may still
> change. The Python package and the tools are still called `nexus` / `nexus_*` (the project's
> working name). See [What is verified](#what-is-verified).

## Quick start

Python 3.11+ with SQLite 3.34+ (for the FTS5 trigram tokenizer; check with `python -c "import sqlite3; print(sqlite3.sqlite_version)"`). Tested on SQLite 3.37 and 3.5x.

```sh
git clone https://github.com/2806138009-maxbao/nextru && cd nextru
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

Web clients (ChatGPT connectors, Claude on the web) need a public HTTPS endpoint with OAuth.
Nextru doesn't ship that piece yet: today you put your own authenticating proxy or tunnel in
front. Built-in remote auth is the next planned piece.

## What is verified

Verified, with tests or recorded runs:

- 368 automated tests (unittest; `python scripts/run_tests.py`), passing on Windows (Python 3.14)
  and Linux (Python 3.12), including a fresh-virtualenv install from PyPI.
- Files: PDF pages searchable by page, page and image rendering, write-once storage,
  hash verification and tamper detection, one file with many sources.
- **ChatGPT, end to end:** a file attached in a ChatGPT chat is stored through `nexus_file_put`
  (ChatGPT passes the attachment, Nextru downloads, hashes and keeps it), and ChatGPT's model reads
  the image `nexus_file_view` returns (2026-11-26).
- In daily use on a small VPS behind the loopback-only HTTP transport, with ChatGPT, Claude
  and Grok as clients.

Not verified yet:

- File tools in clients other than ChatGPT (Claude, Grok, local models): how they pass
  attachments and whether they show returned images differs per client.
- Retrieval quality has no public benchmark yet. In the author's own tests, agents sometimes
  missed a precedent that was stored in another course or project.
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
