# sightline

**Give a coding agent a complete picture of a repository in one call, so it reads
the right file instead of five plausible ones.**

Every token a tool returns is spent on *every subsequent request* for the rest of
the session. Tool output is not a one-time cost — it is a permanent tax. sightline
exists because the standard toolset pays that tax badly: `dir_list` sends every
finding twice, `grep` burns context on call sites when you wanted a definition,
and `read` re-sends whole files the model has already seen.

Nothing here is a wrapper. Each tool answers a question the built-in equivalent
answers expensively, and every claim below is measured rather than estimated.

---

## What it does

```
sightline index                    # once: 6.4s for 53k files
sightline map --budget 2000        # the repo, compressed
sightline map --focus src/auth     # re-ranked toward one area
sightline list lib                 # complete inventory, 3x denser than a listing
sightline file src/linter.ts       # one file's API in ~40 tokens
sightline find compactMessages     # declarations, not call sites
sightline callers compactMessages  # blast radius in one call
```

Five tools, each replacing at least one built-in:

| Tool | Replaces | Why |
|---|---|---|
| `map` | `dir_list` + 2–4 reads | ranks by import-graph importance |
| `list` | `dir_list` | 26 chars/entry instead of 159 |
| `file` | `read` (for API only) | ~40 tokens vs ~13,000 |
| `find` | `grep` (for definitions) | one result per definition |
| `callers` | `grep` + a read per hit | resolves the real import graph |

## The numbers

**Accuracy** — 183 questions across 21 repos, each checked against an oracle that
never reads the index (directory listing, `git grep`, the file itself):

| Arm | Accuracy | Tokens |
|---|---|---|
| map, 1 call | 91% | 304k |
| **map, 2 calls** | **92%** | **304k** |
| grep + read | 68% | 712k |

92% at **2.34x lower cost**, and it wins at every repo size:

| Files | map | grep |
|---|---|---|
| under 150 | **99%** | 78% |
| 150–600 | **93%** | 63% |
| 600–2,000 | **77%** | 50% |
| 2,000+ | **75%** | 50% |

**Scale** — 210 public repositories, 81M LOC indexed, zero budget violations in
276 renders. Largest single repo: 53,328 files / 5.7M LOC, cold index 6.4s, map
render 281ms, warm cache 3.2s.

**Against a directory listing**, measured on eslint (2,306 files):

| | `dir_list` | sightline |
|---|---|---|
| 60-entry listing | 3,375 tok | **973 tok** |
| Complete inventory (413) | 17,280 tok | **5,918 tok** |
| Whole repo (2,649) | 110,923 tok | **36,351 tok** |
| Symbol counts | none | **10,722** |
| Non-binary files | all | **all** |
| Latency | ~1 ms | 16 ms (85 with symbols) |

## Honesty as a feature

- **`none` confidence** means the file could not be parsed. It says so instead of
  returning an empty symbol list. 13% of a real repo lands here.
- **Truncation is always marked.** A 2,000-token map of a 53k-file repo says
  "showing directories", so it cannot be mistaken for a full inventory.
- **Every budget is exact.** 276 renders across 92 repos, zero overshoots.

Three classes of bug found by the corpus and fixed here, because they are the ones
that silently lie:

| Assumption | Why it was wrong |
|---|---|
| `lib/` is build output | webpack's `lib/` **is** its source |
| `"use strict"` means generated | eslint's entire hand-written `lib/` starts with it |
| files under 60 bytes are stubs | a 36-byte Python class is real source |

## Install

```bash
pip install -e .
sightline index
```

No dependencies. Python 3.10+.

As an MCP server:

```json
{ "mcpServers": { "sightline": {
  "command": "python3", "args": ["-m", "sightline.mcp_server"] } } }
```

As native Entry tools: `tools/sightline.ts` typechecks against `tsc --strict` and
uses Entry's exact `tool()` + zod + sandbox contract. See `docs/ARCHITECTURE.md`.

## Layout

```
src/sightline/        engine: extract, graph, index, render, cli, mcp_server
tools/                native tool layer (TypeScript, Entry-compatible)
  core.ts             shared contract: budgets, results, registry
  sightline.ts        the five tools
docs/
  ARCHITECTURE.md     why a suite needs a core, and the measured tool-budget problem
  READ-TOOL.md        the read-tool redesign: evidence ledgers, range-aware gates
  VS-DIRLIST.md       head-to-head against a directory listing
  CORPUS-FINDINGS.md  210 repos, 17 bugs, and the ones I got wrong
bench/                reproducible harnesses: corpus, qa, stress, mcp probe
tests/                59 tests, no network or GPU needed
```

## Prior art

The repo-map technique — extract declarations with tree-sitter, build a file
dependency graph, rank it with PageRank, truncate to a token budget — comes from
[aider](https://aider.chat/2023/10/22/repomap). This project follows the same
four steps and adds what that approach lacks: per-file parse confidence, budget
enforcement that reports its own truncation, and an accuracy benchmark with
independent oracles.

Nothing else was used. The engine, the benchmarks, and the tool layer are
original, and every number in this README was measured by the harnesses in
`bench/` rather than estimated.

MIT.
