"""Two-way sync between Nexus and an Obsidian vault.

The vault lives next to the database (on the VPS); the owner's own machines keep a copy of the
vault with Syncthing. This process is the only thing that writes either side of the bridge, and it
writes to Nexus as the owner (``user``).

Layout of the vault::

    实体/<type>/<name>.md     one note per entity (Drive-folder entities stay out)
    anything else *.md        one note per Nexus-native document (new ones from Nexus go to 文档/)

Mapping:

* A document note: ``#`` headings are sections (``#`` = top level), blocks separated by a blank line
  are paragraphs. Node ids never appear in the note; the sync keeps them in its state file and
  matches paragraphs by text, so an edit becomes set_text / set_title / insert / delete ops on the
  revision the note was based on (compare-and-swap, nothing is overwritten silently).
* An entity note: front matter carries the type and aliases, the body is the description.
* Every note ends with a collapsed ``> [!nexus]`` callout listing the object's relations, one per
  line. Deleting a line removes that relation (it stays in history). Text typed into the callout is
  not kept: it is regenerated from Nexus.
* ``[[links]]`` written in a note become the owner's LINKS_TO relations, with the paragraph as the
  evidence; removing the link removes the relation this sync made for it.
* A new note becomes a new document (or, under 实体/<type>/, a new entity). Deleting a document note
  archives the document (undoable); deleting an entity note only stops syncing it.
* Both sides changed: if the changes touch different paragraphs (or different entity fields) they
  are merged; otherwise Nexus keeps its version, and the local text is saved next to it as
  ``<name> (冲突 <time>).md`` with ``nexus_conflict_of`` in its front matter (not synced).

CLI::

    python -m nexus.obsidian --db main.sqlite --scope research:realtest --vault VAULT --state STATE.json once
    python -m nexus.obsidian ... run            # loop: local changes every 2 s, Nexus every 30 s
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from .db import connect, init_schema
from .errors import Status
from . import review
from .native import OWNER, NativeDocs, NativeError
from .redaction import scrub_local_paths
from . import register_edit as regedit
from . import drive_migrate

ENTITY_DIR = "实体"
DOC_DIR = "文档"
MIRROR_DIR = "Drive"          # Drive document snapshots, in their Drive folder tree (read-only)
REG_DIR = "登记表"            # course register mirrors (read-only)
FULL_RENDER_EVERY = 20        # pulls; a full re-render now and then catches anything a fingerprint missed
CALLOUT_HEAD = "> [!nexus]- Nexus 关系（删掉一行 = 删掉这条关系；这里打字不会保存）"
SKIP_ENTITY_TYPES = {"folder"}
SETTLE_SECONDS = 2.0          # a file must be this old before it is read (writers finish first)
NEW_NOTE_SETTLE = 10.0        # a brand-new note waits longer (Obsidian names it "Untitled" first)
MAX_REL_LINES = 300
MASS_DELETE_GUARD = 0.3       # more than this share of notes gone at once = refuse (unmounted vault?)

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_WIKILINK = re.compile(r"(?<!!)\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")
_REL_ID = re.compile(r"%%r:([^%\s]+)%%")
_BAD_NAME = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def log(event: str, **kw: Any) -> None:
    print(json.dumps({"t": _dt.datetime.now().isoformat(timespec="seconds"), "event": event, **kw},
                     ensure_ascii=False), flush=True)


# ================================================================== front matter
def _yaml_value(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in "\"[{" or raw in ("true", "false", "null") or re.fullmatch(r"-?\d+", raw):
        try:
            return json.loads(raw)
        except ValueError:
            if raw.startswith("[") and raw.endswith("]"):
                return [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
    if raw[0] == "'" and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    return raw


def split_front_matter(text: str) -> tuple[list[tuple[str, list[str]]], str]:
    """-> ([(key, raw lines)], body). Unknown keys are kept verbatim when the note is rewritten."""
    if not text.startswith("---"):
        return [], text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return [], text
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            break
    else:
        return [], text
    entries: list[tuple[str, list[str]]] = []
    for ln in lines[1:end]:
        m = re.match(r"^([A-Za-z0-9_\-一-鿿]+):(.*)$", ln)
        if m and not ln.startswith((" ", "\t")):
            entries.append((m.group(1), [ln]))
        elif entries:
            entries[-1][1].append(ln)
    return entries, "\n".join(lines[end + 1:]).lstrip("\n")


def fm_get(entries: list[tuple[str, list[str]]], key: str) -> Any:
    for k, raw in entries:
        if k == key:
            first = raw[0].split(":", 1)[1]
            if first.strip():
                return _yaml_value(first)
            items = [ln.strip()[1:].strip() for ln in raw[1:] if ln.strip().startswith("-")]
            return [_yaml_value(i) for i in items] if items else None
    return None


def _emit(key: str, value: Any) -> list[str]:
    if isinstance(value, list):
        return [f"{key}:"] + [f"  - {json.dumps(v, ensure_ascii=False)}" for v in value] if value \
            else [f"{key}: []"]
    if isinstance(value, int):
        return [f"{key}: {value}"]
    return [f"{key}: {json.dumps(value, ensure_ascii=False)}"]


def build_front_matter(own: list[tuple[str, Any]], keep: list[tuple[str, list[str]]]) -> str:
    own_keys = {k for k, _ in own}
    lines = ["---"]
    for k, v in own:
        if v is not None:
            lines += _emit(k, v)
    for k, raw in keep:
        if k not in own_keys and not k.startswith("nexus_") and k not in ("title", "aliases"):
            lines += raw
    lines.append("---")
    return "\n".join(lines) + "\n"


# ================================================================== markdown body
def strip_callout(body: str) -> tuple[str, set[str] | None]:
    """Remove the generated relations callout. -> (body, relation ids still listed or None)."""
    lines = body.split("\n")
    out, ids, inside, seen = [], set(), False, False
    for ln in lines:
        if ln.startswith("> [!nexus]"):
            inside = seen = True
            continue
        if inside:
            if ln.startswith(">"):
                ids.update(_REL_ID.findall(ln))
                continue
            inside = False
        out.append(ln)
    return "\n".join(out).strip("\n"), (ids if seen else None)


def parse_blocks(body: str) -> list[dict]:
    """Markdown body (callout removed) -> [{kind, depth, text}] in document order. Section depth is
    normalised to parent depth + 1, paragraphs sit one level under the open section."""
    blocks: list[dict] = []
    stack: list[tuple[int, int]] = []          # (markdown level, depth) of open sections
    para: list[str] = []
    in_fence = False

    def cur_depth() -> int:
        return stack[-1][1] + 1 if stack else 0

    def flush() -> None:
        if para:
            text = "\n".join(para).strip("\n")
            text = "\n".join(l.rstrip() for l in text.split("\n"))
            if text.strip():
                blocks.append({"kind": "paragraph", "depth": cur_depth(), "text": text})
            para.clear()

    for ln in body.split("\n"):
        if _FENCE.match(ln):
            in_fence = not in_fence
            para.append(ln)
            continue
        if in_fence:
            para.append(ln)
            continue
        m = _HEADING.match(ln)
        if m and m.group(2).strip():
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            depth = stack[-1][1] + 1 if stack else 0
            stack.append((level, depth))
            blocks.append({"kind": "section", "depth": depth, "text": m.group(2).strip()})
        elif not ln.strip():
            flush()
        else:
            para.append(ln)
    flush()
    return blocks


def render_blocks(blocks: list[dict]) -> str:
    parts = []
    for b in blocks:
        parts.append(f"{'#' * min(6, b['depth'] + 1)} {b['text']}" if b["kind"] == "section" else b["text"])
    return "\n\n".join(parts)


def _parents(blocks: list[dict]) -> list[int | None]:
    """Index of each block's parent section (None = top level)."""
    out: list[int | None] = []
    open_: list[int] = []
    for i, b in enumerate(blocks):
        while open_ and blocks[open_[-1]]["depth"] >= b["depth"]:
            open_.pop()
        out.append(open_[-1] if open_ else None)
        if b["kind"] == "section":
            open_.append(i)
    return out


