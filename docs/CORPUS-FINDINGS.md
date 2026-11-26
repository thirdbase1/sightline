# Corpus study: 118 repositories, 30.3M LOC

A single repo tells you a tool works. A hundred tells you *where it breaks*.

Method: `bench/corpus.py` clones each repo shallow and blobless, indexes it,
measures, writes one JSONL record, and deletes the clone. Disk was the binding
constraint (2.9 GB free), so the corpus never holds more than one checkout.
118 of 126 attempted repos produced usable records; none failed to index.

## The headline

| | |
|---|---|
| Repositories | **118** |
| Source LOC indexed | **30,259,215** |
| Total index time | 436 s (**69 kLOC/s**) |
| Median files per repo | 485 |
| Median parsed rate | **83.7%** |
| Mean parsed rate | 79.7% |
| Minimum parsed rate | **36.7%** |
| Budget violations | **0 / 118** |

## Bug 1: every `.tsx` file had zero imports

`IMPORT_PATTERNS` was keyed by exact language name. `.tsx`, `.jsx`, `.vue` and
`.svelte` are separate language names that share TypeScript import grammar, but
only `typescript` had patterns. So **every `.tsx` file extracted `imports: []`**.

Found by checking `callers` against grep on a real repo: codemap reported 59
importers of `schema.ts` where the true count was 73. The missing 14 all imported
through `@/lib/db/schema` from `.tsx` files.

| | before | after |
|---|---|---|
| Import edges (entry-agents) | 1,585 | **2,965** (+87%) |
| Import edges (t3code) | 20,963 | **33,933** (+62%) |
| Caller recall vs grep truth | 81% | **100%** |

This is the largest correctness bug found, and it was invisible on the single
TypeScript monorepo I had been testing because that repo's `.ts` files carried
enough edges to look plausible.

**Fixed:** an explicit alias table, flattened. A first attempt chained
`jsx → javascript`, which had no patterns of its own, so JSX still returned
nothing. A regression test now asserts every TS-like language resolves to a real
pattern set.

## Bug 2: six repos were completely blind

PHP and Ruby had no extractor at all. Six of 118 repos parsed **under 10%**:

| Repo | before | after |
|---|---|---|
| composer/composer | **0.0%** | 91.4% |
| sinatra/sinatra | **0.0%** | 76.4% |
| Homebrew/brew | **0.0%** | 59.7% |
| fastlane/fastlane | 4.8% | 68.3% |
| jekyll/jekyll | 0.4% | 58.4% |
| laravel/laravel | **0.0%** | 36.7% |

After: **0 blind repos**, minimum 36.7%, median 83.7%.

The failure was silent. The map rendered these repos as a list of filenames with
`[N LOC, unparsed: no extractor for php]` on every line — technically honest, and
useless. `laravel/laravel` at 0% produced a 576-token "map" of a repo with 30
files.

## Bug 3: coverage collapses on large repos, invisibly

A fixed 2,000-token budget produces a **constant ~20 files**, so coverage falls
as repos grow:

| Files | Repos | Median coverage | Median files shown |
|---|---|---|---|
| <100 | 19 | **55.4%** | 25 |
| 100–300 | 27 | 14.2% | 24 |
| 300–700 | 24 | 4.4% | 20 |
| 700–1,500 | 20 | 1.6% | 17 |
| 1,500+ | 28 | **0.6%** | 19 |

Showing 20 arbitrary files out of 7,922 is worse than useless: it implies the
other 7,902 do not exist. Fixed with **directory rollup** — above 400 files and
below 3% coverage, the map shows directory aggregates instead.

```
# 7922 files, 2,344,086 LOC - too large for per-file detail at 2000 tokens
apps/server/  [1639 files, 795,298 LOC, 62,936 symbols]  top: shared.ts, OrchestratorScenario.ts
apps/web/     [2169 files, 509,934 LOC, 46,377 symbols]  top: messages.ts, locales.ts
```

**The first attempt at this broke the benchmark.** The threshold fired at 120
files, so a 1,213-file repo rolled up too, and the 29-question benchmark fell
from **100% to 72% answered** while reporting a *better* 92x ratio. That is the
exact failure the tool exists to avoid: a token win with a missing answer. The
threshold is now on coverage ratio, conservative, with a test that asserts a
1,213-file repo does **not** roll up.

