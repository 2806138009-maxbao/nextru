"""Compact presentation of nexus_read / nexus_search results (format "compact-1").

Presentation only: the same result dict is rewritten with less repetition, and ``expand_*`` turns it
back into the full form. Nothing is dropped that changes a judgement: anchors, versions, sources,
lifecycles, statuses, paging, conflicts and verification all stay. What goes:

* the SourceRef and lifecycle repeated on every paragraph: stated once, kept per paragraph only
  where a paragraph differs (another source or lifecycle);
* field names repeated on every paragraph / hit / section: lists of fixed-order arrays instead;
* a hit's snippet when the hit already carries the full text that contains it (the only thing
  ``expand_search`` cannot rebuild; ``drop_redundant_snippets`` gives the comparable full form);
* the ``obs/<object_id>`` prefix of a hit's item id (the object id sits next to it);
* in a multi-query search, document fields already shown for an earlier query.

``expand_read(compact_read(x)) == x`` and
``expand_search(compact_search(x)) == drop_redundant_snippets(x)`` for every result these tools
produce (tests and the A/B script check it on real queries).
"""
from __future__ import annotations

import copy
from typing import Any

FORMAT = "compact-1"
SAME = "=above"   # text identical to an earlier hit / head in the same result
# Explained once in the tool descriptions (compact mode), not repeated in every result.
READ_NOTE = ("compact-1: paragraphs are [anchor, text] in order; a third/fourth element is that "
             "paragraph's own source / lifecycle when it differs from paragraph_source / "
             "paragraph_lifecycle. sections are [section, title, paragraphs, first_anchor, digest].")
SEARCH_NOTE = ("compact-1: hits are [object_id, anchor, item, text, snippet] in rank order; item is the "
               "item id after 'obs/<object_id>' (pass the full id to nexus_trace); text is null when "
               "not included, snippet is left out when text contains it, \"=above\" repeats an earlier "
               "hit's text. head/also_matching are [anchor, text]; matching_sections are [section, "
               "title, matches, digest]. A document already shown for an earlier query lists only "
               "what differs.")
_DOC_STATIC = ("name", "type", "source", "card", "sections_total", "digest_note")


# ---------------------------------------------------------------- read
def compact_read(out: dict) -> dict:
    out = copy.deepcopy(out)
    paras = out.get("paragraphs")
    if paras:
        src = _most_common([p.get("source") for p in paras])
        life = _most_common([p.get("lifecycle") for p in paras])
        rows = []
        for p in paras:
            row = [p.get("anchor"), p.get("text")]
            if p.get("source") != src or p.get("lifecycle") != life:
                row += [p.get("source"), p.get("lifecycle")]
            if set(p) - {"anchor", "text", "source", "lifecycle"}:
                return _as_is(out)          # unknown paragraph field: leave untouched
            rows.append(row)
        out["paragraph_source"], out["paragraph_lifecycle"] = src, life
        out["paragraphs"] = rows
    if out.get("sections") and isinstance(out["sections"][0], dict):
        keys = ("section", "title", "paragraphs", "first_anchor", "digest")
        if all(set(s) <= set(keys) for s in out["sections"]):
            out["sections"] = [[s.get(k) for k in keys] for s in out["sections"]]
    for key in ("current_facts", "observations"):
        if out.get(key) == []:
            out.setdefault("empty", []).append(key)
            del out[key]
    out["format"] = FORMAT
    return out


def expand_read(c: dict) -> dict:
    c = copy.deepcopy(c)
    if c.pop("format", None) is None:
        return c
    for key in c.pop("empty", []):
        c[key] = []
    if c.get("_as_is"):
        del c["_as_is"]
        return c
    src, life = c.pop("paragraph_source", None), c.pop("paragraph_lifecycle", None)
    if isinstance(c.get("paragraphs"), list) and c["paragraphs"] and isinstance(c["paragraphs"][0], list):
        c["paragraphs"] = [{"anchor": r[0], "text": r[1], "source": r[2] if len(r) > 2 else src,
                            "lifecycle": r[3] if len(r) > 2 else life} for r in c["paragraphs"]]
    if c.get("sections") and isinstance(c["sections"][0], list):
        keys = ("section", "title", "paragraphs", "first_anchor", "digest")
        c["sections"] = [dict(zip(keys, s)) for s in c["sections"]]
    return c


# -------------------------------------------------------------- search
def compact_search(out: dict) -> dict:
    out = copy.deepcopy(out)
    seen_text: dict[tuple, str] = {}
    seen_docs: dict[str, dict] = {}
    if out.get("mode") == "multi":
        out["results"] = [_compact_one(r, seen_text, seen_docs) for r in out.get("results", [])]
    elif out.get("mode") != "catalog":
        out = _compact_one(out, seen_text, seen_docs)
    else:
        return out
    out["format"] = FORMAT
    return out