def diff_ops(base: list[dict], local: list[dict]) -> tuple[list[dict], set[str]]:
    """Ops that turn ``base`` (blocks with node_id) into ``local``; plus the node ids they touch."""
    ka = [(b["kind"], b["text"]) for b in base]
    kb = [(b["kind"], b["text"]) for b in local]
    match: dict[int, int] = {}                 # local index -> base index
    changed: set[int] = set()                  # local indexes matched with different text
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                match[j1 + k] = i1 + k
        elif tag == "replace":
            for k in range(min(i2 - i1, j2 - j1)):
                if base[i1 + k]["kind"] == local[j1 + k]["kind"]:
                    match[j1 + k] = i1 + k
                    changed.add(j1 + k)
    pa, pb = _parents(base), _parents(local)
    for j in range(len(local)):                # parents come first, so one pass settles it
        if j not in match:
            continue
        i = match[j]
        want = None if pb[j] is None else match.get(pb[j], -1)
        if pa[i] != want or base[i]["depth"] != local[j]["depth"]:
            del match[j]
            changed.discard(j)
    matched_base = set(match.values())
    ops: list[dict] = []
    touched: set[str] = set()
    deleted: set[int] = set()
    for i, b in enumerate(base):
        if i in matched_base:
            continue
        deleted.add(i)
        if pa[i] is not None and pa[i] in deleted:
            continue                           # removed with its section
        ops.append({"op": "delete", "node_id": b["node_id"]})
        touched.add(b["node_id"])
    for j in sorted(changed):
        nid = base[match[j]]["node_id"]
        if local[j]["kind"] == "section":
            ops.append({"op": "set_title", "node_id": nid, "title": local[j]["text"]})
        else:
            ops.append({"op": "set_text", "node_id": nid, "body": local[j]["text"]})
        touched.add(nid)
    ids: dict[int, str] = {j: base[i]["node_id"] for j, i in match.items()}
    last_child: dict[int | None, int] = {}
    for j, b in enumerate(local):
        p = pb[j]
        if j not in match:
            ref = f"r{j}"
            op = {"op": "insert", "kind": b["kind"], "parent_id": ids.get(p) if p is not None else None,
                  "after": ids.get(last_child[p]) if p in last_child else None, "ref": ref}
            op["title" if b["kind"] == "section" else "body"] = b["text"]
            ops.append(op)
            ids[j] = ref
            for x in (op["parent_id"], op["after"]):
                if x and not x.startswith("r"):
                    touched.add(x)
        last_child[p] = j
    return ops, touched


def links_in(text: str) -> list[str]:
    return [m.strip() for m in _WIKILINK.findall(text)]


PRIMARY_ASPECTS = ("register_row", "receipt")
NOT_CELLS = ("title", "aliases", "tags", "cssclasses")


def _prop_name(aspect: str, key: str, used: set[str]) -> str:
    """A register cell's property name: plain identifier, Learner_State cells prefixed ``ls_``."""
    base = re.sub(r"[^0-9A-Za-z_一-鿿]+", "_", key).strip("_").lower() or "field"
    if aspect == "learner_state":
        base = "ls_" + base
    if base in NOT_CELLS or base.startswith("nexus_"):
        base = "f_" + base
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    return name


def is_mirror(oid: str) -> bool:
    """Drive snapshots and register rows: their Authority is elsewhere, so the note is read-only."""
    return oid.startswith(("doc/", "reg/"))


NAME_CAP = 80                 # file name (without .md)
FOLDER_CAP = 30               # each folder name
MAX_REL_PATH = 180            # whole path inside the vault: Windows stops at 260 with the vault's own path


def safe_name(title: str, cap: int = NAME_CAP) -> str:
    s = _BAD_NAME.sub("_", title).strip().rstrip(".")
    return (s[:cap].rstrip().rstrip(".") or "untitled")


def name_matches(title: str, stem: str) -> bool:
    """Is ``stem`` the file name this title would get (possibly shortened, possibly with " (n)")?"""
    base = re.sub(r" \(\d+\)$", "", stem)
    if base == safe_name(title):
        return True
    # shortened to fit the path budget (or by an older, longer cap): a long prefix of the full name
    return len(base) >= 20 and safe_name(title, 10_000).startswith(base)


def fit_path(folders: list[str], title: str) -> str:
    """Folder names + title -> a vault path that stays within MAX_REL_PATH (middle folders collapse
    into "…" when the tree is too deep)."""
    folders = [safe_name(f, FOLDER_CAP) for f in folders]
    stem = safe_name(title)

    def join(fs: list[str], st: str) -> str:
        return "/".join(fs + [f"{st}.md"])
    tries = [folders]
    if len(folders) > 3:
        tries += [folders[:2] + ["…"] + folders[-1:], folders[:1] + ["…"] + folders[-1:]]
    if len(folders) > 1:
        tries += [folders[:1] + ["…"]]
    for fs in tries:
        if len(join(fs, stem)) <= MAX_REL_PATH:
            return join(fs, stem)
    fs = tries[-1]
    room = MAX_REL_PATH - len(join(fs, ""))
    return join(fs, stem[:max(20, room)].rstrip())


