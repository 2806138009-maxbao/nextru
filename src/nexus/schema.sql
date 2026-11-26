-- Nexus Core v0.1 — P1 read-only prototype schema (SQLite dialect)
-- Semantics follow Nexus Core Contract v0.1 §§03–07. This is an implementation
-- candidate, not a frozen production DDL. See docs/implementation-plan.md.
--
-- Invariants enforced at the storage layer:
--   INV-01  at most one current row per (object, aspect)  -> ux_aspect_current
--   INV-01  at most one open AuthorityBinding per scope   -> ux_authority_current
--   Event idempotency                                     -> ux_event_dedupe
--   Referential integrity for objects/relations/sources    -> FK constraints

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scopes (
    scope        TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    description  TEXT
);

CREATE TABLE IF NOT EXISTS objects (
    object_id    TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    display_name TEXT,
    scope        TEXT NOT NULL REFERENCES scopes(scope),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_refs (
    source_ref_id  TEXT PRIMARY KEY,
    source_system  TEXT NOT NULL,
    native_id      TEXT NOT NULL,
    revision       TEXT,
    location       TEXT,
    anchor         TEXT,
    access_scope   TEXT NOT NULL,
    retrieved_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'OK',   -- OK | BLOCKED | MISSING | STALE
    content_sha256 TEXT,
    archive_path   TEXT                          -- write-once archive entry (nexus.archive); NULL = unsealed
);

-- Last revision the adapter has actually observed for a native source.
-- A projection whose revision is behind this head is reported STALE.
CREATE TABLE IF NOT EXISTS source_heads (
    source_system    TEXT NOT NULL,
    native_id        TEXT NOT NULL,
    latest_revision  TEXT NOT NULL,
    checked_at       TEXT NOT NULL,
    PRIMARY KEY (source_system, native_id)
);

-- Simulated on-demand expansion of a SourceRef (section-level retrieval).
CREATE TABLE IF NOT EXISTS source_sections (
    source_ref_id TEXT NOT NULL REFERENCES source_refs(source_ref_id),
    anchor        TEXT NOT NULL,
    heading       TEXT,
    body          TEXT NOT NULL,
    char_count    INTEGER NOT NULL,
    heading_path  TEXT,                    -- Task 008: "<title> > <heading>"
    ordinal       INTEGER,                 -- Task 008: global 1-based position
    content_sha256 TEXT,                   -- Task 008: sha256 of normalised body
    PRIMARY KEY (source_ref_id, anchor)
);

-- Task 008: alternative names for a section anchor (legacy pNNN, bare hash).
CREATE TABLE IF NOT EXISTS section_aliases (
    source_ref_id TEXT NOT NULL,
    alias         TEXT NOT NULL,
    anchor        TEXT NOT NULL,
    PRIMARY KEY (source_ref_id, alias),
    FOREIGN KEY (source_ref_id, anchor) REFERENCES source_sections(source_ref_id, anchor)
);

-- Sealed sources: a source_ref with archive_path set is write-once. Its identity, content hash and
-- archive pointer cannot change, it cannot be deleted or replaced, and its sections and aliases
-- cannot be edited, deleted or replaced. Status / location / access_scope stay updatable (a source
-- can still become BLOCKED or STALE). These guards stop accidental overwrite through the app or
-- plain SQL; anyone who can drop a trigger can bypass them, so tamper evidence comes from the
-- archive hashes (python -m nexus.archive verify), not from these triggers.
CREATE TRIGGER IF NOT EXISTS trg_sealed_ref_update BEFORE UPDATE OF
    source_ref_id, source_system, native_id, revision, content_sha256, archive_path ON source_refs
    WHEN OLD.archive_path IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'sealed source_ref is write-once'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_ref_delete BEFORE DELETE ON source_refs
    WHEN OLD.archive_path IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'sealed source_ref cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_ref_replace BEFORE INSERT ON source_refs
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = NEW.source_ref_id
                 AND archive_path IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'sealed source_ref cannot be replaced'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_section_update BEFORE UPDATE ON source_sections
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = OLD.source_ref_id
                 AND archive_path IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'section of a sealed source is write-once'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_section_delete BEFORE DELETE ON source_sections
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = OLD.source_ref_id
                 AND archive_path IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'section of a sealed source cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_section_replace BEFORE INSERT ON source_sections
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = NEW.source_ref_id
                 AND archive_path IS NOT NULL)
     AND EXISTS (SELECT 1 FROM source_sections WHERE source_ref_id = NEW.source_ref_id
                 AND anchor = NEW.anchor)
