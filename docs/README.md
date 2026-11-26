# Native tools, and what to call the thing

Two questions: how to make this a native tool rather than an MCP server, and what
to name the repo. The second one is a real blocker and worth 10 minutes.

---

## 1. Native tools

You are right that MCP is the wrong layer. MCP adds a process, a protocol
handshake, and ~850 tokens of tool definitions whether or not the model uses them.
A native tool is a function in the agent's own tool array, registered next to
`read` and `dir_list`, sharing their auth, path security, and result shape.

`entry-tools/codemap.ts` is that. It uses the exact contract Entry's tools use:

```ts
import { tool } from "ai";
import { z } from "zod";
import { getSandbox, toDisplayPath } from "./utils";
import { resolveWorkspacePath } from "./path-security";

export const codemapMapTool = () =>
  tool({
    description: `...WHEN TO USE / WHEN NOT TO USE / USAGE...`,
    inputSchema: z.object({ budget: z.number().int().optional() }),
    execute: async (input, { experimental_context }) => { /* ... */ },
  });
```

### Drop-in steps

1. Copy `entry-tools/codemap.ts` to `packages/agent/tools/codemap.ts`.
2. Add to `packages/agent/tools/index.ts`:

```ts
export {
  codemapMapTool, codemapListTool, codemapFileTool,
  codemapFindTool, codemapCallersTool,
} from "./codemap";
```

3. Register in the agent's tool map, beside `dir_list`:

```ts
sightline_map: codemapMapTool(),
sightline_list: codemapListTool(),
sightline_file: codemapFileTool(),
sightline_find: codemapFindTool(),
sightline_callers: codemapCallersTool(),
```

4. Make `codemap` available in the sandbox image: `pip install codemap`, or set
   `CODEMAP_PYTHONPATH` to a checkout. The tools degrade explicitly if it is
   missing — they return "fall back to dir_list and grep", not a stack trace.

### Why it shells out to Python

The engine is 2,600 lines that have been measured against 210 repositories,
53,328-file repos, and 183 oracle-checked questions. Porting it to TypeScript
would discard all of that and reintroduce every bug the corpus found. A subprocess
call is ~40ms and invisible.

If you do port it later, the contract is one process with five subcommands
(`map`, `list`, `file`, `find`, `callers`) writing formatted text to stdout. The
TS layer above does not need to change.

### The rules these tools follow, and why

Every one comes from a bug found during measurement:

**Descriptions open with WHEN TO USE and WHEN NOT TO USE.** The failure mode is not
a bad tool, it is an agent reaching for `grep` because it is familiar. The negative
cases do the work: "searching for a string literal (use grep)" is what stops a
model from using `sightline_find` for everything.

**Text out, not JSON.** `dir_list` returns both an `entries` array and a `formatted`
string. The model receives both and pays for both — 159 chars/entry versus 26.
Formatted text only.

**Errors are results, never throws.** A thrown error becomes a transport error the
model cannot read. `{ success: false, error: "codemap is unavailable..." }` lets it
adapt.

**No unrequested work.** `list` does not parse symbols unless asked (4x faster
without). `map` never reads file bodies. The budget is honoured exactly, because a
tool that overshoots its budget is a tool whose budget cannot be trusted.

**Path security is the host's, not ours.** `resolveWorkspacePath` before any
filesystem access, exactly like `read` and `dir_list`. A tool that bypasses it is a
path-traversal bug with a nice name.

### What to measure after wiring it

The tool existing is not the same as the agent using it. Two numbers:

- **Tool reach rate**: in 20 unfamiliar-repo tasks, how often does the agent call
  `sightline_map` before `dir_list` or `grep`? If it is under ~30%, the descriptions
  are not doing their job and no amount of engine work matters.
- **Tokens per task**: compare against a run with the tools removed. If it does not
  drop, the tools are being ignored or used badly.

`bench/mcp_probe.py` verifies the protocol, and `bench/qa.py` verifies the engine,
but neither can tell you whether an agent reaches for it. That needs the agent.

---

## 2. Naming

The current name has problems. "codemap" is generic, already means a few unrelated
things, and does not survive a search. Here is how to pick, rather than a list of
guesses.

### What the thing actually is

Not a map. A **retrieval layer**: it decides what an agent looks at, which is the
one decision that determines both cost and correctness. Everything else — parsing,
ranking, formatting — serves that.

The differentiator is not "fewer tokens". Everyone claims that. It is:

- **3x fewer tokens at higher accuracy than grep**, measured against oracles
- **Complete**: every non-binary file, including configs and dotfiles
- **Honest**: it reports what it could not parse instead of omitting silently

### Naming constraints that actually matter

1. **Sayable once, spelled right first try.** If someone hears it and cannot find
   it, the name is dead. Reject anything with ambiguous spelling.
2. **Not a metaphor that needs explaining.** `codemap` needs none; `archivist` needs
   a sentence.
3. **Free on npm and GitHub** — check before you fall in love.
4. **Not a common English word.** `survey`, `index`, `atlas` are all taken hundreds
   of times and are unsearchable.
5. **No `-map`, `-graph`, `-code` suffix.** They read as a category, not a product,
   and there are already dozens.

### Directions, with tradeoffs

| Direction | Example | Tradeoff |
|---|---|---|
| **The act of looking** | `sightline`, `periscope`, `vantage` | Concrete, memorable, no code connotation. `sightline` is the strongest: it means the line between what you can and cannot see. |
| **The act of choosing** | `cull`, `winnow`, `pare` | Says exactly what it does. Risk: sounds like it deletes things. |
| **A narrowing instrument** | `aperture`, `focal`, `lens` | Accurate and pretty. `aperture` is taken by a photo app; `focal` collides with ML. |
| **Surveying** | `theodolite`, `plumb`, `benchmark` | Precise and unusual. Too long to type, spells badly. |
| **Short and unclaimed** | `kemap`, `scry`, `trawl` | Cheap to own. `scry` (to see remotely) is short, spellable, and unclaimed in dev tools. |

### The two I would actually ship

**`sightline`** — it names the real value. A sightline is what you can see from
where you stand, and this tool changes where the agent stands. It is spellable,
searchable, has no dev-tool collision, and works as a verb in an agent context
("sightline this repo"). Domain: `sightline.dev`.

**`scry`** — if you want maximum memorability and minimum syllables. Four letters,
one syllable, means "to perceive by means other than sight", which is what a
structural map is. Risk: fantasy-novel connotation, and some people will not know
the word. Check npm before committing.

Avoid: anything with `map`, `graph`, `code`, `repo`, `context`, or `token` in it.
Those read as npm packages, not products, and you will never rank for them.

### One naming test before you commit

Write the sentence you would put at the top of the README, using the name:

> **sightline** gives a coding agent a complete picture of a repository in one call,
> so it reads the right file instead of five plausible ones. Measured at 3x fewer
> tokens and 24 points more accurate than grep across 21 repositories.

If that sentence sounds natural, the name works. If you have to explain the name in
the sentence, it does not.

---

## 3. What I would rename, concretely

| Thing | Now | Suggestion |
|---|---|---|
| repo | `codemap` | `sightline` |
| binary | `codemap` | `sight` or keep `codemap` as the binary inside `sightline` |
| tools | `sightline_map` | `sight`, `scan`, `outline`, `locate`, `importers` |
| index dir | `.codemap/` | `.sightline/` |

Tool names matter more than the repo name, because they are the words the model
reads on every request. `locate` and `importers` describe the action; `find` and
`callers` describe the implementation.