## Bug 4: `callers` used string containment

The original matched `Path(path).stem in spec`, so `schema.ts` matched anything
importing *any* module called `schema`. Reported 67 importers where the truth was
73 with 8 false positives — and false positives are worse than misses, because the
agent then reads files that cannot contain the answer. Now resolves through the
real graph.

## Bug 5: corpus harness could wedge indefinitely

One blobless clone stalled at 708 KB for minutes and blocked the entire 121-repo
run at position 74. `subprocess` timeout alone did not help because the clone had
already succeeded and the *index* was the slow part.

**Fixed:** 90 s hard clone cap, no LFS smudge, partial clones deleted on failure,
a `SIGALRM` watchdog at 120 s per index, and `EXTRACTOR_VERSION` in the cache key
so parser improvements actually invalidate instead of silently reusing stale
symbols. That last one is why adding PHP/Ruby appeared to do nothing at first.

## Language coverage after the fixes

| Language | Repos | Median parsed | Min |
|---|---|---|---|
| TypeScript/JS | 13 | 89.0% | 51.2% |
| Python | 12 | 76.0% | 56.4% |
| Go | 9 | **97.0%** | 69.1% |
| Rust | 10 | 85.7% | 58.3% |
| C/C++ | 6 | 73.0% | 41.5% |
| PHP/Ruby | 6 | 64.0% | 36.7% |

Go is highest because its grammar is the most regular: `func` and `type` at
column zero, one declaration per line. C/C++ is lowest among the mature extractors
because multi-line function signatures and macro-heavy headers defeat line-based
matching.

## Symbol density

| Language | Symbols per 1k LOC |
|---|---|
| TypeScript/JS | 63.1 |
| Python | 60.5 |
| Rust | 55.7 |
| Go | 41.3 |
| C/C++ | 39.1 |

The spread is 1.6x, which matters for budgeting: a 2,000-token map shows fewer Go
symbols than TypeScript symbols for the same LOC.

## What this study did not measure

- **Answer quality across the corpus.** The 29-question benchmark runs on one
  repo. Building ground-truth questions for 118 repos is the obvious next step
  and is a much larger project than this.
- **Clone failure rate.** 8 of 126 repos were dropped for clone timeouts or size,
  which biases the corpus toward repos that clone quickly.
- **Co-change.** Disabled for the corpus run (`with_cochange=False`) because
  shallow clones have one commit and would report zero pairs. Measured
  separately on a full clone: 399 pairs from 1,080 commits.
- **Rendering cost.** `render_ms` reads 0 in the records because a sub-millisecond
  render rounds down. It is fast; the number is just too small to show.

## Reproducing

```bash
python3 bench/corpus.py --list bench/repos100.txt --out /tmp/corpus.jsonl
```

Resumable: re-running skips repos already in the output file. Records are
appended one at a time, so an interrupted run keeps everything it measured.

---

# Part 2: 92 large repositories

The first corpus was mostly under 1,500 files. This one targets 2,000–29,000
source files, where a fixed token budget and a monolithic JSON index start to
strain. 92 of 97 attempted repos produced records (5 exceed the clone budget on
this machine).

| | |
|---|---|
| Repositories | **92** |
| Files indexed | **334,724** |
| LOC indexed | **50,919,458** |
| Median files | 2,151 |
| Max files | **29,188** (webpack) |
| Median index time | 1.1 s |
| Max index time | 31.6 s (n8n, 28k files / 5.7M LOC) |
| Median peak RSS | 109 MB |
| Max peak RSS | **394 MB** |
| Median index size | 2.7 MB |
| Max index size | **50.3 MB** |
| Median parsed | 83.6% |
| Min parsed | 28.2% (webpack) |

Budget violations: **0 of 276 renders** (92 repos × 3 budgets).

## Bug 6: `lib/` skipping was a false positive

Skipping `lib/` unconditionally fixed mui (28,582 → 7,078 files by dropping
10,751 transpiled icon files) and simultaneously broke **webpack**, whose `lib/`
*is* the source — it has no `src/` directory. webpack fell to 28% parsed.

The discriminator that works for both: **`lib/` is output only if a sibling
`src/` exists.**

| Project | `lib/` | sibling `src/`? | verdict |
|---|---|---|---|
| mui-icons-material | 10,751 `.js`/`.mjs` | yes | output, skip |
| webpack | 30 modules | no | source, keep |
| python `lib/` | real modules | either | source, keep |

