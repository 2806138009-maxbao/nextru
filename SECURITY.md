# Security

Nextru holds personal data. These are the boundaries it enforces itself, and the ones you have to
provide when you run it.

## What the server enforces

- **Loopback only.** `--transport http` refuses any `--host` that isn't a loopback address. The
  server has **no authentication of its own**. Whoever can reach the port can use every tool the
  server was started with.
- **Read-only unless you say otherwise.** Write tools exist only when the server is started with
  `--agent-name <name>`. The writer identity (`agent:<name>`) is fixed by whoever starts the
  server, not chosen by the agent, and every write is logged with it.
- **One scope per server.** The scope is fixed at start. Objects, files and evidence from other
  scopes are refused (`NOT_FOUND` / `INVALID_REQUEST`), not just hidden.
- **Agents can't overwrite or delete.**
  - Native document edits are new revisions (compare-and-swap on the revision read).
  - Restore and archive undo them; documents, revisions, file rows and sealed sources can't be
    deleted (SQLite triggers).
  - Drive and Sheets mirrors are read-only (`MIRROR_READ_ONLY`).
  - An agent's relation or new object is `UNVERIFIED` and must cite evidence.
- **No internal paths leave the server.** Agent-facing results are scrubbed of local filesystem
  paths (`nexus.redaction`).
- **File downloads.** `nexus_file_put` fetches only `https` URLs whose host resolves to public
  addresses (redirects re-checked), up to 50 MB. It never fetches from the server's own network.
- **Every call is timed and logged** to `<db>.calls.jsonl` (tool, actor, argument shape, size,
  status), and writes also to `<db>.writes.jsonl`. These logs contain argument summaries; treat
  them as personal data.

Triggers stop accidents and misbehaving agents, not someone with shell access to the database
file. Tamper evidence comes from hashes: `python -m nexus.files ... verify` and
`python -m nexus.archive verify`.

## What you must provide

- **Authentication in front of HTTP**: an SSH tunnel, an authenticating reverse proxy (OAuth),
  Tailscale, or a provider tunnel (e.g. OpenAI Secure MCP Tunnel). Never put the port on a public
  interface. Quick tunnels with an unguessable URL are not authentication.
- **One server per client** if clients deserve different powers: e.g. a read-only server for one
  assistant, a writing one (`--agent-name`) for another.
- **OS isolation**: run as a dedicated user with only the data directory writable. The units in
  [`deploy/`](deploy/) do this (`ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`,
  explicit `ReadWritePaths`).
- **Backups you have restored at least once**: [`deploy/backup/nexus-backup`](deploy/backup/nexus-backup)
  backs up the database and mirrors the file originals; [`deploy/backup/restore-drill.py`](deploy/backup/restore-drill.py)
  proves a backup restores, including every original's hash.
- **Credentials stay local**: Drive OAuth files (`client_secret.json`, `token.json`) are read from
  `--token-dir` and git-ignored. Nextru only asks Drive for read access to ingest.

## Reporting a vulnerability

Please report security problems privately through GitHub's "Report a vulnerability" (Security
advisories) on this repository rather than in a public issue.