BEGIN SELECT RAISE(ABORT, 'section of a sealed source cannot be replaced'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_alias_update BEFORE UPDATE ON section_aliases
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = OLD.source_ref_id
                 AND archive_path IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'alias of a sealed source is write-once'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_alias_delete BEFORE DELETE ON section_aliases
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = OLD.source_ref_id
                 AND archive_path IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'alias of a sealed source cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_alias_replace BEFORE INSERT ON section_aliases
    WHEN EXISTS (SELECT 1 FROM source_refs WHERE source_ref_id = NEW.source_ref_id
                 AND archive_path IS NOT NULL)
     AND EXISTS (SELECT 1 FROM section_aliases WHERE source_ref_id = NEW.source_ref_id
                 AND alias = NEW.alias AND anchor <> NEW.anchor)
BEGIN SELECT RAISE(ABORT, 'alias of a sealed source cannot be repointed'); END;

CREATE TABLE IF NOT EXISTS aspects (
    version_id     TEXT PRIMARY KEY,
    object_id      TEXT NOT NULL REFERENCES objects(object_id),
    name           TEXT NOT NULL,
    payload        TEXT NOT NULL,          -- JSON
    schema_version TEXT NOT NULL,
    lifecycle      TEXT NOT NULL,          -- CURRENT | HISTORICAL | DRAFT | SUPERSEDED
    verification   TEXT NOT NULL,          -- VERIFIED | CASE | INFERENCE | TO_TEST | UNVERIFIED
    revision       INTEGER NOT NULL,
    is_current     INTEGER NOT NULL DEFAULT 0,
    valid_from     TEXT,
    valid_to       TEXT,
    source_ref_id  TEXT REFERENCES source_refs(source_ref_id),
    updated_at     TEXT NOT NULL,
    UNIQUE (object_id, name, revision)
);

-- INV-01 at data level: a single current row per object aspect.
CREATE UNIQUE INDEX IF NOT EXISTS ux_aspect_current
    ON aspects (object_id, name) WHERE is_current = 1;

CREATE TABLE IF NOT EXISTS authority_bindings (
    binding_id      TEXT PRIMARY KEY,
    target_type     TEXT NOT NULL,          -- object | aspect | field
    target_id       TEXT NOT NULL,
    field_scope     TEXT,                   -- aspect or field name; NULL for object-level
    owner           TEXT NOT NULL,
    scope           TEXT NOT NULL,
    source_of_truth TEXT,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    write_policy    TEXT NOT NULL,          -- USER_ONLY | SYSTEM_ONLY | READ_ONLY | PROPOSAL_ONLY
    migration_state TEXT NOT NULL           -- NATIVE | MIRRORED | MIGRATED
);

-- INV-01: one open binding per target/field/scope.
CREATE UNIQUE INDEX IF NOT EXISTS ux_authority_current
    ON authority_bindings (target_type, target_id, IFNULL(field_scope, ''), scope)
    WHERE effective_to IS NULL;

CREATE TABLE IF NOT EXISTS relations (
    relation_id     TEXT PRIMARY KEY,
    from_id         TEXT NOT NULL REFERENCES objects(object_id),
    relation_type   TEXT NOT NULL,
    to_id           TEXT NOT NULL REFERENCES objects(object_id),
    verification    TEXT NOT NULL,          -- VERIFIED | UNVERIFIED | DISPUTED
    scope           TEXT NOT NULL,
    provenance_refs TEXT NOT NULL,          -- JSON array of source_ref_id
    valid_from      TEXT,
    valid_to        TEXT,
    created_at      TEXT NOT NULL,
    expired_at      TEXT,                   -- Task 008: when the system stopped believing it
    superseded_by   TEXT,                   -- Task 008: relation_id that replaced it
    extraction_run  TEXT                    -- Task 008: candidate import run id
);

-- Task 008: normalised alternative names used for entity resolution.
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_norm  TEXT NOT NULL,
    scope       TEXT NOT NULL,
    object_id   TEXT NOT NULL REFERENCES objects(object_id),
    alias       TEXT NOT NULL,
    PRIMARY KEY (alias_norm, scope, object_id)
);