Skipping it unconditionally is worse than indexing some artifacts, because a
miss is invisible while an artifact is merely noise.

## Bug 7: the size heuristic dropped real files

An earlier version treated files under 60 bytes as generated. A 36-byte Python
class is legitimate source. Removing the rule restored `lib/core.py` and every
small module. Size is not a signal; content markers are.

## Bug 8: the harness stalled indefinitely, twice

Two separate stalls, both my bugs, not the repos':

1. **Clone timeout left partial state.** `subprocess` timeout fired, but deleting
   a 500 MB tree took longer than the parent's patience, leaving it for the next
   run to trip over. Fixed: sweep stale clones at startup, `rm -rf` before
   `rmtree`, record an `attempted` row *before* each clone so restarts skip a
   repo that wedges rather than retrying the same hang forever.
2. **Size check ran after the walk.** `dir_size_mb` walked the entire 897 MB
   checkout before the size guard could skip it, which took minutes and looked
   like a hang. Fixed: `du -sm` (C speed) first, walk only for repos that pass.

Both would have been invisible if I had only run small repos.

## Bug 9: CSS and HTML had no extractor

6,585 CSS and 3,751 HTML files in webpack alone scored `none`, which reads as an
extractor failure rather than "a stylesheet has no functions". Added
selector/custom-property and id/custom-element extractors: webpack 28.2% → 31.2%.

## Finding: the budget that matters depends on repo size

Rollup trigger rate at 2,000 tokens, by size:

| Files | Repos | Rolled up |
|---|---|---|
| <500 | 16 | 0% |
| 500–2,000 | 27 | 7% |
| 2,000–5,000 | 32 | **100%** |
| 5,000+ | 17 | **100%** |

At 25,000 tokens the 2–8k band flips to per-file detail. Above 8k files, no
practical budget shows filenames, so `--focus` is mandatory.

**This contradicts the advice from Part 1.** "Start at 2,000" is correct under
2,000 files and actively unhelpful above it. The map now prints the budget it
would need: `# need --budget 250k for per-file detail`.

## Where it still fails

- **webpack 28.2%** is mostly correct: `examples/*.js` and test fixtures genuinely
  have no declarations. The number is honest, not broken.
- **5 repos are unclonable here** in 150 s. That is a measurement limit, not a
  tool limit, but it biases the corpus toward repos with lean history.
- **Peak RSS scales with the largest single file**, not repo size: 394 MB max came
  from repos with big generated bundles, not the 29k-file one.

## Bug 10: the rollup could not spend its budget

Three failure modes in a row, all in the directory-rollup path, all found by
checking output size against the requested budget rather than trusting it:

1. **Grouped by full parent path** → webpack produced 20,079 "directories"
   instead of ~20, so a 25k-token request returned 7k.
2. **Grouped by top level only** → webpack collapsed to one line,
   `test/: 28,743 files`, because 98% of its files live under `test/`.
3. **`_expand_dir` matched the exact parent** → a depth-2 key like
   `test/fixtures` matched zero files, so expansion silently did nothing.

Fixed with balance-based depth selection (reward group count, punish one dominant
bucket), prefix matching in `_expand_dir`, and budget-scaled per-group allocation.

**Final behaviour on webpack:** 1,946 / 2,877 / 9,142 tokens for 2k / 8k / 25k.
It does not reach 25k, and that is correct: webpack has **0.80 symbols per file**
across 29,238 files, because it is overwhelmingly tiny test fixtures. 9k tokens
covers its meaningful structure. A map that padded to fill the budget would be
lying.

Other repos with real code density do fill their budget: t3code and mui both hit
24,942 on request.

---

# Part 3: Q&A accuracy on large repos

Parts 1 and 2 measured whether the tool *runs*. This measures whether it *helps*.

The hard part is oracles. Question sets generated *by* the index would validate
the index against itself, so every question is checked against an independent
source that never reads codemap's data:

| Question type | Oracle |
|---|---|
| orient: "what are the main directories?" | `os.scandir` on the real tree |
| drill: "where is `<symbol>` defined?" | `git grep` for the definition |
| outline: "what does `<file>` declare?" | regex over the file itself |

