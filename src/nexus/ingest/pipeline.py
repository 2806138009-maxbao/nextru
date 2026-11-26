"""Standard document ingestion: fetched text -> SourceRef + anchored sections.

What one ``ingest_document`` call writes (all inside one transaction):

* ``objects``          the document object (created once, never renamed here)
* ``source_refs``      one row per fetched native revision
* ``source_heads``     the revision the adapter actually observed
* ``source_sections``  one row per paragraph, stable anchor + heading path + hash
* ``section_aliases``  legacy ``pNNN`` and bare 8-char hash -> anchor
* ``aspects``          a DRAFT/UNVERIFIED ``content`` aspect (never Current);
                       earlier content revisions become SUPERSEDED
* ``observations``     one ``document_paragraph`` per section, so the existing
                       ``get_research_evidence`` reads the document unchanged

Re-ingesting a revision whose content hash is already stored writes nothing (if an archive is
given and that revision is not yet sealed, it is archived and sealed).

Nothing is overwritten. The same revision label arriving with different content keeps the
earlier rows, is stored under ``<ref>~<sha8>`` and is recorded in ``conflicts``.

With ``archive`` (a ``nexus.archive.Archive``) the exported bytes are written to the write-once
archive first and the source_ref is sealed (``archive_path``): the schema triggers then refuse any
edit, delete or replacement of that ref, its sections and aliases.

Nothing here writes to the source system.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..db import Store, canonical_json, sha256_text, utcnow
from .sections import split_sections
from .sources import FetchedDocument

if TYPE_CHECKING:
    from ..archive import Archive

PARAGRAPH_DIMENSION = "document_paragraph"


def default_object_id(doc: FetchedDocument) -> str:
    return f"doc/{doc.doc_id[:16]}"


def source_ref_id(doc: FetchedDocument) -> str:
    system = re.sub(r"[^a-z0-9]+", "-", doc.source_system.lower()).strip("-")
    return f"src/{system}/{doc.doc_id}@v{doc.version}"


def ingest_document(store: Store, doc: FetchedDocument, scope: str,
                    object_id: str | None = None, object_type: str = "document",
                    display_name: str | None = None,
                    archive: "Archive | None" = None) -> dict[str, Any]:
    conn = store.conn
    object_id = object_id or default_object_id(doc)
    base_ref = source_ref_id(doc)
    content_sha = sha256_text(doc.text)

    same_revision = [dict(r) for r in conn.execute(
        "SELECT source_ref_id, content_sha256, archive_path FROM source_refs"
        " WHERE source_system = ? AND native_id = ? AND revision = ? ORDER BY source_ref_id",
        (doc.source_system, doc.doc_id, doc.version))]
    match = next((r for r in same_revision if r["content_sha256"] == content_sha), None)
    if match:
        out = {"status": "UNCHANGED", "object_id": object_id, "source_ref_id": match["source_ref_id"],
               "archive_path": match["archive_path"]}
        if archive is not None and not match["archive_path"]:
            rec = _archive(archive, doc, content_sha)
            with store.tx():
                conn.execute("UPDATE source_refs SET archive_path = ? WHERE source_ref_id = ?",
                             (rec.path, match["source_ref_id"]))
            out.update(archive_path=rec.path, sealed_now=True)
        return out

    rec = _archive(archive, doc, content_sha) if archive is not None else None
    ref_id = base_ref if not same_revision else f"{base_ref}~{content_sha[:8]}"
    tag = ref_id.rsplit("@", 1)[1]                      # "v<version>" or "v<version>~<sha8>"
    title, sections = split_sections(doc.text)
    now = utcnow()
    with store.tx():
        if store.get_scope(scope) is None:
            store.add_scope(scope, "research", "created by nexus.ingest")
        if store.get_object(object_id) is None:
            store.add_object(object_id, object_type, display_name or doc.name or title, scope, now)

        store.add_source_ref(ref_id, doc.source_system, doc.doc_id, doc.version,
                             doc.web_view, "full document (plain-text export)", scope,
                             retrieved_at=now, status="OK", content=doc.text,
                             archive_path=rec.path if rec else None)
        store.set_source_head(doc.source_system, doc.doc_id, doc.version, now)
        if same_revision:  # same revision label, different bytes: keep both, record the conflict
            candidates = same_revision + [{"source_ref_id": ref_id, "content_sha256": content_sha,
                                           "archive_path": rec.path if rec else None}]
            conn.execute(
                "INSERT INTO conflicts(conflict_id, object_id, aspect, description, candidates,"
                " source_ref_ids, detected_at) VALUES (?,?,?,?,?,?,?)",
                (f"conflict/{object_id}/content/{tag}", object_id, "content",
                 f"revision {doc.version} of {doc.doc_id} was received with different content",
                 canonical_json(candidates), canonical_json([c["source_ref_id"] for c in candidates]), now))

        for index, sec in enumerate(sections, start=1):
            store.add_section(ref_id, sec.anchor, sec.heading or title, sec.body,
                              heading_path=sec.heading_path, ordinal=index,
                              content_sha256=sec.content_sha256)
        seen_hash_alias: set[str] = set()
        for sec in sections:
            store.add_section_alias(ref_id, sec.legacy, sec.anchor)
            short = sec.content_sha256[:8]
            if short not in seen_hash_alias:  # repeated paragraphs: the first one keeps the alias
                seen_hash_alias.add(short)
                conn.execute("INSERT INTO section_aliases(source_ref_id, alias, anchor)"
                             " VALUES (?,?,?)", (ref_id, short, sec.anchor))

        prior = conn.execute(
            "SELECT version_id, revision FROM aspects WHERE object_id = ? AND name = 'content'",
            (object_id,)).fetchall()
        revision = max((r["revision"] for r in prior), default=0) + 1
        conn.execute("UPDATE aspects SET lifecycle = 'SUPERSEDED', valid_to = ?"
                     " WHERE object_id = ? AND name = 'content' AND lifecycle = 'DRAFT'",
                     (now, object_id))
        payload = {"doc_id": doc.doc_id, "name": doc.name, "version": doc.version,
                   "modified_time": doc.modified_time, "chars": len(doc.text),
                   "paragraphs": len(sections), "retrieved_at": now,
                   "anchor_format": "h<heading>.<ordinal>-<sha8>",
                   "source_ref_id": ref_id, "content_sha256": content_sha,
                   "revision_id": doc.revision_id, "archive_path": rec.path if rec else None}
        store.add_aspect(f"{object_id}:content:r{revision}", object_id, "content", payload,
                         schema_version="1", lifecycle="DRAFT", verification="UNVERIFIED",
                         revision=revision, is_current=False, valid_from=doc.modified_time,
                         valid_to=None, source_ref_id=ref_id, updated_at=now)

        for index, sec in enumerate(sections, start=1):
            detail = {"anchor": sec.anchor, "legacy_anchor": sec.legacy,
                      "heading_path": sec.heading_path, "doc_id": doc.doc_id,
                      "doc_version": doc.version, "text_review_status": "DOCUMENT_TEXT"}
            store.add_observation(f"obs/{object_id}@{tag}/{index:04d}", object_id,
                                  PARAGRAPH_DIMENSION, sec.body, "TO_TEST",
                                  json.dumps(detail, ensure_ascii=False), [ref_id])

    return {"status": "INGESTED", "object_id": object_id, "source_ref_id": ref_id,
            "content_revision": revision, "sections": len(sections),
            "headings": len({s.heading_no for s in sections}), "title": title,
            "archive_path": rec.path if rec else None,
            "version_conflict": [r["source_ref_id"] for r in same_revision]}


def _archive(archive: "Archive", doc: FetchedDocument, content_sha: str):
    rec = archive.put(doc)
    if rec.text_sha256 != content_sha:  # cannot happen unless the archive and the store disagree on hashing
        raise RuntimeError("archive text hash differs from the store's content hash")
    return rec