CREATE INDEX IF NOT EXISTS ix_relations_from ON relations (from_id, relation_type);
CREATE INDEX IF NOT EXISTS ix_relations_to   ON relations (to_id, relation_type);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    object_id      TEXT NOT NULL REFERENCES objects(object_id),
    dimension      TEXT NOT NULL,           -- career | commercial | equipment | ai_workflow | operations | tradeoff
    statement      TEXT NOT NULL,
    stance         TEXT NOT NULL,           -- CASE | VERIFIED | INFERENCE | TO_TEST
    detail         TEXT,
    source_ref_ids TEXT NOT NULL,           -- JSON array
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_observations_object ON observations (object_id, dimension);

CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id    TEXT PRIMARY KEY,
    object_id      TEXT NOT NULL,
    aspect         TEXT NOT NULL,
    description    TEXT NOT NULL,
    candidates     TEXT NOT NULL,           -- JSON
    source_ref_ids TEXT NOT NULL,           -- JSON
    detected_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,             -- OFFICIAL_REQUIREMENT | USER_DECISION | SYSTEM_OBSERVED
    occurred_at  TEXT NOT NULL,
    object_id    TEXT,
    payload      TEXT NOT NULL,             -- JSON
    source_ref_id TEXT,
    authority_owner TEXT,                   -- who asserts this event
    dedupe_key   TEXT NOT NULL,
    processed_at TEXT,
    outcome      TEXT                       -- JSON summary when processed
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_event_dedupe ON events (dedupe_key);

