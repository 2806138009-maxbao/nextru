# Document archive and sealed sources

Nexus stores documents as anchored paragraphs. The archive keeps the exported original of every
ingested revision next to that, so every anchor can be traced to fixed bytes.

## Layout (`nexus.archive`, format `nexus-archive/1`)
```
<root>/manifest.jsonl                                  append-only, one line per archived entry
<root>/<source_system>/<doc_id>/v<version>__<sha12>/
    export.txt     plain-text export, bytes exactly as received (BOM, CRLF kept)
    export.docx    lossless rendition when the source gave one (Drive fetches try it)
    meta.json      doc id, name, version, Docs revision id, modified time, link, archived_at,
                   per-file sha256/bytes/mime, text_sha256, version_conflict_with, notes
```
- Write-once: staged in a temp dir, renamed into place, files set read-only. The same bytes again
  are a no-op; an existing entry that fails verification is never reused.
- `sha12` comes from the raw export hash, so the same version label with different bytes gets a
  second directory and both are flagged; nothing is replaced.

## Two hashes
- `files[*].sha256`: the stored bytes.
- `text_sha256`: `sha256_text(decode_export(bytes))`, i.e. UTF-8, BOM removed, CRLF/CR -> LF. This
  is `source_refs.content_sha256`, the same convention every store was built with
  (`Path.read_text(encoding="utf-8-sig")`). Both `LocalTextSource` and `DriveSource` now use
  `decode_export`, so the two adapters give the same hash for the same export.

## Sealing in the store
`ingest_document(..., archive=Archive(root))` (CLI: `--archive <root>`) archives first, then writes
the source_ref with `archive_path`. For a sealed ref, schema triggers refuse changes to its
identity, hash and archive pointer, deleting or replacing it, and editing, deleting or replacing
its sections and aliases. `status`, `location` and `access_scope` stay updatable. Unsealed refs
(fixtures, older stores) behave as before. Re-ingesting an unsealed revision with an archive seals
it.

The same revision label with new content no longer deletes and rebuilds: the earlier rows stay, the
new content goes under `<ref>~<sha8>`, and a row is written to `conflicts`.

## Commands
```powershell
$env:PYTHONPATH = "src"
python -X utf8 -m nexus.archive add --root <root> --index <dir>/index.json   # archive local exports
python -X utf8 -m nexus.archive verify --root <root>                         # recompute every hash
python -X utf8 -m nexus.ingest --db <db> --scope <scope> --index <dir>/index.json --archive <root>
python -X utf8 -m nexus.ingest --db <db> --scope <scope> --doc <id> --token-dir <dir> --archive <root>
```

## Limits
- Read-only attributes and triggers stop accidents, not a determined local user (anyone can drop a
  trigger or clear an attribute). Tamper evidence is `verify` against the manifest; keep a copy of
  the manifest or the root elsewhere if that matters.
- The `.docx` rendition and Docs revision id are best effort and untested against live Drive
  (only a fake HTTP layer). The revision id is taken from the last `revisions.list` entry after the
  export, so an edit between the two calls can make it point one revision ahead; that case is noted
  when the times differ.
- Exports already on disk have no `.docx` and no revision id.
- The Creative Pilot store (`p1_creative_pilot/db/pilot.sqlite`, built by `build_pilot.py`) hashed
  the text with the BOM kept, so its `content_sha256` values do not match `text_sha256`; Task 008
  stores do match.