Drill questions are filtered to symbols with **exactly one** definition. A name
defined in three files has no single right answer, and scoring either arm against
one of them would be arbitrary.

## Result: 183 questions, 21 repos (corrected)

| Arm | Hits | Accuracy | Tokens | Calls |
|---|---|---|---|---|
| map, 1 call | 167 | 91% | **303,914** | **21** |
| **map, 2 calls (focus)** | **169** | **92%** | 303,914 | 42 |
| grep + read | 125 | 68% | 712,032 | 269 |

**92% at 2.34x lower cost, in 6% of the calls.** By repo size:

| Files | Repos | Questions | map | grep | Token ratio |
|---|---|---|---|---|---|
| under 150 | 10 | 100 | **99%** | 78% | 1.56x |
| 150–600 | 5 | 41 | **93%** | 63% | 0.98x |
| 600–2,000 | 4 | 30 | **77%** | 50% | 3.75x |
| 2,000+ | 2 | 12 | **75%** | 50% | 5.24x |

No repo in the set now favours grep.

## Correction: two bugs made the map look worse than it is

An earlier run reported 79% vs 70% and claimed *grep wins on repos under 100
files*. Both were artifacts of my own code, and both are worth recording because
they are the kind that produce confident, plausible, wrong numbers.

### Bug A: root-level files scored as directories

`map_names()` treated any slash-free path as a directory, so for `cobra.go` it
searched for `cobra.go/`. On a 36-file repo most files sit at the root, so the
bug hit small repos hardest and manufactured the false "grep wins under 100
files" conclusion.

| | before | after fix |
|---|---|---|
| cobra | 4/10 | **10/10** |
| viper | 5/10 | **11/11** |
| 10 small repos | 75% | **99%** |

### Bug B: `"use strict"` treated as build output

`ARTIFACT_MARKERS` contained `"use strict";` as a CommonJS tell. eslint's entire
hand-written `lib/` opens with exactly that line, so the walker deleted
two-thirds of the repository:

| | before | after |
|---|---|---|
| eslint files indexed | 600 | **1,521** |
| eslint map accuracy | 5/9 | **9/9** |
| eslint outline symbol recall | 0% | 100% |

A generated-file marker must be something *only a transpiler* emits:
`Object.defineProperty(exports,`, `__webpack_require__(`, `//# sourceMappingURL=`.
Language boilerplate is not a marker.

### Bug C: fixture trees outranked real source

PageRank over eslint's import graph put 63 `tests/fixtures/*` files at the top of
the map and pushed `lib/linter/vfile.js` off it entirely — fixture trees are
numerous and densely interlinked. Fixed by demoting (not excluding) fixture,
docs, example and benchmark paths, and by giving files absent from the graph a
median rank instead of a floor.

## Outline recall: where the map is strongest

Symbol recall for outline questions — does the map list the file's declarations:

- **100% on 13 of 20 repos**
- 0% on 2 (eslint, fastapi), where the file never reaches the ranked cut

When a file is in the map, its symbols are right. The failure is coverage, not
extraction.

## Bugs found by building this benchmark

1. **`git grep -l` prints bare paths, not `path:line:match`.** A `":" in line`
   filter discarded every result, so drill and outline questions were never
   generated and the harness silently ran with orient-only questions. It still
   produced plausible-looking numbers.
2. **`git grep` uses POSIX ERE.** `\s` and `\b` are not portable, and
   `struct`/`enum`/`trait` were missing from the keyword list, so Rust and Go
   symbols never disambiguated — all drills vanished.
3. **Arm B focused on the top-level directory.** In a monorepo `packages/` covers
   everything, so focus did nothing. A real agent narrows to the deepest
   directory the map named, which is what the fixed version models.
4. **Token accounting charged a full map render per question.** Eight questions
   at a 25k budget reported 121k tokens. The orientation map is fetched once and
   reused; only focused slices repeat.

## What this does not show

- Questions are generated from definition statements, so they favour
  symbol-shaped code. A question like "why does this retry twice" has no oracle
  here and is not measured.
- The grep arm is a *model* of a competent agent (grep, then read the top hits),
  not a live agent trace.
- 20 repos, 182 questions. Enough to see the size crossover; not enough for a
  tight confidence interval on the 9-point gap.

## A trap this benchmark caught in itself

Running the 29-question benchmark against the *wrong* repo produces a spectacular
number:

