# Tool audit: what burns context, measured

Every tool result stays in the conversation for the rest of the session. A 5,000
token result at step 3 is paid again at steps 4, 5, 6… so the cost of a tool is
**output size × remaining turns**, not output size.

This audit measures two things per tool: the hard ceiling in its code, and what it
actually returns on a real repository (webpack, 54,022 files).

## The honest headline

Typical reads are **cheap**. The median source file in webpack is 114 bytes — 30
tokens. A 500-line read returns it whole for 30 tokens, and that is fine.

The problem is entirely in the tail and in repetition:

| | |
|---|---|
| Median source file | 114 B = **30 tok** |
| p95 source file | 12,862 B = **3,384 tok** |
| p95 all-text file | 43,620 B = **11,478 tok** |
| `read` worst case (500 × 2,000 chars) | 1,000,000 B = **263,157 tok** |
| Files over the 128 KB read ceiling | 61 of 54,022 (**0.1%**) |

So: **do not optimize the median, it is already fine.** Optimize the 5% that cost
thousands of tokens each, and eliminate the repeats. That is a different design
than "make reads smaller".

---

## Ranked by context damage

### 1. `read` — the only tool that can exceed the window in one call

**Worst case: 263,157 tokens = 132% of a 200k window.**

`read-ceilings.ts` has three good ceilings (2,000 lines, 128 KB total, 2,000 chars
per line). But they multiply: 500 lines × 2,000 chars = 1 MB of text in a single
result. A minified bundle or a generated file under 128 KB with long lines can
return six figures of tokens in one call, and the session is over.

That is a real bug and a small fix: the byte ceiling should be the binding
constraint, not the line ceiling, and it should be expressed in tokens.

### 2. `read` again — the compaction re-read loop

**Cost: unbounded, proportional to file size × number of compactions.**

`read-state.ts` documents this itself:

> once auto-compaction collapses an old read payload to a placeholder notice, its
> hash is no longer derivable — the model must re-read.

So compaction saves tokens and then forces a full re-send. p95 file = 3,384 tokens;
a 40-step session with two compactions of a large file pays 6,768 tokens for
content it already had. On `chat.ts` in this repo (271 KB ≈ 71,000 tokens) one
repeat exceeds a typical remaining budget.

This is the single largest avoidable cost, and it is architectural, not a constant.
See `READ-TOOL.md`.

### 3. `visualize` — inline HTML with no ceiling

**Worst case: ~15,789 tokens.**

A self-contained HTML document is returned inline. No truncation, no size limit.
It is legitimate output (a chart or diagram), but it lives in context forever, and
a model that visualizes three times has spent 47,000 tokens on markup it will never
reference again.

**Fix:** return a handle plus a short description. The UI renders from the handle;
the model sees "chart rendered, 42 bars, range 0–100" at ~60 tokens.

### 4. `skill` — full markdown, no ceiling

**Worst case: ~10,526 tokens.**

Loading a skill injects its entire SKILL.md. That is the point of a skill, but the
file is unbounded — a 40 KB skill is 10,526 tokens on every subsequent turn.

**Fix:** cap at ~2,000 tokens and require skills to be written to that budget, the
same way tool descriptions should be. A skill that needs more should reference
files it loads on demand.

### 5. `grep` — 5,263 tokens and no truncation flag

**Worst case: 100 matches × 200 chars = 5,263 tokens.**

Capped at 100 matches, which is good. But each match carries up to 200 chars of
context, and the result does not say when it hit the cap. A model that gets 100
matches does not know whether there were 100 or 10,000.

**Fix:** two lines. Report `totalMatches` vs `returned`, and default to
`filesOnly` when the count exceeds ~30 — a file list is 40 chars/row instead of
200.

### 6. `bash` — silent middle truncation

**Worst case: 3,157 tokens (12,000 chars).**

Head 3,000 + tail 8,000 chars, middle dropped. The size is reasonable. The problem
is that the drop is silent: a stack trace whose relevant frame is in the middle
looks complete.

**Fix:** insert `… 4,213 chars omitted …` at the cut. One line.

### 7. `dir_list` — the same data twice

**Worst case: 4,184 tokens for 100 entries.**

Returns an `entries` array *and* a `formatted` string. The model receives both and
pays for both — 159 chars/entry where 26 would do.

**Fix:** `entries` moves to `meta`. One-line change, 3x cheaper, every listing,
forever. This is the highest value-per-minute fix in the whole list.

### 8. `fetch` — 2,631 tokens, no pagination

**Worst case: 10,000 chars.**

Reasonably capped, but there is no way to ask for the next chunk. A model that
needs page 2 re-fetches and re-reads page 1.

**Fix:** an `offset` parameter, and a note when the body was cut.

### Under control

| Tool | Ceiling | Note |
|---|---|---|
| `glob` | 100 paths ≈ 1,052 tok | capped, unmarked |
| `web-search` | 8 × 500 chars ≈ 1,052 tok | well designed |
| `task` | ~789 tok | tail-bounded |
| `todo` | small | fine |

---

## The pattern behind the bad ones

Five of the eight have no ceiling at all, or a ceiling expressed in the wrong unit:

| Tool | Ceiling unit | Should be |
|---|---|---|
| `read` | lines × chars | **tokens** |
| `visualize` | none | tokens, or a handle |
| `skill` | none | tokens |
| `grep` | matches | tokens + a truncation flag |
| `fetch` | bytes | bytes + offset |

**The rule: every tool's output should be expressible in tokens, and every
truncation should be announced.** A ceiling in lines or matches is a proxy that
fails on the tail — which is exactly where the damage is.

---

## Priority order

| # | Fix | Effort | Saves |
|---|---|---|---|
| 1 | `dir_list`: entries → meta | 1 line | 3x on every listing |
| 2 | `read`: byte ceiling binds, in tokens | ~10 lines | prevents window-ending calls |
| 3 | `read`: evidence ledger | 1 file | eliminates compaction re-reads |
| 4 | `grep`: truncation flag + filesOnly default | 2 lines | ~5k tok on broad searches |
| 5 | `bash`: announce the omitted middle | 1 line | prevents silent confusion |
| 6 | `visualize` + `skill`: token caps or handles | ~20 lines | ~26k tok worst case |

Items 1, 4 and 5 are three lines of code between them and should ship today.

---

## How this was measured

- **Ceilings** read from the source constants (`MAX_COMMAND_OUTPUT_CHARS`,
  `READ_MAX_LINES`, `maxTotal`, `MAX_BODY_LENGTH`) in
  `packages/agent/tools/`.
- **Real file sizes** from webpack, 54,022 files, sampled by extension.
- **Token estimates** at 3.8 chars/token, the constant calibrated against this
  tool's own output elsewhere in `CORPUS-FINDINGS.md`.

Not measured: actual session traces. The multiplies above (`output × remaining
turns`) are the model; a trace would confirm the distribution. If you instrument
one thing, instrument **tokens returned per tool per session** — that single metric
would replace most of this document with fact.