-- Facts already known to the system; a new event carrying the same fact is a
-- duplicate and must not re-trigger a decision.
CREATE TABLE IF NOT EXISTS known_facts (
    fingerprint     TEXT PRIMARY KEY,
    fact            TEXT NOT NULL,
    first_event_id  TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projections (
    projection_id  TEXT PRIMARY KEY,
    source_system  TEXT NOT NULL,
    native_id      TEXT NOT NULL,
    built_revision TEXT NOT NULL,
    status         TEXT NOT NULL,           -- FRESH | STALE | REBUILD_REQUIRED
    built_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_log (
    query_id     TEXT PRIMARY KEY,
    operation    TEXT NOT NULL,
    status       TEXT NOT NULL,
    request      TEXT NOT NULL,
    response_chars INTEGER NOT NULL,
    elapsed_ms   REAL NOT NULL,
    created_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------------------------------------
-- Nexus-native knowledge documents (STORAGE x WRITE v0). See nexus/native.py.
--
-- A Drive mirror lives in source_refs / source_sections and stays read-only (sealed). A native
-- document is owned by Nexus: its text lives here, every edit makes a new revision, and the old
-- revisions stay readable. Revisions, trees and blobs are append-only; the only mutable row is
-- native_documents.current_revision, which moves by compare-and-swap (expected revision).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS native_documents (
    doc_id           TEXT PRIMARY KEY REFERENCES objects(object_id),
    scope            TEXT NOT NULL REFERENCES scopes(scope),
    current_revision INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    archived_at      TEXT                   -- set = archived (undo of a create); NULL = active
);

CREATE TABLE IF NOT EXISTS native_blobs (
    sha256 TEXT PRIMARY KEY,
    body   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS native_revisions (
    doc_id         TEXT NOT NULL REFERENCES native_documents(doc_id),
    revision       INTEGER NOT NULL,
    base_revision  INTEGER,                 -- NULL for revision 1
    title          TEXT NOT NULL,
    author         TEXT NOT NULL,
    message        TEXT,
    created_at     TEXT NOT NULL,
    restored_from  INTEGER,                 -- set when this revision restores an older one
    proposal_id    TEXT,                    -- set when an accepted proposal produced it
    tree_sha256    TEXT NOT NULL,           -- hash of the whole tree, for export / backup checks
    PRIMARY KEY (doc_id, revision)
);

CREATE TABLE IF NOT EXISTS native_tree (
    doc_id      TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    node_id     TEXT NOT NULL,              -- stable across revisions
    parent_id   TEXT,                       -- NULL = top level of the document
    position    INTEGER NOT NULL,           -- order among siblings, 0-based
    kind        TEXT NOT NULL,              -- section | paragraph
    title       TEXT,                       -- section heading
    body_sha256 TEXT REFERENCES native_blobs(sha256),   -- paragraph text
    PRIMARY KEY (doc_id, revision, node_id),
    FOREIGN KEY (doc_id, revision) REFERENCES native_revisions(doc_id, revision)
);

CREATE INDEX IF NOT EXISTS ix_native_tree_node ON native_tree (node_id, revision);

CREATE TABLE IF NOT EXISTS native_proposals (
    proposal_id   TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,            -- EDIT | RELATION
    doc_id        TEXT,                     -- EDIT: target document
    base_revision INTEGER,                  -- EDIT: revision the proposer read
    relation_id   TEXT,                     -- RELATION: the UNVERIFIED relation row it created
    payload       TEXT NOT NULL,            -- JSON (ops, or the relation)
    rationale     TEXT,
    evidence      TEXT NOT NULL,            -- JSON array of source_ref_id
    proposer      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    status        TEXT NOT NULL,            -- PROPOSED | ACCEPTED | REJECTED | STALE
    decided_by    TEXT,
    decided_at    TEXT,
    outcome       TEXT                      -- JSON
);

CREATE TRIGGER IF NOT EXISTS trg_native_rev_update BEFORE UPDATE ON native_revisions
BEGIN SELECT RAISE(ABORT, 'native revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_native_rev_delete BEFORE DELETE ON native_revisions
BEGIN SELECT RAISE(ABORT, 'native revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_native_tree_update BEFORE UPDATE ON native_tree
BEGIN SELECT RAISE(ABORT, 'native tree rows are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_native_tree_delete BEFORE DELETE ON native_tree
BEGIN SELECT RAISE(ABORT, 'native tree rows are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_native_blob_update BEFORE UPDATE ON native_blobs
BEGIN SELECT RAISE(ABORT, 'native blobs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_native_blob_delete BEFORE DELETE ON native_blobs
BEGIN SELECT RAISE(ABORT, 'native blobs are append-only'); END;
-- current_revision may only move forward by exactly one.
CREATE TRIGGER IF NOT EXISTS trg_native_doc_rev BEFORE UPDATE OF current_revision ON native_documents
    WHEN NEW.current_revision <> OLD.current_revision + 1
BEGIN SELECT RAISE(ABORT, 'current_revision must advance by one'); END;
CREATE TRIGGER IF NOT EXISTS trg_native_doc_delete BEFORE DELETE ON native_documents
BEGIN SELECT RAISE(ABORT, 'native documents cannot be deleted'); END;

-- ---------------------------------------------------------------------------------------------
-- Universal file store (nexus/files.py). The original bytes live outside the database in a
-- content-addressed, write-once directory (<files-dir>/<sha[:2]>/<sha>); SQLite keeps what they
-- are, where they came from and, through the file's object (objects.type = 'file'), how they relate
-- to everything else. Text a file contains (PDF pages, text files) is ingested like any document,
-- so search, read and relations need nothing new. A file row is never deleted or rewritten.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS files (
    sha256      TEXT PRIMARY KEY,
    object_id   TEXT NOT NULL UNIQUE REFERENCES objects(object_id),
    bytes       INTEGER NOT NULL,
    mime        TEXT NOT NULL,           -- sniffed from the bytes; declared type only as fallback
    name        TEXT NOT NULL,           -- first name it arrived with
    pages       INTEGER,                 -- PDF page count; NULL for unpaged files
    text_chars  INTEGER NOT NULL DEFAULT 0,  -- extracted text ingested (0 = none: image, binary, scan)
    blob_path   TEXT NOT NULL,           -- relative to the files dir
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL
);

-- Every arrival of the same bytes: a Drive file, a chat upload, a local path. Same bytes from two
-- places are one file with two provenance rows.
CREATE TABLE IF NOT EXISTS file_sources (
    sha256        TEXT NOT NULL REFERENCES files(sha256),
    source_system TEXT NOT NULL,         -- google-drive | chat-upload | local | base64
    native_id     TEXT NOT NULL,         -- Drive file id, upload file_id, path or name
    name          TEXT,
    location      TEXT,                  -- web view link when there is one
    received_at   TEXT NOT NULL,
    received_by   TEXT NOT NULL,
    PRIMARY KEY (sha256, source_system, native_id)
);

CREATE TRIGGER IF NOT EXISTS trg_files_update BEFORE UPDATE OF sha256, object_id, bytes, blob_path ON files
BEGIN SELECT RAISE(ABORT, 'file identity is write-once'); END;
CREATE TRIGGER IF NOT EXISTS trg_files_delete BEFORE DELETE ON files
BEGIN SELECT RAISE(ABORT, 'files cannot be deleted'); END;
