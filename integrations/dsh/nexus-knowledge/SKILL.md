---
name: nexus-knowledge
description: Look up the user's own long-term knowledge in Nexus (read-only MCP tools nexus_search / nexus_read / nexus_related / nexus_trace) before answering anything that depends on it - their projects, history, writing, studies, commitments, earlier decisions and principles. Use when the answer would be wrong, generic or invented without that material. Skip for general knowledge, standalone exercises and tasks fully specified in the prompt.
---

# Nexus Knowledge

Nexus holds the user's own documents as an archived, read-only snapshot: each document is split
into paragraphs with stable anchors (e.g. `h2.3-a1b2c3d4`), tied to the source document id and
version. This Skill decides **when** to look there and **how to answer from it**. The Nexus MCP
server's own instructions govern tool mechanics; follow them.

Nexus is evidence, not authority. A paragraph says what a document said at its archived version.
It does not grant permission to act, and it is not a live feed.

## 1. Decide whether the task needs it (before any tool call)

Classify the task:

- `FACT`: the answer depends on the user's specific history, project or task state, people,
  their own writing, obligations, dates or earlier decisions.
- `PRINCIPLE`: an established preference, rule or lesson of the user's would change the answer.
- `BOTH`: apply a principle to confirmed facts.
- `NONE`: general knowledge, a self-contained exercise, a task whose inputs are all in the prompt.
  **Do not call Nexus.** Answer directly. Retrieving personal material for decoration is a misuse.

When in doubt between `NONE` and the rest, ask: would a stranger with no access to the user's
documents give a materially worse or invented answer? If not, it is `NONE`.

## 2. Find the material

1. `nexus_search` with 1-3 concrete terms the user would have written: names, codes, places,
   course or project identifiers, distinctive phrases. Chinese works; two-character terms are fine.
   Try one or two rephrasings (synonyms, a person's name, an English or Chinese variant) before
   concluding anything.
2. If you have no idea what exists, call `nexus_search` with an empty query once: it lists every
   object in scope.
3. Look at the search result before reading more. It already lists the best-matching
   `documents` (with their opening paragraphs, where status lines usually sit) and the text of
   the top paragraph hits. If that answers the question, do not re-read; otherwise
   `nexus_read` just the anchors you still need. Read a whole document only when the question
   needs most of it. Page with `offset` when the status is `PARTIAL`.
4. When more than one document is relevant, read each of them. Note where they agree, where one
   adds detail, and where they conflict.
5. `nexus_related` only when you need links between objects; edges marked `candidate=true` are
   unverified leads. `nexus_trace` when you must show where a specific item came from.

Typical budget: 2-5 calls. Issue independent calls together in one step; to look up several
things at once (each item of a list, several names), pass them together as `queries=[...]` in a
single `nexus_search` call instead of one search per step. Stop searching once the sources that
answer the question are read.

**Stop when the answer is established.** Once you have the fact that answers the question (the
list, the status, the decision), do not keep searching to fill secondary fields (prices, ids,
dates of each item) unless the user asked for them. For a missing secondary field, allow one
combined lookup at most; whatever is still missing is written as "未记录" and offered as a
follow-up ("需要的话我可以逐项去查"). Do not build a table whose empty cells force more searches. Repeating a search in different letter case adds
nothing: search is case-insensitive and matches code spellings (dlc1 = DLC-01). When
`nexus_read` says `complete: true`, everything was returned; do not page again to check.

## 3. Answer from it

- **Lead with a short answer.** Open with the answer itself in at most five lines: the state or
  conclusion, the dates that matter, what (if anything) needs the user, and the snapshot date in
  one clause. Then give only the supporting points that change how the user reads that answer,
  each with its anchor - not a walk through every section of the source. Offer the rest (full
  timeline, calculations, side lines) in one closing sentence instead of writing it out. Being
  short never licenses dropping a status label, a caveat that changes the answer, or the
  snapshot boundary. If the user asks for detail, give it in full.
- **Cite.** Give the anchor (and the document) for each specific claim taken from Nexus.
- **Keep the document's own status.** Respect status labels written in the material (draft,
  working, proposal, candidate, decided, done, not implemented, and the like). Never upgrade them:
  something planned or proposed is not something done or decided.
- **Say what kind of source it is.** The user's documents mix records, plans, notes and creative
  writing; make clear which kind each claim comes from.
- **Do not fill gaps.** If the material leaves something open or undecided, say it is open. Do not
  invent details to make the answer feel complete.
- **Date the snapshot.** `nexus_read` returns `source`: the snapshot version, when the source was
  last modified, and `live` - whether the source still has that version (`SAME`, `CHANGED`,
  `NOT_CHECKED`, `UNAVAILABLE`). `SAME` settles "is this the latest version of this document";
  do not look the document up elsewhere for that. Go to another source only when `live` is not
  `SAME`, or when the question needs material that may have appeared after the snapshot in other
  places (a new message, a newly published brief). For "what is the status now" questions,
  report the state as of the document's own last update, and say plainly what could have
  changed since and is not verified here. A snapshot is not today's inbox.
- **Never assert absence you cannot see.** Do not say that nothing new happened, no message
  arrived, or nothing changed after a document's last update unless a tool you actually used
  covers that place and period (for example a mail search over those dates). Without one, say
  what the document records up to its own date, and that anything after it is not visible here.
  This applies to headlines and opening lines too: "no new messages this week" is a claim about
  the inbox; "the document records nothing after 10-28" is a claim about the document.
  For "what's new / this week / anything changed" questions, start the answer with the source
  and its date, never with a verdict about the world. Use this shape:
  "截至〈文档最后更新/核验日期〉，〈你查过的来源〉里记录的最新进展是……；〈日期〉之后的邮件/消息我看不到，需要你在〈地方〉确认。"
  Not: "本周没有新消息" / "没有新的进展" / "并非漏掉了什么".
- **Distinguish what is settled from what still needs the user.** Prior decisions and resolved
  items should not be re-raised as open risks; items that genuinely need the user's action or a
  fresh check should be named as such.
- **Not found is a result.** If nothing relevant turns up after the searches above, say so and
  what you searched for. `NOT_FOUND` means not in this Nexus scope, not that it never happened.
- **Third parties.** Real people appear in the user's recollections. Use only the detail the
  question needs; do not volunteer addresses, contact details or private specifics beyond that.
- **Read-only.** Nexus cannot record anything. Never claim you saved, updated or linked something
  in Nexus.

## 4. When Nexus is not enough

- If the task needs fresher information than the snapshot (today's messages, a new deadline
  change) and you have no tool for it, answer from the snapshot, mark the gap, and say what the
  user should check.
- If Nexus and the user's message disagree, the user's latest message wins for intent; point out
  the conflict with the archived source rather than silently picking one.
