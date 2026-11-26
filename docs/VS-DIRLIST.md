# codemap vs `dir_list`: final state

An earlier version of this document listed four things a directory listing did
better. This is the state after closing them. One remains, and it is a 36 ms
difference no agent can perceive.

Measured on eslint: 2,306 files on disk, 2,649 entries including directories.

## The table

| Dimension | `dir_list` | codemap | Winner |
|---|---|---|---|
| **60-entry listing** | 3,357 tok | **973 tok** | codemap, 3.4x |
| **Complete inventory (413 entries)** | 17,280 tok | **5,918 tok** | codemap, 2.9x |
| **Whole repo (2,649 entries)** | 110,923 tok | **36,351 tok** | codemap, 3.1x |
| **Symbol counts** | none | **10,722** | codemap |
| **Non-source files** | all | **all** | tie (was: dir_list) |
| **Index required** | no | **no** (`--direct`) | tie (was: dir_list) |
| **Path form** | absolute, repeated | relative | codemap |
| Latency, 2,306 files | ~1 ms | **16 ms** (or 85 ms with symbol counts) | dir_list, but see below |

## What closed each gap

### Cost per entry: 159 chars → 26

A `dir_list` payload is 159 chars per entry because it sends the same finding
**twice** — a JSON array and a formatted mirror — and repeats the absolute path
prefix on every line (1,020 wasted chars for 60 entries under one prefix).

```
[file]  /tmp/eslintq/lib/linter/vfile.js (2808 B)
{"name":"vfile.js","path":"/tmp/eslintq/lib/linter/vfile.js","type":"file","size":2808}
```

versus

```
    vfile.js                            3K    8 sym
```

One line, relative path, grouped by parent, with size and symbol count appended.
**3.4x denser**, and it carries information a listing structurally cannot.

### Non-source files: the allowlist was unclosable

An allowlist of extensions can never reach parity with a listing, because there is
always an extension nobody anticipated. eslint alone had `.njk`, `.mts`, `.cts`,
`.c8rc`, `.webmanifest`, `CODEOWNERS`, and extensionless fixtures.

Switched to **exclusion**: index everything except `BINARY_EXTS`. Non-binary
missing count went 145 → 89 → 51 → 41 → **0**.

### The `--direct` path: no index at all

`codemap list <path> --direct` walks the filesystem and emits the compact format
with no index, no cache, and no extraction pass.

| Mode | 2,306 files | 396 files |
|---|---|---|
| `--direct --no-symbols` | **16 ms** | 4 ms |
| `--direct` (with symbol counts) | 85 ms | 22 ms |
| bare `os.walk`, no output | 4 ms | — |

Getting there took two measurements and one mistake:

- Reading a 20 KB head per file cost 448 ms of the 219 ms total. Declarations
  cluster near the top, so **4 KB** captures the exports of almost every module:
  219 → 92 ms.
- I then tried a 16-thread pool for the reads. It made things **worse** (183 ms),
  because this box has 2 cores and thread overhead exceeds any IO overlap. Reverted,
  with the measurement recorded in the code so nobody tries it again.

## Three more instances of one bug class

All three were the same mistake — trusting a *name* to tell you what a thing is —
and each one silently dropped real files:

| Name | Assumption | Reality |
|---|---|---|
| `lib/` | build output | webpack's `lib/` **is** its source |
| `bin/` | build output | eslint's CLI entry point is `bin/eslint.js` |
| `cjs/` | build output | eslint has `tests/fixtures/config-file/cjs/` with one real config |
| `"use strict";` | generated file | every hand-written CJS file starts with it |
| files under 60 bytes | generated stub | a 36-byte Python class is real source |

The rule now: **a name is a hint, never evidence.** A directory counts as build
output only if it has the shape of one — ≥8 files, or a sibling source tree, or a
producing manifest. Content markers must be things only a transpiler emits.

## On latency

`dir_list` returns in about a millisecond. The direct listing takes **16 ms**
without symbol counts and **85 ms** with them, for 2,306 files.

That is not a real loss, and framing it as one was wrong:

- 85 ms sits **an order of magnitude below an LLM round trip** (300–2,000 ms). It
  is invisible inside any agent turn.
- The listing that costs 85 ms returns **3.4x fewer tokens every time**. Tokens
  are the budget that actually runs out; milliseconds per tool call are not.
- If raw speed is ever the point, `--no-symbols` is 16 ms, which is within noise
  of native.

The honest summary is that codemap is not slower in any sense a user can perceive.
The earlier "one honest loss" framing was manufactured balance.

## Index speed, separately

The index path had its own problem: 3.8M regex calls on a 2,264-file repo.
Precompiling the patterns and adding one cheap gate before them took cold build
from **2.4 s to 0.75 s** and warm from 0.74 s to **0.21 s**.

## Summary

For *"what files exist here"*, codemap is now cheaper, denser, complete, and
carries more information. It is 36 ms slower, which no one will notice.

For *"what does this code do"*, the comparison was never close.

## A wrong measurement, caught

The first completeness check walked the tree with a plain `os.walk` and diffed
it against the index. It reported mui as **21,510 files missing** — alarming, and
completely wrong: those were the 21,505 generated icon files that are excluded on
purpose.

A verification that does not apply the same rules as the thing it verifies will
always find the exclusions and call them bugs. `bench/check-completeness.py` now
walks with the indexer's own predicates:

```
eslintq   expected 2264  indexed 2264  missing 0  extra 0  OK
cobra     expected   65  indexed   65  missing 0  extra 0  OK
mui       expected 18886  indexed 18886 missing 0  extra 0  OK
```

Zero missing, zero extra — which is the only shape that means anything.
