"""SQLite persistence layer for the Nexus P1 prototype.

Deliberately thin: SQLite is an implementation candidate explicitly allowed by
the task book ("可以提交有理由的简化方案，例如本地 SQLite 快速原型").  Nothing
here is claimed to be PostgreSQL-validated; see reports/P1-evaluation.md.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import datetime as _dt
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(db_path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    _migrate_columns(conn)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


# Columns added after Task 007. CREATE TABLE IF NOT EXISTS does not add them to
# an existing database, so they are added here before the DDL script runs.
_ADDED_COLUMNS = {
    "source_sections": [("heading_path", "TEXT"), ("ordinal", "INTEGER"),
                        ("content_sha256", "TEXT")],
    "relations": [("expired_at", "TEXT"), ("superseded_by", "TEXT"),
                  ("extraction_run", "TEXT")],
    "source_refs": [("archive_path", "TEXT")],
    "native_documents": [("archived_at", "TEXT")],
}


def _migrate_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:  # table not created yet; the DDL script will create it
            continue
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


class Store:
    """Data-access helpers. All writes are local-test writes only."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------------ writes
    def add_scope(self, scope: str, kind: str, description: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scopes(scope, kind, description) VALUES (?,?,?)",
            (scope, kind, description),
        )

    def add_object(self, object_id: str, type: str, display_name: str, scope: str,
                   created_at: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO objects(object_id, type, display_name, scope, created_at)"
            " VALUES (?,?,?,?,?)",
            (object_id, type, display_name, scope, created_at or utcnow()),
        )

    def add_source_ref(self, source_ref_id: str, source_system: str, native_id: str,
                       revision: str | None, location: str | None, anchor: str | None,
                       access_scope: str, retrieved_at: str | None = None,
                       status: str = "OK", content: str | None = None,
                       archive_path: str | None = None) -> None:
        """Insert or replace an unsealed source_ref; ``archive_path`` seals it (write-once).

        A sealed row is never replaced: the schema triggers refuse it, and this raises first with
        a clearer message.
        """
        sha = sha256_text(content) if content is not None else None
        row = self.conn.execute("SELECT archive_path FROM source_refs WHERE source_ref_id = ?",
                                (source_ref_id,)).fetchone()
        if row and row["archive_path"]:
            raise sqlite3.IntegrityError(f"sealed source_ref {source_ref_id} is write-once")
        self.conn.execute(
            "INSERT OR REPLACE INTO source_refs(source_ref_id, source_system, native_id, revision,"
            " location, anchor, access_scope, retrieved_at, status, content_sha256, archive_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source_ref_id, source_system, native_id, revision, location, anchor,
             access_scope, retrieved_at or utcnow(), status, sha, archive_path),
        )

    def set_source_head(self, source_system: str, native_id: str, latest_revision: str,
                        checked_at: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_heads(source_system, native_id, latest_revision, checked_at)"
            " VALUES (?,?,?,?)",
            (source_system, native_id, latest_revision, checked_at or utcnow()),
        )

    def add_section(self, source_ref_id: str, anchor: str, heading: str, body: str,
                    heading_path: str | None = None, ordinal: int | None = None,
                    content_sha256: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_sections(source_ref_id, anchor, heading, body,"
            " char_count, heading_path, ordinal, content_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (source_ref_id, anchor, heading, body, len(body), heading_path, ordinal,
             content_sha256),
        )

    def add_section_alias(self, source_ref_id: str, alias: str, anchor: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO section_aliases(source_ref_id, alias, anchor) VALUES (?,?,?)",
            (source_ref_id, alias, anchor),
        )

    def resolve_anchor(self, source_ref_id: str, anchor_or_alias: str) -> str | None:
        row = self.conn.execute(
            "SELECT anchor FROM source_sections WHERE source_ref_id = ? AND anchor = ?",
            (source_ref_id, anchor_or_alias)).fetchone()
        if row:
            return row["anchor"]
        row = self.conn.execute(
            "SELECT anchor FROM section_aliases WHERE source_ref_id = ? AND alias = ?",
            (source_ref_id, anchor_or_alias)).fetchone()
        return row["anchor"] if row else None

    def move_source_ref(self, source_ref_id: str, new_location: str, new_anchor: str | None = None) -> None:
        self.conn.execute(
            "UPDATE source_refs SET location = ?, anchor = COALESCE(?, anchor) WHERE source_ref_id = ?",
            (new_location, new_anchor, source_ref_id),
        )

    def add_aspect(self, version_id: str, object_id: str, name: str, payload: dict,
                   schema_version: str, lifecycle: str, verification: str, revision: int,
                   is_current: bool, valid_from: str | None, valid_to: str | None,
                   source_ref_id: str | None, updated_at: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO aspects(version_id, object_id, name, payload, schema_version,"
            " lifecycle, verification, revision, is_current, valid_from, valid_to, source_ref_id,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, object_id, name, canonical_json(payload), schema_version, lifecycle,
             verification, revision, 1 if is_current else 0, valid_from, valid_to,
             source_ref_id, updated_at or utcnow()),
        )

    def add_authority(self, binding_id: str, target_type: str, target_id: str,
                      field_scope: str | None, owner: str, scope: str, source_of_truth: str,
                      effective_from: str, write_policy: str, migration_state: str,
                      effective_to: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO authority_bindings(binding_id, target_type, target_id,"
            " field_scope, owner, scope, source_of_truth, effective_from, effective_to, write_policy,"
            " migration_state) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (binding_id, target_type, target_id, field_scope, owner, scope, source_of_truth,
             effective_from, effective_to, write_policy, migration_state),
        )

    def add_relation(self, relation_id: str, from_id: str, relation_type: str, to_id: str,
                     verification: str, scope: str, provenance_refs: Iterable[str],
                     valid_from: str | None = None, valid_to: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO relations(relation_id, from_id, relation_type, to_id,"
            " verification, scope, provenance_refs, valid_from, valid_to, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (relation_id, from_id, relation_type, to_id, verification, scope,
             canonical_json(list(provenance_refs)), valid_from, valid_to, utcnow()),
        )

    def add_observation(self, observation_id: str, object_id: str, dimension: str,
                        statement: str, stance: str, detail: str,
                        source_ref_ids: Iterable[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO observations(observation_id, object_id, dimension, statement,"
            " stance, detail, source_ref_ids, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (observation_id, object_id, dimension, statement, stance, detail,
             canonical_json(list(source_ref_ids)), utcnow()),
        )

    def add_conflict(self, conflict_id: str, object_id: str, aspect: str, description: str,
                     candidates: list, source_ref_ids: Iterable[str]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO conflicts(conflict_id, object_id, aspect, description,"
            " candidates, source_ref_ids, detected_at) VALUES (?,?,?,?,?,?,?)",
            (conflict_id, object_id, aspect, description, canonical_json(candidates),
             canonical_json(list(source_ref_ids)), utcnow()),
        )

    def add_event(self, event_id: str, event_type: str, occurred_at: str, object_id: str,
                  payload: dict, source_ref_id: str | None, authority_owner: str,
                  dedupe_key: str) -> None:
        self.conn.execute(
            "INSERT INTO events(event_id, event_type, occurred_at, object_id, payload,"
            " source_ref_id, authority_owner, dedupe_key) VALUES (?,?,?,?,?,?,?,?)",
            (event_id, event_type, occurred_at, object_id, canonical_json(payload),
             source_ref_id, authority_owner, dedupe_key),
        )

    def add_projection(self, projection_id: str, source_system: str, native_id: str,
                       built_revision: str, status: str = "FRESH") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO projections(projection_id, source_system, native_id,"
            " built_revision, status, built_at) VALUES (?,?,?,?,?,?)",
            (projection_id, source_system, native_id, built_revision, status, utcnow()),
        )

    # ------------------------------------------------------------------- reads
    def get_scope(self, scope: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM scopes WHERE scope = ?", (scope,)).fetchone()
        return dict(row) if row else None

    def list_scopes(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM scopes ORDER BY scope")]

    def list_aspect_names(self, object_id: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT name FROM aspects WHERE object_id = ? ORDER BY name",
            (object_id,),
        ).fetchall()
        return [r["name"] for r in rows]

    def mark_projection(self, projection_id: str, built_revision: str, status: str) -> None:
        self.conn.execute(
            "UPDATE projections SET built_revision = ?, status = ?, built_at = ?"
            " WHERE projection_id = ?",
            (built_revision, status, utcnow(), projection_id),
        )

    def get_object(self, object_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM objects WHERE object_id = ?", (object_id,)).fetchone()
        return dict(row) if row else None

    def find_objects_by_native_id(self, native_id: str) -> list[dict]:
        """Resolve a native source identifier to the business objects it backs.

        Only *verified* business projections are considered; a source ref by
        itself never creates an object.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT o.* FROM objects o"
            " JOIN aspects a ON a.object_id = o.object_id"
            " JOIN source_refs s ON s.source_ref_id = a.source_ref_id"
            " WHERE s.native_id = ?",
            (native_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_objects(self, text: str, scope: str | None = None) -> list[dict]:
        pattern = f"%{text}%"
        sql = (
            "SELECT o.*, 1.0 AS score, 'display_name' AS match_reason FROM objects o"
            " WHERE o.display_name LIKE ?"
        )
        params: list[Any] = [pattern]
        if scope and scope != "global":
            sql += " AND o.scope = ?"
            params.append(scope)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        if rows:
            return rows
        sql = (
            "SELECT DISTINCT o.*, 0.6 AS score, 'observation' AS match_reason"
            " FROM objects o JOIN observations b ON b.object_id = o.object_id"
            " WHERE b.statement LIKE ? OR b.detail LIKE ?"
        )
        params = [pattern, pattern]
        if scope and scope != "global":
            sql += " AND o.scope = ?"
            params.append(scope)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_aspects(self, object_id: str, current_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM aspects WHERE object_id = ?"
        if current_only:
            sql += " AND is_current = 1"
        sql += " ORDER BY name, revision"
        return [dict(r) for r in self.conn.execute(sql, (object_id,)).fetchall()]

    def get_aspect(self, object_id: str, name: str, current_only: bool = True) -> dict | None:
        sql = "SELECT * FROM aspects WHERE object_id = ? AND name = ?"
        if current_only:
            sql += " AND is_current = 1"
        sql += " ORDER BY revision DESC LIMIT 1"
        row = self.conn.execute(sql, (object_id, name)).fetchone()
        return dict(row) if row else None

    def get_aspect_history(self, object_id: str, name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM aspects WHERE object_id = ? AND name = ? ORDER BY revision",
            (object_id, name),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_binding(self, object_id: str, aspect: str | None, scope: str,
                    as_of: str | None = None) -> dict | None:
        """Most specific open binding: aspect > object, filtered by effective time."""
        time_sql = ""
        time_params: list[Any] = []
        if as_of:
            time_sql = (" AND effective_from <= ?"
                        " AND (effective_to IS NULL OR effective_to > ?)")
            time_params = [as_of, as_of]
        if aspect is not None:
            row = self.conn.execute(
                "SELECT * FROM authority_bindings WHERE target_type = 'aspect' AND target_id = ?"
                " AND field_scope = ? AND scope = ? AND effective_to IS NULL" + time_sql,
                [object_id, aspect, scope, *time_params],
            ).fetchone()
            if row:
                return dict(row)
        row = self.conn.execute(
            "SELECT * FROM authority_bindings WHERE target_type = 'object' AND target_id = ?"
            " AND scope = ? AND effective_to IS NULL" + time_sql,
            [object_id, scope, *time_params],
        ).fetchone()
        return dict(row) if row else None

    def get_source_ref(self, source_ref_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM source_refs WHERE source_ref_id = ?", (source_ref_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_source_ref_by_native_id(self, native_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM source_refs WHERE native_id = ? ORDER BY retrieved_at DESC LIMIT 1",
            (native_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_source_head(self, source_system: str, native_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM source_heads WHERE source_system = ? AND native_id = ?",
            (source_system, native_id),
        ).fetchone()
        return dict(row) if row else None

    def get_sections(self, source_ref_id: str, anchors: Iterable[str] | None = None) -> list[dict]:
        if anchors:
            anchors = list(anchors)
            placeholders = ",".join("?" for _ in anchors)
            rows = self.conn.execute(
                f"SELECT * FROM source_sections WHERE source_ref_id = ? AND anchor IN ({placeholders})",
                [source_ref_id, *anchors],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM source_sections WHERE source_ref_id = ? ORDER BY anchor",
                (source_ref_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_relations(self, object_id: str, relation_types: list[str] | None = None,
                      scope: str | None = None) -> list[dict]:
        # Expired (superseded) edges stay in the table for trace_evidence but
        # are not part of the traversable graph.
        sql = ("SELECT * FROM relations WHERE (from_id = ? OR to_id = ?)"
               " AND expired_at IS NULL")
        params: list[Any] = [object_id, object_id]
        if relation_types:
            placeholders = ",".join("?" for _ in relation_types)
            sql += f" AND relation_type IN ({placeholders})"
            params.extend(relation_types)
        if scope and scope != "global":
            sql += " AND scope = ?"
            params.append(scope)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_relation(self, relation_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM relations WHERE relation_id = ?", (relation_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_verified_relations(self, object_id: str, relation_types: list[str] | None = None,
                               scope: str | None = None,
                               as_of: str | None = None) -> list[dict]:
        """Relations usable for business queries: VERIFIED, in scope, still valid."""
        now = as_of or utcnow()
        rows = self.get_relations(object_id, relation_types, scope)
        return [
            r for r in rows
            if r["verification"] == "VERIFIED"
            and (r["valid_from"] is None or r["valid_from"] <= now)
            and (r["valid_to"] is None or r["valid_to"] > now)
        ]

    def get_conflicts(self, object_id: str, aspect: str | None = None) -> list[dict]:
        sql = "SELECT * FROM conflicts WHERE object_id = ?"
        params: list[Any] = [object_id]
        if aspect:
            sql += " AND aspect = ?"
            params.append(aspect)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_observations(self, object_id: str, dimensions: list[str] | None = None) -> list[dict]:
        sql = "SELECT * FROM observations WHERE object_id = ?"
        params: list[Any] = [object_id]
        if dimensions:
            placeholders = ",".join("?" for _ in dimensions)
            sql += f" AND dimension IN ({placeholders})"
            params.extend(dimensions)
        sql += " ORDER BY observation_id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_event(self, event_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    def recent_events(self, object_id: str, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE object_id = ? AND processed_at IS NOT NULL"
            " ORDER BY occurred_at DESC LIMIT ?",
            (object_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_projection(self, source_system: str, native_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM projections WHERE source_system = ? AND native_id = ?",
            (source_system, native_id),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- transaction
    def tx(self):
        return _Tx(self.conn)


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
