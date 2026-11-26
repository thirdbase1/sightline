# The read tool: what is right, what is wrong, and the architecture that fixes it

You described both halves yourself, and both are correct:

> I love that read tool the way it forces the model to read before changing a file,
> so it doesn't edit files it doesn't know about. But I hate that it always forces
> a re-read and I know we are wasting context and money.

The gate is right. The mechanism that implements the gate is where the money goes.
Those are separable, and separating them is the whole design.

---

## What the current architecture does

`read-state.ts` keeps a `Map<path, contentHash>` of files the model has *seen*. On
every step, `rebuildReadFileState` clears that map and walks the whole message
history, folding each `read` tool result's `contentHash` back into it. `write.ts`
then refuses to edit unless `readState.get(path) === hash(disk)`.

The comment in `read-state.ts` states the consequence plainly:

> once auto-compaction collapses an old read payload to a placeholder notice, its
> hash is no longer derivable — the model must re-read.

So the loop is:

```
read file (13,000 tokens)
  → compaction collapses it to a notice
    → gate no longer has the hash
      → write refuses: "read it again"
        → read file again (13,000 tokens)
```

Compaction exists to *save* tokens. Here it causes a 13,000-token re-read, and the
re-read becomes the next thing compaction has to collapse. On a long session that
is the dominant cost, and it is entirely self-inflicted.

### The three specific defects

**1. Evidence is inferred from the transcript.** The gate reconstructs *"has the
model seen this?"* by re-parsing tool results. Anything that rewrites the
transcript — compaction, summarization, subagent handoff — destroys evidence that
is still true. A fact about the model's state is being stored in the model's
context, which is the one place that is designed to be lossy.

**2. The hash is whole-file; the read is partial.** `read.ts` records
`hashFileContent(content)` of the **entire** normalized file, but it can return
only 500 lines. So a model that read lines 1–500 of a 2,000-line file holds a
hash asserting it saw the whole thing. That is unsafe in the other direction —
the gate is weaker than it looks — and it also means a partial read cannot be
"topped up"; any later read must re-send from line 1.

**3. Dedup keys on offset and limit.** `dedupKey =
"${cwd}:${path}:${offset}:${limit}"`. Asking for the same file at a *different*
range is always a full read, even when the file has not changed and the model has
already seen most of those lines.

---

## The architecture

Three changes. None of them touch the safety property — the gate stays exactly as
strong, and gets stronger in one respect.

### 1. Move evidence out of the transcript

The read-state map must be **persistent, per-session state**, not something
rebuilt from messages.

```ts
interface ReadEvidence {
  path: string;
  contentHash: string;     // sha256 of the full normalized file
  size: number;            // bytes
  mtimeMs: number;         // invalidates cheaply before hashing
  ranges: Array<[number, number]>;  // line ranges actually returned
  totalLines: number;
  seenAtStep: number;
}
type ReadLedger = Map<string, ReadEvidence>;
```

Stored in the session record, not in the messages. Compaction then cannot touch
it, because it never lives in a message. `rebuildReadFileState` disappears.

This is strictly better for safety: today, a compaction that drops a read result
*weakens* the gate's memory while the model may still "know" the content. With a
ledger, the gate's belief is accurate and independent of what the transcript
happens to still contain.

### 2. Make the gate range-aware

```ts
function canEdit(ledger, path, disk): GateResult {
  const ev = ledger.get(path);
  if (!ev) return { ok: false, reason: "unread" };

  const st = statSync(path);
  if (st.size !== ev.size || st.mtimeMs !== ev.mtimeMs) {
    if (hash(disk) !== ev.contentHash) return { ok: false, reason: "stale" };
  }
  // Covering the whole file means the whole-file edit is safe.
  if (ev.ranges covers [1, ev.totalLines]) return { ok: true };

  // Partial knowledge: allow a ranged edit, refuse a whole-file overwrite.
  return { ok: false, reason: "partial", covered: ev.ranges };
}
```

