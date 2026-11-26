"""Split a plain-text document export into anchored sections.

Anchor format (stable under edits elsewhere in the document)::

    h<heading-number>.<ordinal-within-heading>-<sha256[:8] of the paragraph>

e.g. ``h2.3-a1b2c3d4`` is the third paragraph under heading "二｜…" whose
normalised text hashes to ``a1b2c3d4…``. Paragraphs before the first heading
use ``h0``. The hash part lets a reader detect that the cited text changed;
the heading/ordinal part lets a human find it. Legacy ``pNNN`` (global
ordinal) and the bare hash are recorded as aliases.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}

# "03｜…", "2｜…", "二｜…", "十一｜…" on a short line.
HEADING = re.compile(r"^\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[｜|]\s*(\S.*)$")
MAX_HEADING_CHARS = 60
# Page break written by nexus.files for paged files (PDF): "⟦page 12⟧". The brackets are U+27E6/7,
# which no Drive text export produces, so existing documents keep their anchors. Page n becomes
# heading n, so a page's lines are anchored h<n>.<ordinal>-<sha8> and their path ends "> Page n".
PAGE_MARK = re.compile(r"^⟦page (\d{1,5})⟧$")


def _cn_to_int(label: str) -> int:
    if label.isdigit():
        return int(label)
    if label == "十":
        return 10
    if label.startswith("十"):
        return 10 + _CN_DIGITS[label[1]]
    if len(label) == 3 and label[1] == "十":
        return _CN_DIGITS[label[0]] * 10 + _CN_DIGITS[label[2]]
    if len(label) == 2 and label[1] == "十":
        return _CN_DIGITS[label[0]] * 10
    return _CN_DIGITS[label]


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("﻿", "")).strip()


def text_sha(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Section:
    anchor: str
    legacy: str            # pNNN, global ordinal (1-based)
    heading_no: int
    ordinal: int
    heading: str           # heading line text ('' for the preamble)
    heading_path: str      # "<title> > <heading>"
    body: str
    content_sha256: str


def split_sections(text: str) -> tuple[str, list[Section]]:
    """Return (title, sections). One section per non-empty line.

    Heading lines are themselves sections (ordinal 0) so they can be cited.
    """
    lines = [ln.replace("﻿", "").rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        return "", []
    title = normalise(lines[0])
    heading_no, heading, ordinal = 0, "", 0
    out: list[Section] = []
    used: set[str] = set()
    for global_index, raw in enumerate(lines, start=1):
        body = normalise(raw)
        page = PAGE_MARK.match(body)
        match = HEADING.match(body) if not page and len(body) <= MAX_HEADING_CHARS else None
        if page:
            heading_no, ordinal = int(page.group(1)), 0
            heading = body = f"Page {page.group(1)}"
        elif match and global_index > 1:
            heading_no, heading, ordinal = _cn_to_int(match.group(1)), body, 0
        else:
            ordinal += 1
        sha = text_sha(body)
        anchor = f"h{heading_no}.{ordinal}-{sha[:8]}"
        if anchor in used:  # identical line repeated at the same position class
            anchor = f"{anchor}-{global_index}"
        used.add(anchor)
        path = f"{title} > {heading}" if heading else title
        out.append(Section(anchor=anchor, legacy=f"p{global_index:03d}",
                           heading_no=heading_no, ordinal=ordinal, heading=heading,
                           heading_path=path, body=body, content_sha256=sha))
    return title, out