# ================================================================== the bridge
class Bridge:
    def __init__(self, conn: sqlite3.Connection, scope: str, vault: Path, state_path: Path):
        self.conn, self.scope, self.vault, self.state_path = conn, scope, Path(vault), Path(state_path)
        self.nd = NativeDocs(conn)
        self.state = self._load_state()

    # ------------------------------------------------------------ state + files
    def _load_state(self) -> dict:
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            # an entry without a synced base was never finished: forget it, the next pull redoes it
            st["notes"] = {k: v for k, v in st["notes"].items() if "base" in v}
            return st
        return {"version": 1, "notes": {}, "tombstones": [], "failed": {}}

    def save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _abs(self, rel: str) -> Path:
        return self.vault / rel

    def _write(self, rel: str, text: str) -> None:
        p = self._abs(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f".nexus-tmp-{os.getpid()}"   # same directory: the rename is atomic
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, p)

    def _read(self, rel: str) -> str:
        return self._abs(rel).read_text(encoding="utf-8").replace("\r\n", "\n")

    def scan(self) -> dict[str, tuple[float, list[int]]]:
        """Synced notes in the vault -> (age in seconds since the later of mtime / ctime, [mtime_ns, size])."""
        out: dict[str, tuple[float, list[int]]] = {}
        now = time.time()
        for root, dirs, files in os.walk(self.vault):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if not f.endswith(".md") or f.startswith(".") or ".sync-conflict-" in f or "~syncthing~" in f:
                    continue
                p = Path(root) / f
                try:
                    st = p.stat()
                except OSError:
                    continue
                out[p.relative_to(self.vault).as_posix()] = (now - max(st.st_mtime, st.st_ctime),
                                                             [st.st_mtime_ns, st.st_size])
        return out

    def _stat(self, rel: str) -> list[int] | None:
        try:
            st = self._abs(rel).stat()
        except OSError:
            return None
        return [st.st_mtime_ns, st.st_size]

    # ------------------------------------------------------------ change fingerprints
    def _fingerprints(self) -> dict[str, str]:
        """One cheap string per object that changes whenever its note could render differently:
        its own version, its relations, and the set of note paths (callouts link by note name)."""
        paths = sorted((i, n["path"]) for i, n in self.state["notes"].items())
        sig = hashlib.sha1(json.dumps(paths, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
        stems: dict[str, int] = {}
        for _, p in paths:
            stems[Path(p).stem] = stems.get(Path(p).stem, 0) + 1
        self._stem_count = stems
        fp: dict[str, list] = {}
        for oid, n, mx, rv in self.conn.execute(
                "SELECT object_id, COUNT(*), MAX(updated_at), MAX(revision) FROM aspects GROUP BY object_id"):
            fp[oid] = [n, mx, rv]
        for oid, rev, arch in self.conn.execute(
                "SELECT doc_id, current_revision, archived_at FROM native_documents"):
            fp.setdefault(oid, []).extend([rev, arch])
        for oid, name in self.conn.execute("SELECT object_id, display_name FROM objects WHERE scope = ?",
                                           (self.scope,)):
            fp.setdefault(oid, []).append(name)
        for oid, state in self.conn.execute("SELECT target_id, migration_state FROM authority_bindings"
                                            " WHERE effective_to IS NULL AND target_id LIKE 'reg/%'"):
            fp.setdefault(oid, []).append(state)          # a register cutover re-renders its notes
        for oid, live, mx in self.conn.execute(
                "SELECT oid, SUM(expired_at IS NULL), MAX(MAX(created_at, IFNULL(expired_at, ''))) FROM ("
                " SELECT from_id AS oid, created_at, expired_at FROM relations WHERE scope = ?"
                " UNION ALL SELECT to_id, created_at, expired_at FROM relations WHERE scope = ?) GROUP BY oid",
                (self.scope, self.scope)):
            fp.setdefault(oid, []).extend([live, mx])
        return {oid: sig + json.dumps(v, ensure_ascii=False) for oid, v in fp.items()}

    # ------------------------------------------------------------ mirrored (read-only) objects
    def _folder_tree(self) -> tuple[dict[str, str], dict[str, str]]:
        """child -> parent folder (Drive PART_OF), folder id -> folder name. Cached per cycle."""
        if getattr(self, "_tree_cache", None) is None:
            parent: dict[str, str] = {}
            for f, t in self.conn.execute(
                    "SELECT from_id, to_id FROM relations WHERE relation_type = 'PART_OF' AND expired_at IS NULL"
                    " AND to_id LIKE 'ent/folder/%' AND scope = ? ORDER BY relation_id", (self.scope,)):
                parent.setdefault(f, t)
            names = {r[0]: r[1] for r in self.conn.execute(
                "SELECT object_id, display_name FROM objects WHERE object_id LIKE 'ent/folder/%'")}
            self._tree_cache = (parent, names)
        return self._tree_cache

    def _mirror(self, oid: str) -> dict | None:
        o = self.conn.execute("SELECT type, display_name, scope FROM objects WHERE object_id = ?", (oid,)).fetchone()
        if o is None or o["scope"] != self.scope:
            return None
        title = o["display_name"] or oid
        fm: list[tuple[str, Any]] = []
        if oid.startswith("doc/"):
            rows = self.conn.execute(
                "SELECT observation_id, statement, source_ref_ids FROM observations WHERE object_id = ?"
                " AND dimension = 'document_paragraph'", (oid,)).fetchall()

            def ver(r):
                m = re.search(r"@v(\d+)/", r["observation_id"])
                return int(m.group(1)) if m else 0
            top = max((ver(r) for r in rows), default=0)
            rows = sorted((r for r in rows if ver(r) == top), key=lambda r: r["observation_id"])
            paras, _, _ = scrub_local_paths([r["statement"] or "" for r in rows])
            body = "\n\n".join(p.strip() for p in paras if p and p.strip())
            ref = json.loads(rows[0]["source_ref_ids"])[0] if rows else None
            src = self.conn.execute("SELECT location FROM source_refs WHERE source_ref_id = ?",
                                    (ref,)).fetchone() if ref else None
            st = self.conn.execute("SELECT payload FROM aspects WHERE object_id = ? AND name = 'doc_status'"
                                   " AND is_current = 1", (oid,)).fetchone()
            fm += [("nexus_readonly", "只读：正本在 Google Drive，这里改了不会写回"),
                   ("source", src["location"] if src else None),
                   ("snapshot", f"v{top}" if top else None),
                   ("doc_status", json.loads(st["payload"]).get("currency") if st else None)]
            version = f"v{top}"
        else:
            parts, version = [], None
            for r in self.conn.execute("SELECT name, payload FROM aspects WHERE object_id = ? AND is_current = 1"
                                       " ORDER BY name", (oid,)):
                p = json.loads(r["payload"])
                row = p.get("row") if isinstance(p.get("row"), dict) else None
                vals = row or (p.get("value") if isinstance(p.get("value"), dict) else {"value": p.get("value")})
                lines = [f"## {r['name']}", ""]
                for k, v in vals.items():
                    if isinstance(v, dict):
                        v = "; ".join(f"{a}={b}" for a, b in v.items())
                    lines.append(f"- **{k}**: {v}")
                if p.get("note"):
                    lines += ["", f"> {p['note']}"]
                parts.append("\n".join(lines))
                version = p.get("register_version") or version
            body = "\n\n".join(parts)
            fm += [("nexus_readonly", "只读：正本在 CORE REGISTER（Google Sheet），这里改了不会写回"),
                   ("register_version", f"v{version}" if version else None)]
        return {"type": o["type"], "title": title, "body": body, "fm": fm, "version": version}

    def _readonly(self, oid: str) -> bool:
        """Drive snapshots always; register rows until their register is migrated to Nexus."""
        if oid.startswith("doc/"):
            return True
        if oid.startswith("reg/"):
            return not regedit.is_migrated(self.conn, oid)
        return False

    def _register(self, oid: str) -> dict | None:
        """A register row as note parts: every cell a property (Learner_State cells prefixed ``ls_``),
        the row's ``notes`` cell as the body."""
        o = self.conn.execute("SELECT type, display_name, scope FROM objects WHERE object_id = ?", (oid,)).fetchone()
        if o is None or o["scope"] != self.scope:
            return None
        fields, revs, used, body, version = [], {}, set(), "", None
        for r in self.conn.execute(
                "SELECT name, payload, revision FROM aspects WHERE object_id = ? AND is_current = 1 ORDER BY"
                " CASE name WHEN 'register_row' THEN 0 WHEN 'receipt' THEN 1 WHEN 'learner_state' THEN 2 ELSE 3 END,"
                " name", (oid,)):
            p = json.loads(r["payload"])
            revs[r["name"]] = r["revision"]
            version = p.get("register_version") or version
            if r["name"] == "register_import":
                rows = (p.get("value") or {}).get("rows") or {}
                body = "\n".join(f"- {k}：{v} 行" for k, v in rows.items())
                continue
            row = p.get("row") if isinstance(p.get("row"), dict) else {}
            for k, v in row.items():
                if k == "notes" and r["name"] in PRIMARY_ASPECTS:
                    body = "" if v is None else str(v)
                    continue
                prop = _prop_name(r["name"], k, used)
                used.add(prop)
                fields.append((prop, r["name"], k, "" if v is None else str(v)))
        return {"type": o["type"], "title": o["display_name"] or oid, "body": body, "fields": fields,
                "revs": revs, "version": version, "readonly": self._readonly(oid)}

    @staticmethod
    def _props(parsed: dict) -> dict[str, str]:
        """A note's own properties (cells), as strings."""
        out = {}
        for k, _ in parsed["entries"]:
            if k.startswith("nexus_") or k in NOT_CELLS:
                continue
            v = fm_get(parsed["entries"], k)
            if isinstance(v, list):
                v = "; ".join("" if x is None else str(x) for x in v)
            elif isinstance(v, bool):
                v = "true" if v else "false"
            out[k] = "" if v is None else str(v)
        return out

    def _ensure_bases(self) -> None:
        """One table view (.base) per register tab, written once; the owner may edit or delete it."""
        made = self.state.setdefault("bases", [])
        groups: dict[str, list[list]] = {}
        for oid, n in self.state["notes"].items():
            parts = n.get("path", "").split("/")
            if oid.startswith("reg/") and len(parts) == 4 and parts[0] == REG_DIR and n.get("fields"):
                groups.setdefault("/".join(parts[:3]), []).append(n["fields"])
        for folder, rows in sorted(groups.items()):
            rel = f"{folder}.base"
            if rel in made:
                continue
            cols: list[str] = []
            values: dict[str, set] = {}
            for fields in rows:
                for p, _, _, v in fields:
                    if p not in cols:
                        cols.append(p)
                    values.setdefault(p, set()).add(v)
            # leave out what tells rows apart nowhere (same in every row) or repeats the row id
            cols = [c for c in cols if not (len(rows) > 1 and len(values[c]) == 1)
                    and c not in ("object_id", "ls_knowledge_id", "evidence_id", "receipt_id")]
            lines = ["# Made once by the Nexus bridge: a table over this register tab. Edit freely;",
                     "# it is not overwritten. Cells you change here are written to Nexus.",
                     "filters:", "  and:", f"    - file.inFolder({json.dumps(folder, ensure_ascii=False)})",
                     "views:", "  - type: table", f"    name: {json.dumps(folder.split('/')[-1], ensure_ascii=False)}",
                     "    order:", "      - file.name"] + [f"      - note.{c}" for c in cols[:14]]
            if not self._abs(rel).exists():
                self._write(rel, "\n".join(lines) + "\n")
                log("base_created", path=rel)
            made.append(rel)

    def _mirror_path(self, oid: str, title: str) -> str:
        if oid.startswith("reg/"):
            parts = oid.split("/")
            course = parts[1].upper()
            o = self.conn.execute("SELECT type FROM objects WHERE object_id = ?", (oid,)).fetchone()
            sub = "" if len(parts) == 2 else f"{(o['type'] if o else 'item')}/"
            return fit_path([REG_DIR, course] + ([sub.rstrip("/")] if sub else []), title)
        parent, names = self._folder_tree()
        chain, cur, seen = [], parent.get(oid), set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(names.get(cur, "folder"))
            cur = parent.get(cur)
        return fit_path([MIRROR_DIR] + chain[::-1], title)

    def _free_path(self, rel: str, own_id: str | None = None) -> str:
        taken = {n["path"] for i, n in self.state["notes"].items() if i != own_id}
        base, n = rel[:-3], 2
        while rel in taken or (self._abs(rel).exists() and own_id is None):
            rel, n = f"{base} ({n}).md", n + 1
        return rel

    # ------------------------------------------------------------ Nexus side: read + render
    def _doc_row(self, oid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM native_documents WHERE doc_id = ? AND scope = ?",
                              (oid, self.scope)).fetchone()
        return dict(r) if r else None

    def _entity(self, oid: str) -> dict | None:
        r = self.conn.execute(
            "SELECT o.type, o.display_name, a.payload, a.revision, a.verification FROM objects o"
            " JOIN aspects a ON a.object_id = o.object_id AND a.name = 'entity' AND a.is_current = 1"
            " WHERE o.object_id = ? AND o.scope = ?", (oid, self.scope)).fetchone()
        if r is None:
            return None
        p = json.loads(r["payload"])
        return {"type": r["type"], "name": p.get("name") or r["display_name"],
                "aliases": p.get("aliases") or [], "description": p.get("description") or "",
                "revision": r["revision"], "verification": r["verification"]}

    def _name_of(self, oid: str) -> str:
        r = self.conn.execute("SELECT display_name, type FROM objects WHERE object_id = ?", (oid,)).fetchone()
        return (r["display_name"] if r else oid) or oid

    def _link_or_name(self, oid: str) -> str:
        n = self.state["notes"].get(oid)
        if n:
            stem = Path(n["path"]).stem
            if getattr(self, "_stem_count", {}).get(stem, 1) > 1:
                return f"[[{n['path'][:-3]}|{stem}]]"     # same name elsewhere: link by path
            return f"[[{stem}]]"
        r = self.conn.execute("SELECT type, display_name FROM objects WHERE object_id = ?", (oid,)).fetchone()
        kind = {"folder": "Drive 文件夹", "document": "Drive 快照"}.get(r["type"] if r else "", "")
        if oid.startswith("reg/"):
            kind = "登记表"
        name = (r["display_name"] if r else oid) or oid
        return f"{name}（{kind}）" if kind else name

    def _relations(self, oid: str, linked: set[str]) -> list[dict]:
        rows = self.conn.execute(
            "SELECT relation_id, from_id, relation_type, to_id, verification, provenance_refs, extraction_run"
            " FROM relations WHERE (from_id = ? OR to_id = ?) AND expired_at IS NULL AND scope = ?"
            " ORDER BY relation_type, relation_id", (oid, oid, self.scope)).fetchall()
        out = []
        for r in rows:
            if r["from_id"] == oid and r["relation_type"] == "LINKS_TO" and r["to_id"] in linked:
                continue                       # the [[link]] in the text already shows it
            if r["from_id"].startswith("ent/folder/") or r["to_id"].startswith("ent/folder/"):
                continue                       # Drive folders are the vault's directories
            out.append(dict(r))
        return out[:MAX_REL_LINES]

    def _rel_line(self, oid: str, r: dict) -> str:
        mark = ""
        if r["verification"] != "VERIFIED":
            tier = review.classify_relation(self.conn, r["from_id"], r["relation_type"], r["to_id"],
                                            json.loads(r["provenance_refs"] or "[]"))["tier"]
            mark = "（AI·需核原文）" if tier == review.GATE else "（AI·未核验）"
        if r["from_id"] == oid:
            return f"> - {r['relation_type']} → {self._link_or_name(r['to_id'])}{mark} %%r:{r['relation_id']}%%"
        return f"> - ← {r['relation_type']} {self._link_or_name(r['from_id'])}{mark} %%r:{r['relation_id']}%%"

    def _linked_ids(self, text: str) -> set[str]:
        out = set()
        for name in links_in(text):
            t = self.resolve_link(name)
            if t:
                out.add(t)
        return out

    def render(self, oid: str, keep: list | None = None, rel: str | None = None) -> dict | None:
        """Current Nexus state of one object as note text. None = gone (archived / missing).
        ``rel``: the note's path; when its file name is not the title, the title goes in front matter."""
        keep = keep or []
        stem = Path(rel).stem if rel else None
        if oid.startswith("ndoc/"):
            d = self._doc_row(oid)
            if d is None or d["archived_at"]:
                return None
            r = self.nd.read(oid)
            blocks = [{"node_id": n["node_id"], "kind": n["kind"], "depth": n["depth"],
                       "text": n["title"] if n["kind"] == "section" else n["text"]} for n in r["nodes"]]
            body = render_blocks(blocks)
            title, rev = r["title"], r["revision"]
            fm = [("nexus_id", oid), ("nexus_revision", rev)]
            if (stem if stem is not None else safe_name(title)) != title:
                fm.append(("title", title))
            extra: dict = {"blocks": blocks, "title": title}
        elif oid.startswith("reg/"):
            m = self._register(oid)
            if m is None:
                return None
            body, title = m["body"], m["title"]
            rev = ",".join(f"{a}@{r}" for a, r in sorted(m["revs"].items()))
            fm = [("nexus_id", oid), ("nexus_type", m["type"])]
            if (stem if stem is not None else safe_name(title)) != title:
                fm.append(("title", title))
            fm += [(p, v) for p, _, _, v in m["fields"]]
            if m["readonly"]:
                fm += [("nexus_readonly", "只读：正本在 CORE REGISTER（Google Sheet），这里改了不会写回"),
                       ("register_version", f"v{m['version']}" if m["version"] else None)]
            keep = [(k, v) for k, v in keep if k in ("tags", "cssclasses")]   # every other property is a cell
            extra = {"title": title, "body": body, "fields": [list(f) for f in m["fields"]], "revs": m["revs"]}
        elif is_mirror(oid):
            m = self._mirror(oid)
            if m is None:
                return None
            body, title, rev = m["body"], m["title"], m["version"]
            fm = [("nexus_id", oid), ("nexus_type", m["type"])]
            if (stem if stem is not None else safe_name(title)) != title:
                fm.append(("title", title))
            fm += m["fm"]
            extra = {"title": title, "body": body}
        else:
            e = self._entity(oid)
            if e is None:
                return None
            body, title, rev = e["description"], e["name"], e["revision"]
            fm = [("nexus_id", oid), ("nexus_type", e["type"]), ("nexus_revision", rev)]
            if (stem if stem is not None else safe_name(title)) != title:
                fm.append(("title", title))
            fm.append(("aliases", e["aliases"]))
            if e["verification"] != "VERIFIED":
                fm.append(("nexus_status", "AI 写入·未核验"))
            extra = {"name": title, "aliases": e["aliases"], "description": e["description"]}
        rels = self._relations(oid, self._linked_ids(body))
        text = build_front_matter(fm, keep) + "\n" + (body + "\n" if body else "")
        if rels:
            text += "\n" + "\n".join([CALLOUT_HEAD] + [self._rel_line(oid, r) for r in rels]) + "\n"
        return {"text": text, "title": title, "revision": rev, "rels": [r["relation_id"] for r in rels],
                **extra}

    def default_path(self, oid: str, title: str) -> str:
        if is_mirror(oid):
            return self._mirror_path(oid, title)
        if oid.startswith("ent/"):
            return fit_path([ENTITY_DIR, oid.split("/")[1]], title)
        return fit_path([DOC_DIR], title)

    def resolve_link(self, name: str) -> str | None:
        name = name.strip()
        hits = {i for i, n in self.state["notes"].items()
                if Path(n["path"]).stem == name or n["path"][:-3] == name}
        if len(hits) == 1:
            return hits.pop()
        if not hits:
            rows = self.conn.execute("SELECT DISTINCT object_id FROM entity_aliases WHERE alias_norm = ?"
                                     " AND scope = ?", (re.sub(r"\s+", " ", name).strip().lower(),
                                                        self.scope)).fetchall()
            ids = {r[0] for r in rows if r[0] in self.state["notes"]}
            if len(ids) == 1:
                return ids.pop()
        return None

    # ------------------------------------------------------------ local note -> parts
    def parse_note(self, rel: str, text: str) -> dict:
        entries, body = split_front_matter(text)
        body, rel_ids = strip_callout(body)
        stem = Path(rel).stem
        fm_title = fm_get(entries, "title")
        same = isinstance(fm_title, str) and name_matches(fm_title, stem)
        title = fm_title if same else stem
        aliases = fm_get(entries, "aliases")
        if isinstance(aliases, str):
            aliases = [aliases]
        return {"entries": entries, "id": fm_get(entries, "nexus_id"),
                "conflict_of": fm_get(entries, "nexus_conflict_of"), "title": title, "body": body,
                "rel_ids": rel_ids, "aliases": [str(a) for a in (aliases or []) if a is not None]}

    # ------------------------------------------------------------ one cycle
    def sync_once(self, pull: bool = True, full: bool = False) -> dict:
        """One pass. ``pull``: also look at Nexus; ``full``: re-render every note (else only notes
        whose fingerprint changed)."""
        stats = {"pushed": 0, "pulled": 0, "created_here": 0, "created_there": 0, "archived": 0,
                 "conflicts": 0, "restored": 0, "migrated": 0, "relations_added": 0, "relations_removed": 0,
                 "errors": 0}
        notes = self.state["notes"]
        self._tree_cache = None
        files = self.scan()
        by_path = {n["path"]: oid for oid, n in notes.items()}
        texts: dict[str, str] = {}
        unsettled = False
        for rel, (age, stat) in files.items():
            if age < SETTLE_SECONDS:
                unsettled = True
                continue
            oid = by_path.get(rel)
            if oid is not None and notes[oid].get("stat") == stat:
                continue                       # untouched since we last wrote or read it
            try:
                texts[rel] = self._read(rel)
            except (OSError, UnicodeDecodeError) as exc:
                log("read_failed", path=rel, error=str(exc))
                unsettled = True
        # renames: a tracked id now found at another path
        for rel, text in texts.items():
            if rel in by_path:
                continue
            pid = self.parse_note(rel, text)["id"]
            if isinstance(pid, str) and pid in notes and notes[pid]["path"] not in files:
                log("renamed", id=pid, old=notes[pid]["path"], new=rel)
                notes[pid]["path"] = rel
                notes[pid]["stat"] = None
        by_path = {n["path"]: oid for oid, n in notes.items()}
        # local deletions (guarded against an unmounted / half-synced vault)
        missing = [oid for oid, n in notes.items() if n["path"] not in files]
        if unsettled:
            missing = []                       # an unsettled file may be a renamed note: wait
        if notes and len(missing) > max(3, MASS_DELETE_GUARD * len(notes)):
            log("mass_delete_refused", missing=len(missing), tracked=len(notes))
            missing = []
        for oid in missing:
            self._local_deleted(oid, stats)
        # new local notes
        for rel, text in texts.items():
            if rel in by_path:
                continue
            parsed = self.parse_note(rel, text)
            if parsed["conflict_of"] or (isinstance(parsed["id"], str) and parsed["id"] in notes):
                continue
            if files[rel][0] < NEW_NOTE_SETTLE:
                continue
            if self.state["failed"].get(rel) == text:
                continue
            self._create_there(rel, text, parsed, stats)
        # tracked notes: push / pull / merge
        self._fps = self._fingerprints() if pull else {}
        for oid in list(notes):
            n = notes.get(oid)
            if n is None or n["path"] not in files or files[n["path"]][0] < SETTLE_SECONDS:
                continue
            local = texts.get(n["path"])
            if local is None and not pull:
                continue
            if local is None and not full and n.get("fp") is not None and n["fp"] == self._fps.get(oid):
                continue                       # nothing changed on either side
            try:
                self._sync_note(oid, local if local is not None else n["base"], pull, stats)
            except NativeError as exc:
                stats["errors"] += 1
                log("sync_failed", id=oid, path=n["path"], **exc.as_dict())
        # new on the Nexus side
        if pull:
            new = [oid for oid in self._remote_ids()
                   if oid not in notes and oid not in self.state["tombstones"]]
            planned = {}
            for oid in new:                    # paths first, so links between new notes resolve
                title = self._title_of(oid)
                if title is not None:
                    planned[oid] = self._free_path(self.default_path(oid, title))
                    notes[oid] = {"path": planned[oid]}
            if planned:                        # stem counts and path signature include the new notes
                self._fps = self._fingerprints()
            for oid, rel in planned.items():
                self._create_here(oid, rel, stats)
            self._ensure_bases()
        self.save_state()
        return stats

    def _remote_ids(self) -> list[str]:
        docs = [r[0] for r in self.conn.execute(
            "SELECT doc_id FROM native_documents WHERE scope = ? AND archived_at IS NULL ORDER BY doc_id",
            (self.scope,))]
        ents = [r[0] for r in self.conn.execute(
            "SELECT o.object_id FROM objects o WHERE o.object_id LIKE 'ent/%' AND o.scope = ?"
            " AND EXISTS (SELECT 1 FROM aspects a WHERE a.object_id = o.object_id AND a.name = 'entity'"
            " AND a.is_current = 1) ORDER BY o.object_id", (self.scope,))
            if r[0].split("/")[1] not in SKIP_ENTITY_TYPES]
        mirrors = [r[0] for r in self.conn.execute(
            "SELECT object_id FROM objects WHERE scope = ? AND ((object_id LIKE 'doc/%' AND type = 'document')"
            " OR object_id LIKE 'reg/%') ORDER BY object_id", (self.scope,))]
        return docs + ents + mirrors

    def _remember(self, oid: str, rel: str, rendered: dict, text: str) -> None:
        note = {"path": rel, "base": text, "revision": rendered["revision"], "rels": rendered["rels"],
                "title": rendered["title"], "stat": self._stat(rel),
                "fp": getattr(self, "_fps", {}).get(oid)}
        old = self.state["notes"].get(oid, {})
        note["link_rels"] = old.get("link_rels", {})
        if oid.startswith("ndoc/"):
            note["blocks"] = rendered["blocks"]
        elif oid.startswith("ent/"):
            note.update(name=rendered["name"], aliases=rendered["aliases"], description=rendered["description"])
        elif oid.startswith("reg/"):
            note.update(fields=rendered["fields"], revs=rendered["revs"], body=rendered["body"])
        self.state["notes"][oid] = note

    def _title_of(self, oid: str) -> str | None:
        if oid.startswith("ndoc/"):
            r = self.conn.execute("SELECT title FROM native_revisions v JOIN native_documents d ON"
                                  " d.doc_id = v.doc_id AND d.current_revision = v.revision WHERE d.doc_id = ?",
                                  (oid,)).fetchone()
            return r[0] if r else None
        if is_mirror(oid):
            r = self.conn.execute("SELECT display_name FROM objects WHERE object_id = ?", (oid,)).fetchone()
            return (r[0] or oid) if r else None
        e = self._entity(oid)
        return e["name"] if e else None

    def _create_here(self, oid: str, rel: str, stats: dict) -> None:
        r = self.render(oid, None, rel)
        if r is None:
            self.state["notes"].pop(oid, None)
            return
        self._write(rel, r["text"])
        self._remember(oid, rel, r, r["text"])
        stats["created_here"] += 1
        log("pulled_new", id=oid, path=rel)

    def _create_there(self, rel: str, text: str, parsed: dict, stats: dict) -> None:
        parts = rel.split("/")
        try:
            if parts[0] == REG_DIR and len(parts) == 4:
                cells = self._props(parsed)
                if parsed["body"].strip():
                    cells["notes"] = parsed["body"].strip()
                local_id = parsed["title"].split("｜")[0].strip()
                out = regedit.add_row(self.nd, f"reg/{parts[1].lower()}", parts[2], local_id, OWNER, fields=cells,
                                      label=parsed["title"] if "｜" in parsed["title"] else None)
                oid = out["object_id"]
            elif parts[0] == ENTITY_DIR:
                if len(parts) != 3:
                    raise NativeError(Status.INVALID_REQUEST,
                                      f"an entity note goes in {ENTITY_DIR}/<type>/<name>.md")
                out = self.nd.create_object(self.scope, parts[1], parsed["title"], OWNER,
                                            aliases=parsed["aliases"], description=parsed["body"] or None)
                oid = out["object_id"]
            else:
                blocks = parse_blocks(parsed["body"])
                if not blocks:
                    return                     # empty note: wait for content
                out = self.nd.create(self.scope, _spec(parsed["title"], blocks), OWNER,
                                     message="obsidian: new note")
                oid = out["doc_id"]
        except NativeError as exc:
            self.state["failed"][rel] = text
            stats["errors"] += 1
            log("create_failed", path=rel, **exc.as_dict())
            return
        self.state["failed"].pop(rel, None)
        stats["created_there"] += 1
        log("pushed_new", id=oid, path=rel)
        self.state["notes"][oid] = {"path": rel, "link_rels": {}}
        self._after_push(oid, rel, parsed, None, stats)

    def _local_deleted(self, oid: str, stats: dict) -> None:
        n = self.state["notes"].pop(oid)
        if oid.startswith("ndoc/"):
            d = self._doc_row(oid)
            if d and not d["archived_at"]:
                self.nd.archive(oid, True, OWNER)
                stats["archived"] += 1
                log("archived", id=oid, path=n["path"])
        else:
            self.state["tombstones"].append(oid)
            log("note_deleted", id=oid, path=n["path"],
                note="the object stays in Nexus; its note is no longer synced")

    def _sync_note(self, oid: str, local: str, pull: bool, stats: dict) -> None:
        n = self.state["notes"][oid]
        parsed = self.parse_note(n["path"], local)
        if is_mirror(oid):
            local_changed = local != n["base"]
        else:
            local_changed = local != n["base"] or parsed["title"] != n["title"]   # edited or renamed
        if not local_changed and not pull:
            return
        remote = self.render(oid, parsed["entries"], n["path"])
        if remote is None:
            if oid.startswith("ndoc/") and local_changed:
                self.nd.archive(oid, False, OWNER)
                log("unarchived", id=oid, reason="edited in Obsidian after it was archived")
                remote = self.render(oid, parsed["entries"], n["path"])
            else:
                self._move_to_trash(n["path"])
                del self.state["notes"][oid]
                log("removed_here", id=oid, path=n["path"])
                return
        if local_changed and is_mirror(oid):
            if self._readonly(oid):
                return self._mirror_local_change(oid, parsed, local, remote, stats)
            return self._push_register(oid, parsed, remote, local, stats)
        if not local_changed:
            if remote["text"] != n["base"]:
                rel = n["path"]
                if remote["title"] != n.get("title") and not name_matches(remote["title"], Path(rel).stem):
                    folder = rel.rsplit("/", 1)[0] + "/" if "/" in rel else ""
                    new_rel = self._free_path(f"{folder}{safe_name(remote['title'], max(20, MAX_REL_PATH - len(folder) - 3))}.md", oid)
                    self._abs(rel).unlink(missing_ok=True)
                    log("renamed_here", id=oid, old=rel, new=new_rel)
                    rel = new_rel
                if rel != n["path"]:
                    remote = self.render(oid, parsed["entries"], rel)
                self._write(rel, remote["text"])
                self._remember(oid, rel, remote, remote["text"])
                stats["pulled"] += 1
                log("pulled", id=oid, path=rel, revision=remote["revision"])
            else:
                n["fp"] = self._fps.get(oid)
                n["stat"] = self._stat(n["path"])
            return
        self._push(oid, parsed, remote, local, stats)

    def _mirror_local_change(self, oid: str, parsed: dict, local: str, remote: dict, stats: dict) -> None:
        """A read-only note was edited: relation lines deleted from the callout are honoured (the
        relations are Nexus's own); any other change is kept as a side copy and the note restored."""
        n = self.state["notes"][oid]
        if parsed["rel_ids"] is not None or CALLOUT_HEAD not in local:
            for rid in set(n.get("rels", [])) - (parsed["rel_ids"] or set()):
                try:
                    self.nd.remove_relation(rid, OWNER)
                    stats["relations_removed"] += 1
                    log("relation_removed", id=oid, relation=rid)
                except NativeError as exc:
                    log("relation_remove_failed", relation=rid, **exc.as_dict())
        theirs = self.parse_note(n["path"], remote["text"])
        if oid.startswith("doc/") and parsed["body"].strip() != (remote["body"] or "").strip():
            return self._migrate_on_edit(oid, parsed, remote, stats)
        if parsed["body"].strip() != (remote["body"] or "").strip() or self._props(parsed) != self._props(theirs):
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            rel = n["path"]
            copy = f"{rel[:-3]} (本地改动 {stamp}).md"
            entries, body = split_front_matter(local)
            keep = [(k, v) for k, v in entries if not k.startswith("nexus_")]
            self._write(copy, build_front_matter([("nexus_conflict_of", oid)], keep) + "\n" + body)
            stats["restored"] += 1
            log("readonly_restored", id=oid, path=rel, local_copy=copy)
        r = self.render(oid, parsed["entries"], n["path"])
        self._write(n["path"], r["text"])
        self._remember(oid, n["path"], r, r["text"])
        self.save_state()

    def _migrate_on_edit(self, oid: str, parsed: dict, remote: dict, stats: dict) -> None:
        """The text of a Drive snapshot note was edited: the document moves into Nexus. Revision 1 of
        the new native document is the snapshot as shown, revision 2 is the edit; the note keeps its
        path and becomes an ordinary two-way document note."""
        n = self.state["notes"][oid]
        rel = n["path"]
        m = self._mirror(oid)
        drive_blocks = parse_blocks(remote["body"] or "")
        if not drive_blocks:
            drive_blocks = [{"kind": "paragraph", "depth": 0, "text": "（Drive 快照为空）"}]
        out = drive_migrate.migrate(self.nd, self.scope, oid, _spec(remote["title"], drive_blocks), OWNER,
                                    snapshot=dict(m["fm"]).get("snapshot") if m else None)
        new = out["doc_id"]
        first = self.nd.read(new, 1 if not out["already"] else None)
        base = [{"node_id": x["node_id"], "kind": x["kind"], "depth": x["depth"],
                 "text": x["title"] if x["kind"] == "section" else x["text"]} for x in first["nodes"]]
        mine = parse_blocks(parsed["body"])
        if mine:
            ops, _ = diff_ops(base, mine)
            if ops:
                self.nd.edit(new, first["revision"], ops, OWNER, message="obsidian: first edit after leaving Drive")
        del self.state["notes"][oid]
        self.state["tombstones"].append(oid)
        self.state["notes"][new] = {"path": rel, "link_rels": {}}
        drive_keys = ("source", "snapshot", "doc_status", "register_version")
        parsed = dict(parsed, entries=[(k, v) for k, v in parsed["entries"] if k not in drive_keys])
        stats["migrated"] = stats.get("migrated", 0) + 1
        log("drive_migrated", drive=oid, native=new, path=rel, relations_moved=out["relations_moved"])
        self._after_push(new, rel, parsed, None, stats)

    def _push_register(self, oid: str, parsed: dict, remote: dict, local: str, stats: dict) -> None:
        """Cells changed in a migrated register row: properties (and the body as the ``notes`` cell)
        become field edits on the row's aspects. A cell changed on both sides is a conflict."""
        n = self.state["notes"][oid]
        base = {p: (a, k, v) for p, a, k, v in n.get("fields", [])}
        theirs = {p: (a, k, v) for p, a, k, v in remote["fields"]}
        mine = self._props(parsed)
        primary = next((a for a in PRIMARY_ASPECTS if a in remote["revs"]), None)
        edits: dict[str, tuple[dict, list]] = {}
        clash = False

        def put(aspect, key, value):
            s, u = edits.setdefault(aspect, ({}, []))
            if value is None:
                u.append(key)
            else:
                s[key] = value
        for p, (a, k, v) in base.items():
            if p in mine and mine[p] == v:
                continue
            clash |= p not in theirs or theirs[p][2] != v
            put(a, k, mine.get(p))
        for p, val in mine.items():
            if p in base:
                continue
            if p in theirs:
                clash |= theirs[p][2] != val
                put(theirs[p][0], theirs[p][1], val)
            elif primary:
                put(primary, p, val)
        body_base = (n.get("body") or "").strip()
        if parsed["body"].strip() != body_base and primary:
            clash |= (remote["body"] or "").strip() != body_base
            put(primary, "notes", parsed["body"].strip())
        if clash:
            return self._conflict(oid, local, remote, stats)
        try:
            for aspect, (s, u) in edits.items():
                regedit.edit_fields(self.nd, oid, aspect, OWNER, expected_revision=remote["revs"].get(aspect),
                                    set_fields=s, unset=u)
        except NativeError as exc:
            if exc.code == Status.VERSION_CONFLICT:
                return self._conflict(oid, local, remote, stats)
            raise
        if edits:
            stats["pushed"] += 1
            log("pushed", id=oid, path=n["path"], cells=sorted(k for s, u in edits.values() for k in [*s, *u]))
        self._after_push(oid, n["path"], parsed, n, stats)

    def _push(self, oid: str, parsed: dict, remote: dict, local: str, stats: dict) -> None:
        n = self.state["notes"][oid]
        if oid.startswith("ndoc/"):
            blocks = parse_blocks(parsed["body"])
            if not blocks:
                log("empty_note_kept", id=oid, path=n["path"], note="a document needs text; not pushed")
                return
            ops, touched = diff_ops(n["blocks"], blocks)
            title = parsed["title"] if parsed["title"] != n["title"] else None
            if ops or title is not None:
                base = n["revision"]
                if remote["revision"] != base:
                    theirs = {c["node_id"] for c in self.nd.diff(oid, base, remote["revision"])["changes"]}
                    title_clash = title is not None and remote["title"] != n["title"]
                    if touched & theirs or title_clash:
                        return self._conflict(oid, local, remote, stats)
                    base = remote["revision"]
                    log("merged", id=oid, base=n["revision"], onto=remote["revision"])
                try:
                    self.nd.edit(oid, base, ops, OWNER, message="obsidian", title=title)
                except NativeError as exc:
                    if exc.code in (Status.VERSION_CONFLICT, Status.NOT_FOUND, Status.INVALID_REQUEST):
                        return self._conflict(oid, local, remote, stats)
                    raise
                stats["pushed"] += 1
                log("pushed", id=oid, path=n["path"], ops=len(ops))
        else:
            mine = {"name": parsed["title"], "aliases": parsed["aliases"], "description": parsed["body"].strip()}
            base = {"name": n["name"], "aliases": n["aliases"], "description": (n["description"] or "").strip()}
            theirs = {"name": remote["name"], "aliases": remote["aliases"],
                      "description": (remote["description"] or "").strip()}
            change = {k: v for k, v in mine.items() if v != base[k]}
            if change:
                if any(theirs[k] != base[k] for k in change):
                    return self._conflict(oid, local, remote, stats)
                try:
                    self.nd.update_object(oid, OWNER, expected_revision=remote["revision"], **change)
                except NativeError as exc:
                    if exc.code == Status.VERSION_CONFLICT:
                        return self._conflict(oid, local, remote, stats)
                    raise
                stats["pushed"] += 1
                log("pushed", id=oid, path=n["path"], fields=sorted(change))
        self._after_push(oid, n["path"], parsed, n, stats)

    def _after_push(self, oid: str, rel: str, parsed: dict, old: dict | None, stats: dict) -> None:
        """Relations deleted from the callout, [[links]] -> LINKS_TO, then re-render the note."""
        if old is not None and parsed["rel_ids"] is not None:
            for rid in set(old.get("rels", [])) - parsed["rel_ids"]:
                try:
                    self.nd.remove_relation(rid, OWNER)
                    stats["relations_removed"] += 1
                    log("relation_removed", id=oid, relation=rid)
                except NativeError as exc:
                    log("relation_remove_failed", relation=rid, **exc.as_dict())
        self._sync_links(oid, parsed["body"], stats)
        r = self.render(oid, parsed["entries"], rel)
        local_now = self._read(rel) if self._abs(rel).exists() else ""
        if r["text"] != local_now:
            self._write(rel, r["text"])
        self._remember(oid, rel, r, r["text"])
        self.save_state()                      # a write to Nexus is never repeated after a crash

    def _sync_links(self, oid: str, body: str, stats: dict) -> None:
        n = self.state["notes"][oid]
        link_rels: dict = n.setdefault("link_rels", {})
        wanted: dict[str, str | None] = {}           # target -> node id of the first paragraph
        node_of = {}
        if oid.startswith("ndoc/"):
            for nd_ in self.nd.read(oid)["nodes"]:
                if nd_["kind"] == "paragraph":
                    for name in links_in(nd_["text"]):
                        node_of.setdefault(name, nd_["node_id"])
        for name in links_in(body):
            t = self.resolve_link(name)
            if t and t != oid:
                wanted.setdefault(t, node_of.get(name))
        for t, node in wanted.items():
            if t in link_rels:
                continue
            exists = self.conn.execute("SELECT relation_id FROM relations WHERE from_id = ? AND to_id = ?"
                                       " AND relation_type = 'LINKS_TO' AND expired_at IS NULL",
                                       (oid, t)).fetchone()
            if exists:
                continue
            ev = [{"doc_id": oid, "node_id": node}] if node else None
            out = self.nd.add_relation(self.scope, oid, "LINKS_TO", t, OWNER, evidence=ev,
                                       note="[[link]] written in Obsidian")
            link_rels[t] = out["relation_id"]
            stats["relations_added"] += 1
            log("relation_added", id=oid, to=t, relation=out["relation_id"])
        for t in [t for t in link_rels if t not in wanted]:
            rid = link_rels.pop(t)
            live = self.conn.execute("SELECT 1 FROM relations WHERE relation_id = ? AND expired_at IS NULL",
                                     (rid,)).fetchone()
            if live:
                self.nd.remove_relation(rid, OWNER)
                stats["relations_removed"] += 1
                log("relation_removed", id=oid, relation=rid, reason="[[link]] removed")

    def _conflict(self, oid: str, local: str, remote: dict, stats: dict) -> None:
        n = self.state["notes"][oid]
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        entries, body = split_front_matter(local)
        rel = n["path"]
        copy = f"{rel[:-3]} (冲突 {stamp}).md"
        keep = [(k, v) for k, v in entries if not k.startswith("nexus_")]
        self._write(copy, build_front_matter([("nexus_conflict_of", oid)], keep) + "\n" + body)
        self._write(rel, remote["text"])
        self._remember(oid, rel, remote, remote["text"])
        stats["conflicts"] += 1
        log("conflict", id=oid, path=rel, local_copy=copy, nexus_revision=remote["revision"])

    def _move_to_trash(self, rel: str) -> None:
        src = self._abs(rel)
        if src.exists():
            dst = self.vault / ".trash" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)


def _spec(title: str, blocks: list[dict]) -> dict:
    """Blocks (normalised depths) -> the nested spec NativeDocs.create takes."""
    root: list = []
    stack: list[tuple[int, list]] = [(-1, root)]      # (section depth, its children)
    for b in blocks:
        while stack[-1][0] >= b["depth"] or (b["kind"] == "paragraph" and stack[-1][0] >= b["depth"] - 1
                                             and stack[-1][0] != b["depth"] - 1):
            stack.pop()
        if b["kind"] == "section":
            node = {"kind": "section", "title": b["text"], "children": []}
            stack[-1][1].append(node)
            stack.append((b["depth"], node["children"]))
        else:
            stack[-1][1].append({"kind": "paragraph", "body": b["text"]})
    return {"title": title, "children": root}


# ================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nexus.obsidian")
    ap.add_argument("--db", required=True)
    ap.add_argument("--scope", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between local scans")
    ap.add_argument("--pull-every", type=float, default=30.0, help="seconds between Nexus checks")
    ap.add_argument("command", choices=["once", "run"])
    a = ap.parse_args(argv)
    Path(a.vault).mkdir(parents=True, exist_ok=True)
    conn = connect(a.db)
    conn.execute("PRAGMA busy_timeout = 10000")       # nexus-main and DSH write to the same file
    init_schema(conn)
    bridge = Bridge(conn, a.scope, Path(a.vault), Path(a.state))
    if a.command == "once":
        log("cycle", **bridge.sync_once(pull=True, full=True))
        return 0
    last_pull, pulls = 0.0, 0
    while True:
        pull = time.time() - last_pull >= a.pull_every
        try:
            if pull:
                pulls += 1
            stats = bridge.sync_once(pull=pull, full=pull and pulls % FULL_RENDER_EVERY == 0)
            if any(stats.values()):
                log("cycle", **stats)
        except Exception as exc:                # keep the loop alive; the next cycle retries
            log("cycle_failed", error=f"{type(exc).__name__}: {exc}")
            bridge.state = bridge._load_state()  # never carry a half-done cycle forward
        if pull:
            last_pull = time.time()
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