The rule becomes: **you may edit what you have seen.** A model that read lines
1–500 of a 2,000-line file may use `edit` on a string within those lines, and may
not use `write` to overwrite the file. That is both safer than today (today a
partial read authorizes a whole-file overwrite) and less annoying (today the
model is pushed to re-read 2,000 lines to change one).

### 3. Serve reads from the ledger

This is where the tokens come back.

```ts
function read(path, offset, limit) {
  const ev = ledger.get(path);
  const diskHash = hash(disk);

  if (ev && ev.contentHash === diskHash) {
    const missing = [offset, offset+limit-1] minus ev.ranges;
    if (missing is empty) {
      return { text: "[already read: lines X–Y, unchanged since step N]" };
      // ~15 tokens instead of ~13,000
    }
    return { text: only the missing ranges, with a note saying so };
  }
  return full window, and record the range
}
```

The model asks for what it needs. If it already saw it, it gets a 15-token
pointer instead of 13,000 tokens of content it still holds earlier in the
transcript. If it needs a new range, it gets *only* the new range.

Measured cost of a real file in this repo: `chat.ts` is 271,270 bytes ≈ **71,000
tokens**. A single unnecessary re-read of it is larger than an entire 8,000-token
map. Three re-reads of that one file are 213,000 tokens — more than the whole
remaining context of a 200k model.

---

## What "the model still holds it earlier in the transcript" actually means

This is the objection, and it deserves a straight answer.

The model does *not* reliably still hold it. Attention over 100k tokens degrades;
a line read 40 steps ago may as well be absent. So a ledger-backed read that says
"you already read this" is only safe if the content is genuinely recoverable.

Three options, and the right one is a policy, not a constant:

| Policy | Behaviour | When |
|---|---|---|
| **Strict** | re-send if the read is older than N steps | schemas, files being edited |
| **Trusting** | pointer only | large files read for reference |
| **Hybrid** | pointer + a 10-line anchor at the requested offset | default |

Hybrid is the honest default: it costs ~120 tokens instead of 13,000, and it puts
the first ten lines of the requested region in front of the model so it can tell
whether its memory is stale. That is enough to catch "I'm thinking of the old
version of this function", which is the failure the full re-read was preventing.

The policy belongs on the tool call (`freshness: "auto" | "force"`), so the model
can ask for a real re-read when it is unsure — which it does far less often than
the current design assumes.

---

## The second half: reads that were never needed

Re-reads are one cost. Reads of files the model did not need are the other, and
they are larger.

`sightline_file` already answers "what does this file declare" at ~40 tokens instead
of 71,000. The read tool should refuse to be the default path for that question —
not by hiding, but by the description:

```
WHEN NOT TO USE:
- You need the file's API, not its body (use sightline_file: 40 tok vs 71,000)
```

And the inverse: `sightline_file` says "if you are about to EDIT it, read it". The
two descriptions point at each other, which is what makes a suite navigable.

---

## Build order

1. **Ledger, no behaviour change.** Store evidence in the session; keep the gate
   identical. Verify: a compaction no longer forces a re-read. One test.
2. **Range-aware gate.** Partial read authorizes ranged edit, not overwrite.
   Verify with the existing `write.ts` tests plus two new ones.
3. **Ledger-backed reads with the hybrid policy.** This is the token win. Verify:
   read → read again → second response under 200 tokens.
4. **Then** measure a real session. Not before. Steps 1–3 are each one file and
   each independently testable.

## The numbers to prove it

- **Re-reads per session**, before and after, on the same task.
- **Tokens spent on `read` results**, before and after.
- **Gate correctness**: does any edit still target content the model has not seen?
  This must be zero, and it is the number that can veto the whole design.

If re-reads drop by 60% and gate correctness stays perfect, this is done. If gate
correctness moves at all, the design is wrong regardless of the savings.
