# A tool suite, not a tool

37 tools live in `packages/agent/tools/`. This is about making them a suite.

## The measured problem

I parsed every tool definition in the repo:

| | |
|---|---|
| Tool definitions found | 30 |
| Total payload | 10,807 chars (**~2,843 tokens**) |
| Description prose | 10,147 chars (94%) |
| Schema shape | 660 chars (6%) |
| Largest single tool | `sandbox.ts`, 1,521 chars = **400 tokens** |
| Smallest | `grep.ts`, 304 chars |

**2,843 tokens are spent on every request, before the agent does anything.**
That is 1.4% of a 200k context and 2.2% of 128k. It never goes away, it never
shrinks, and it grows with every tool you add.

Worse than the cost is the inconsistency:

- Descriptions range **304 to 1,892 chars** — 6x variance with no rule
- Only **3 of 37** files share any output helper
- The schema is 6% of the payload; the prose is 94%. **The prose is the budget.**

A suite where every tool was written by whoever needed it that day is not a
suite. It is 37 one-offs in a folder.

## What a suite needs

Four things shared, defined once, enforced by a check:

### 1. A description budget

`MAX_DESCRIPTION_CHARS = 400`, enforced at registration. `describe()` throws if
you exceed it.

400 chars fits: summary, 2–4 WHEN TO USE bullets, 1–3 WHEN NOT TO USE bullets,
one note. That is everything a model needs to choose a tool. Everything else —
parameters, examples, gotchas — belongs in the tool's docstring or a skill.

The variance today is not because `bash` needs 1,892 chars. It is because nobody
set a limit.

### 2. `WHEN NOT TO USE`, with the alternative named

This is the section that decides whether a suite works. An agent's failure mode is
not picking a bad tool — it is picking the *familiar* one. `grep` wins by default
because it has always been there.

So every tool states which tool to use instead:

```
WHEN NOT TO USE:
- searching for a string literal (use grep)
- you do not know the name yet (use map)
```

Naming the alternative is what makes a suite navigable. Without it, five tools that
overlap look like five tools that might work.

### 3. One result shape

```ts
interface ToolResult<M> {
  success: boolean;
  text: string;    // model-visible, the only thing sent
  meta?: M;        // NEVER sent to the model
  error?: string;
}
```

`meta` is the important field. It lets a tool return rich structure — file handles,
byte counts, timings, a cursor for the next call — without paying tokens for it.
Today `dir_list` returns an `entries` array *and* a `formatted` string, and the
model receives both: 159 chars/entry instead of 26.

Never throw. A thrown error becomes a transport error the model cannot read;
`{ success: false, error: "..." }` lets it adapt.

### 4. A budget that is honoured

```ts
clampToBudget(text, maxTokens, "narrow the path or raise the budget")
```

Every truncating tool appends the marker. Silent truncation is what made a
2,000-token map look like a complete inventory of a 53,328-file repo.

## The registry tells you the cost

```ts
const suite = new ToolSuite()
  .add(mapTool).add(listTool).add(fileTool);

suite.promptCost();
// { tokens: 512, byTool: [{ name: 'map', tokens: 180 }, ...] }
```

A suite should be able to state its own price. If you cannot say what your tools
cost per request, you cannot decide whether adding a 38th is worth it.

`overlaps()` finds tools whose `replaces` sets intersect — the merge candidates.

## Concrete plan for the 37

### Phase 1: measure, do not change (1 hour)

Add a test that prints the total payload. No behaviour change. You need the number
in CI so a new tool cannot quietly add 400 tokens.

### Phase 2: the 6 worst descriptions (half a day)

`sandbox` (400 tok), `task` (333), `dir-list` (220), `todo` (176), `git-status`
(153), `bash` (147). Rewrite to the 400-char format. Those six are **1,429 of
2,843 tokens** — half the payload. Cutting them to 400 chars saves roughly 700
tokens per request, forever.

### Phase 3: merge the overlaps (1–2 days)

Tools that answer the same question at different granularity are the ones to
collapse. `dir-list` and `glob` both answer "what files exist"; `grep` and
`sightline_find` both answer "where is this". A merged tool with a `mode` parameter
costs one definition instead of two, but is harder for a model to use correctly —
so only merge where the question is genuinely identical.

