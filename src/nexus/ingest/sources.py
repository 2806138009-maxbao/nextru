"""Read-only document sources for ingestion.

Two adapters:

* ``LocalTextSource`` — a plain-text export already on disk plus a metadata
  dict (doc_id, name, version, modified_time, web_view). Used for offline,
  reproducible ingestion of previously fetched documents.
* ``DriveSource`` — Google Drive v3 ``files.get`` + ``export text/plain`` using
  an OAuth refresh token directory supplied by the caller. Read-only: it only
  issues GET requests for metadata/export and never writes to Drive. Secrets
  are never logged or returned.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

GOOGLE_DRIVE = "GOOGLE_DRIVE"


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
EMPTY_EXPORT_CHARS = 20   # plain-text exports shorter than this trigger the all-tabs check


def _same_instant(a: str | None, b: str | None, tolerance_s: float = 2.0) -> bool:
    from datetime import datetime
    try:
        ta, tb = (datetime.fromisoformat(x.replace("Z", "+00:00")) for x in (a, b))
    except (AttributeError, ValueError):
        return False
    return abs((ta - tb).total_seconds()) <= tolerance_s


def decode_export(raw: bytes) -> str:
    """Bytes of a plain-text export -> the text Nexus hashes and splits.

    UTF-8, BOM removed, newlines translated like ``Path.read_text`` (CRLF and CR -> LF). This is the
    convention every existing store was built with (``source_refs.content_sha256``); Drive exports
    arrive with a BOM and CRLF, so hashing the raw decode would give a different value for the same
    content. The archive keeps the raw bytes; this is only the derived text.
    """
    return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Rendition:
    """An extra, lossless export of the same revision (e.g. .docx), kept only in the archive."""
    filename: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class FetchedDocument:
    source_system: str
    doc_id: str
    name: str
    version: str
    modified_time: str | None
    web_view: str | None
    text: str
    # Archive inputs (optional; ingestion without an archive ignores them):
    raw: bytes | None = None                 # the plain-text export exactly as received
    revision_id: str | None = None           # source-native revision id (Docs revision), if known
    renditions: tuple[Rendition, ...] = ()   # lossless formats of the same revision
    notes: tuple[str, ...] = ()              # e.g. why a rendition or revision id is missing


class LocalTextSource:
    def __init__(self, text_path: str | Path, meta: dict, source_system: str = GOOGLE_DRIVE):
        self.text_path = Path(text_path)
        self.meta = meta
        self.source_system = source_system

    def fetch(self) -> FetchedDocument:
        raw = self.text_path.read_bytes()
        text = decode_export(raw)
        m = self.meta
        return FetchedDocument(self.source_system, m["doc_id"], m.get("name") or m["doc_id"],
                               str(m["version"]), m.get("modified_time"), m.get("web_view"),
                               text, raw=raw, revision_id=m.get("revision_id"),
                               notes=("local plain-text export; no lossless rendition on disk",))


class DriveSource:
    """Read-only Drive export. ``token_dir`` holds client_secret.json + token.json."""

    API = "https://www.googleapis.com/drive/v3/files/"

    def __init__(self, doc_id: str, token_dir: str | Path, lossless: bool = True):
        self.doc_id = doc_id
        self.token_dir = Path(token_dir)
        self.lossless = lossless

    def _access_token(self) -> str:
        client = json.loads((self.token_dir / "client_secret.json").read_text(encoding="utf-8"))
        client = client.get("installed") or client.get("web") or client
        token = json.loads((self.token_dir / "token.json").read_text(encoding="utf-8"))
        body = urllib.parse.urlencode({
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(client.get("token_uri", "https://oauth2.googleapis.com/token"),
                                     data=body, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]

    def _tabs_text(self, token: str) -> str:
        """Text of every tab (and child tab) via the Docs API, tabs separated by their titles."""
        para = "paragraph(elements(textRun(content)))"
        body = f"documentTab(body(content({para},table(tableRows(tableCells(content({para})))))))"
        fields = f"tabs(tabProperties(title),{body},childTabs(tabProperties(title),{body},childTabs(tabProperties(title),{body})))"
        doc = json.loads(self._get(f"https://docs.googleapis.com/v1/documents/{self.doc_id}"
                                   f"?includeTabsContent=true&fields={urllib.parse.quote(fields, safe='(),')}",
                                   token))

        def blocks(content: list) -> list[str]:
            out = []
            for c in content or []:
                if "paragraph" in c:
                    out.append("".join(e.get("textRun", {}).get("content", "")
                                       for e in c["paragraph"].get("elements", [])))
                for row in (c.get("table") or {}).get("tableRows", []):
                    cells = ["".join(blocks(cell.get("content"))).strip() for cell in row.get("tableCells", [])]
                    out.append("\t".join(cells) + "\n")
            return out

        parts: list[str] = []

        def walk(tabs: list) -> None:
            for tab in tabs or []:
                title = (tab.get("tabProperties") or {}).get("title") or ""
                text = "".join(blocks(((tab.get("documentTab") or {}).get("body") or {}).get("content")))
                if text.strip():
                    parts.append((f"{title}\n" if len(tabs) > 1 or parts else "") + text)
                walk(tab.get("childTabs"))

        walk(doc.get("tabs"))
        return "\n".join(p.rstrip("\n") for p in parts) + "\n" if parts else ""

    @staticmethod
    def _get(url: str, token: str) -> bytes:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()

    def fetch(self) -> FetchedDocument:
        token = self._access_token()
        fields = "id,name,modifiedTime,version,webViewLink"
        meta = json.loads(self._get(f"{self.API}{self.doc_id}?fields={fields}", token))
        raw = self._get(f"{self.API}{self.doc_id}/export?mimeType=text/plain", token)
        text = decode_export(raw)
        renditions: list[Rendition] = []
        notes: list[str] = []
        if len(text.strip()) < EMPTY_EXPORT_CHARS:
            # A near-empty plain-text export can mean the content lives in other document tabs.
            try:
                tabbed = self._tabs_text(token)
                if len(tabbed.strip()) > len(text.strip()):
                    renditions.append(Rendition("export.plain-original.txt", "text/plain; charset=utf-8", raw))
                    raw, text = tabbed.encode("utf-8"), tabbed
                    notes.append("plain-text export was near-empty; text rebuilt from all document tabs "
                                 "(Docs API); the original export is kept as export.plain-original.txt")
                else:
                    notes.append("plain-text export is near-empty and the document tabs hold no more text")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"near-empty export; tab check failed: {type(exc).__name__}")
        revision_id = None
        if self.lossless:
            # Best effort: a missing rendition or revision id is recorded, never fatal.
            try:
                data = self._get(f"{self.API}{self.doc_id}/export?mimeType="
                                 + urllib.parse.quote(DOCX_MIME, safe=""), token)
                renditions.append(Rendition("export.docx", DOCX_MIME, data))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"docx export failed: {type(exc).__name__}")
            try:
                revs = json.loads(self._get(f"{self.API}{self.doc_id}/revisions"
                                            "?fields=revisions(id,modifiedTime)&pageSize=1000", token))
                listed = revs.get("revisions") or []
                if listed:
                    revision_id = listed[-1]["id"]
                    # Drive stamps the file a few ms after the revision (85 ms seen live), so
                    # compare with a tolerance rather than as strings.
                    if not _same_instant(listed[-1].get("modifiedTime"), meta.get("modifiedTime")):
                        notes.append("latest listed revision time differs from file modifiedTime; "
                                     "revision_id may not match the exported content")
                else:
                    notes.append("revisions list empty")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"revisions list failed: {type(exc).__name__}")
        return FetchedDocument(GOOGLE_DRIVE, meta["id"], meta.get("name", self.doc_id),
                               str(meta.get("version")), meta.get("modifiedTime"),
                               meta.get("webViewLink"), text, raw=raw, revision_id=revision_id,
                               renditions=tuple(renditions), notes=tuple(notes))