```
t3code: answer in map 14/29 (48%)   ratio 58.39x
```

58x looks like the best result in this whole document. It is meaningless: those
29 questions name files in `entryagents/entry-agents` (`apps/web/lib/db/schema.ts`,
`packages/agent/tools/glob.ts`). t3code has no such files, so the "answers" are
misses and the map is small because it never found anything.

The harness prints the warning above the ratio for exactly this reason. **Any
token-ratio claim is only valid alongside its accuracy number.** A 58x ratio with
48% accuracy is worse than a 1x ratio with 100%.

Corollary: `bench30.py` is repo-specific by construction. Do not run it on a repo
other than the one its questions describe. Use `qa.py` for anything else, because
its oracles are generated per repo.

---

# Part 4: repeated runs on a 356 MB repo

One run tells you it works. Ten tells you whether it is *stable*.

Target: `webpack/webpack`, **356 MB on disk, 55,154 files, 53,328 indexed**.
Harness: `bench/stress.py --runs 10`.

## Final numbers

| Phase | min | median | p95 | max |
|---|---|---|---|---|
| cold index | 6,389 | 6,389 | 6,389 | 6,389 |
| warm index | 2,930 | **3,220** | 3,435 | 3,435 |
| map (25k) | 235 | **281** | 369 | 369 |
| list (`--direct`) | 738 | **837** | 936 | 936 |
| find | 20 | **28** | 37 | 37 |

- Files indexed: **stable at 53,328** every run
- Warm cache: **53,328/53,328 reused**
- Output: **byte-identical across all 10 runs** (map, list, find)
- RSS: 127 MB → 173 MB, oscillating, not climbing

## What the repeat runs found

### Bug 1: 18.0 s cold index, three separate causes

| Fix | cold | warm |
|---|---|---|
| baseline | 18.0 s | 8.0 s |
| `pathlib` in the import resolver (684k Path objects) | — | 7.2 s |
| Pagerank over lists instead of dicts | — | 6.4 s |
| `walk()` returns strings, not Path objects (611k allocations) | **6.0 s** | **3.0 s** |

The walk was the big one: it built a `Path` per directory *and* per file, then
called `.suffix` and `.stat()` on each. `os.scandir` already carries the name and
caches the stat, so strings do the same work with none of the allocation.

`stat`-based cache signatures instead of SHA-256 were also tried; the gain was
zero because `open()` was not actually the bottleneck. Left in place anyway,
since it removes 106k reads on a repo this size.

### Bug 2: 3.5 s to render a 25k-token map

`_expand_dir` scanned all 53,328 files *for every directory it expanded*, and the
rollup calls it twice per group. O(groups × files).

Fixed with a directory→files prefix index built once:
**3,504 ms → 315 ms** for the 25k map, **206 ms** for the 2k one.

### Bug 3: a memory leak that took three attempts to fix

Object count grew **+549,924 over 3 runs**. Three wrong fixes, each of which
looked right:

1. **`dict[id(idx)]`, no eviction.** 23k entries leaked per run. Worse: `id()` is
   a memory address, so a freed Index's id gets reused and the cache serves one
   repo's directory index to another.
2. **`WeakKeyDictionary`.** A weak key is not enough when the *value* references
   the key's own data — the entry keeps itself alive. 390k lists accumulated.
   Also required making `Index` hashable, which changed a dataclass's semantics
   to fix a caching detail.
3. **A bounded global holding the last 4 indexes.** Correct output, but it
   deliberately retains four full indexes, which reads as a leak in any
   long-lived process.

The right answer was the simplest: store it as an attribute on the Index. Lifetime
follows the object, no global state, no retention. **+549,924 → +30 objects.**

## Two harness bugs that made the tool look worse

Both were in `stress.py`, not the tool, and both produced plausible bad numbers:

- **`cache=` was never passed**, so every "warm" run was actually cold. This is
  how an early run reported warm times *increasing* (6.4 s → 7.1 s) — it was just
  repeated cold builds.
- **`ru_maxrss` is monotonic.** It reports peak, so it can never go down and makes
  a stable process look like it is leaking. Switched to `/proc/self/statm`.
  Separately, the harness held each run's index alive in a variable, which
  inflated RSS by ~50 MB all by itself.

A clean load → build → render → delete loop measures **flat at 75–81 MB across
seven runs**. The tool does not leak.