### Phase 4: fix `dir_list`'s double payload

One-line change with a measurable win: return `formatted` as `text`, move `entries`
into `meta`. Same information, 3x fewer tokens on every listing. This is the
pattern `ToolResult` exists to make obvious.

## Language: why the engine is Python and the tools are TypeScript

You asked why not Rust, or the same language as the rest of the codebase.

**The tools are TypeScript** — they have to be. A native tool is a function in the
agent's own tool array, sharing its `tool()` wrapper, zod schemas, sandbox handle,
and path security. That is TypeScript by definition. `entry-tools/codemap.ts` is
that, and it typechecks clean against `tsc --strict`.

**The engine is Python, and that is a real cost you should weigh:**

| Option | Cost | Benefit |
|---|---|---|
| Keep Python sidecar (current) | subprocess ~40ms; two runtimes in the image; can't share types | 2,600 lines already measured on 210 repos |
| Port to TypeScript | rewrite, reintroduce every bug the corpus found | one runtime, no IPC, shared types |
| Rust | rewrite plus FFI or a binary per platform | speed, but speed was never the bottleneck |

The measured performance settles the Rust question: parsing 53,328 files takes
6.4s cold, and **rendering the answer takes 281ms**. The bottleneck is regex over
source text, not compute. Rust would maybe halve the 6.4s; it would not touch the
281ms that every call pays, and it would not touch the token cost, which is the
only number that matters.

The honest argument for TypeScript over Python is not speed. It is **distribution**:
a sandbox image that already has Node does not need Python, a venv, or a
`PYTHONPATH`. That is a real simplification and probably worth doing eventually —
but as a port of a working, measured engine, not instead of one.

**My recommendation:** ship the sidecar, keep the CLI contract
(`map|list|file|find|callers` on stdout), and port to TypeScript only if the
sidecar becomes an operational problem. Porting first means rewriting 2,600
measured lines into 2,600 unmeasured ones.

## Where codemap fits

It is not a 38th tool competing for budget. It is a **replacement** for three
existing ones, and its `replaces` field says so:

```ts
{ name: 'map',   replaces: ['dir_list', 'read'] }
{ name: 'locate', replaces: ['grep'] }
{ name: 'importers', replaces: ['grep'] }
```

If it earns its place, the suite shrinks: `dir_list` stops needing a 839-char
description because `map` and `list` cover it. The 5 codemap tools cost ~852 tokens
as written; at the 400-char limit they cost ~500, and they displace ~700 tokens of
`dir_list` + `grep` prose.

That is the only honest justification for adding tools to a suite: not "it is
useful", but "it is more useful per token than what it removes".

## Should you improve `dir_list` instead of building this?

Both, and the split is not arbitrary.

**`dir_list` has a fixed defect that should be fixed regardless:** it returns
`entries` *and* `formatted`, and the model pays for both. That is 159 chars/entry
where 26 would do. Move `entries` into `meta` and it becomes 3x cheaper with no
new tool, no new index, no new runtime. Do that first — it is a one-line change
and it makes every existing session cheaper today.

**What `dir_list` cannot become, no matter how it is improved:**

- It cannot rank. It has no import graph, so it cannot say which file is central.
- It cannot answer "what is *in* this file". That needs parsing.
- It cannot answer "who imports this". That needs edges.
- It cannot serve a 53k-file repo in 25k tokens. It has no notion of a budget.

Fixing `dir_list` makes a good listing. codemap is not a listing; it is the
ranking and parsing layer that makes "which file should I read" answerable. The
two are complements: `sightline_list` and a fixed `dir_list` are nearly the same
tool, and codemap would not bother competing there.

So the sequence is: **fix `dir_list`'s double payload this week** (cheap, immediate),
and build the ranking layer alongside it. If the ranking layer never earns its
place, you still have a better `dir_list` and you lost a week.

## The rule to write down

> A tool is not done when it works. It is done when its description fits the
> budget, its result has one text field, its failures name an alternative, and
> the suite's total cost is measured.

Everything else is a one-off in a folder.