def _compact_one(r: dict, seen_text: dict, seen_docs: dict) -> dict:
    if isinstance(r.get("hits"), list):
        rows = []
        if _compactable(r["hits"]):
            for h in r["hits"]:
                oid, item = h.get("object_id"), h.get("item_id")
                pre = f"obs/{oid}"
                item = item[len(pre):] if isinstance(item, str) and oid and item.startswith(pre) else item
                text, snip = h.get("text"), h.get("snippet")
                key = (oid, h.get("anchor"))
                row_text = text
                if text is not None and seen_text.get(key) == text:
                    row_text = SAME
                elif text is not None:
                    seen_text[key] = text
                row = [oid, h.get("anchor"), item, row_text]
                if "text" not in h or not (isinstance(snip, str) and _inside(snip, text)):
                    row.append(snip if "snippet" in h else None)
                rows.append(row)
            r["hits"] = rows
    if isinstance(r.get("documents"), list):
        docs = []
        for d in r["documents"]:
            d = dict(d)
            oid = d.get("object_id")
            if "head" in d:
                d["head"] = [[x.get("anchor"), x.get("text")] for x in d["head"]]
            if "also_matching" in d:
                d["also_matching"] = [[x.get("anchor"), x.get("snippet")] + ([x["section"]] if "section" in x else [])
                                      for x in d["also_matching"]]
            if "matching_sections" in d:
                d["matching_sections"] = [[x.get("section"), x.get("title"), x.get("matches"), x.get("digest")]
                                          for x in d["matching_sections"]]
            first = seen_docs.get(oid)
            if first is None:
                seen_docs[oid] = dict(d)
            else:
                dropped = [k for k in _DOC_STATIC if k in d and k in first and d[k] == first[k]]
                for k in dropped:
                    del d[k]
                if "head" in d and d["head"] == first.get("head"):
                    d["head"] = SAME
                if dropped:
                    d["shown_above"] = dropped
            docs.append(d)
        r["documents"] = docs
    return r


def expand_search(c: dict) -> dict:
    c = copy.deepcopy(c)
    if c.pop("format", None) is None:
        return c
    seen_text: dict[tuple, str] = {}
    seen_docs: dict[str, dict] = {}
    if c.get("mode") == "multi":
        c["results"] = [_expand_one(r, seen_text, seen_docs) for r in c["results"]]
        return c
    return _expand_one(c, seen_text, seen_docs)


def _expand_one(r: dict, seen_text: dict, seen_docs: dict) -> dict:
    if r.get("hits") and isinstance(r["hits"][0], list):
        hits = []
        for row in r["hits"]:
            oid, anchor, item, text = row[:4]
            if isinstance(item, str) and item.startswith(("@", "/")):
                item = f"obs/{oid}{item}"
            key = (oid, anchor)
            if text == SAME:
                text = seen_text[key]
            elif text is not None:
                seen_text[key] = text
            h = {"kind": "paragraph", "object_id": oid, "item_id": item, "anchor": anchor}
            if len(row) > 4:
                h["snippet"] = row[4]
            if text is not None:
                h["text"] = text
            hits.append(h)
        r["hits"] = hits
    for d in r.get("documents", []) or []:
        oid = d.get("object_id")
        for k in d.pop("shown_above", []):
            d[k] = seen_docs[oid][k]
        if d.get("head") == SAME:
            d["head"] = copy.deepcopy(seen_docs[oid]["head"])     # already expanded
        elif isinstance(d.get("head"), list):
            d["head"] = [{"anchor": a, "text": t} for a, t in d["head"]]
        if isinstance(d.get("also_matching"), list):
            d["also_matching"] = [dict(zip(("anchor", "snippet", "section"), x)) for x in d["also_matching"]]
        if isinstance(d.get("matching_sections"), list):
            d["matching_sections"] = [dict(zip(("section", "title", "matches", "digest"), x))
                                      for x in d["matching_sections"]]
        seen_docs.setdefault(oid, dict(d))
    return r


# ------------------------------------------------------------- helpers
def _most_common(values: list) -> Any:
    counts: dict = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=lambda v: (counts[v], -values.index(v)))


def _inside(snippet: str, text: str | None) -> bool:
    """A snippet is redundant when the full text shown with the hit contains it."""
    core = snippet.strip("…")
    return text is not None and bool(core) and core in text


def _compactable(hits: list) -> bool:
    """Hits become arrays only when every hit is a plain paragraph hit; otherwise left as they are."""
    return all(isinstance(h, dict) and h.get("kind") == "paragraph"
               and not set(h) - {"kind", "object_id", "item_id", "anchor", "snippet", "text"} for h in hits)


def drop_redundant_snippets(out: dict) -> dict:
    """The full form minus what compact_search leaves out on purpose (for A/B comparison)."""
    out = copy.deepcopy(out)
    for r in (out.get("results") if out.get("mode") == "multi" else [out]) or []:
        for h in (r.get("hits") or []) if _compactable(r.get("hits") or []) else []:
            if isinstance(h, dict) and "text" in h and isinstance(h.get("snippet"), str)                     and _inside(h["snippet"], h["text"]):
                del h["snippet"]
    return out


def _as_is(out: dict) -> dict:
    out["_as_is"] = True
    out["format"] = FORMAT
    return out
