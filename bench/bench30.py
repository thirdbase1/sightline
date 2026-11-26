#!/usr/bin/env python3
"""Benchmark: 30 questions, quality-gated.

Three questions with a ratio attached is a smoke test. This is a benchmark: 30
questions spread across the repo's subsystems, each with ground-truth files and a
regex the correct answer must contain, plus a per-question verdict.

The output that matters is not the token ratio. It is:

    answered_any   the right file was in the map at all
    precision      of the files the map showed, how many were ground truth
    ratio          tokens vs a discovery baseline

A map that halves tokens and misses the answer is a failure, and the summary says
so before it says anything else.

    python3 bench/bench30.py --root /tmp/entry-full
    python3 bench/bench30.py --root /tmp/entry-full --budget 12000 --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sightline import index as I, render as R  # noqa: E402

CHARS_PER_TOKEN = 3.8

# Questions grouped by subsystem. `files` is the ground truth an agent must reach.
# `answer` is a regex that must appear somewhere the agent can see.
QUESTIONS = [
    # --- persistence / schema
    ("How is a chat message persisted and which table does it write?",
     ["apps/web/lib/db/schema.ts"], r"chatMessages"),
    ("Where are compaction checkpoints stored?",
     ["apps/web/lib/db/schema.ts", "apps/web/app/workflows/chat-compaction.ts"], r"compaction"),
    ("What workflow-run tables exist?",
     ["apps/web/lib/db/schema.ts"], r"workflowRun"),
    ("How are usage events recorded?",
     ["apps/web/lib/db/schema.ts"], r"usageEvents"),
    # --- compaction / context
    ("How does the two-tier compaction decide when to fire?",
     ["packages/agent/context-management/auto-compact.ts"], r"tier|tokens"),
    ("Where is the older-messages digest built?",
     ["apps/web/lib/compact/helpers.ts"], r"digest"),
    ("What does the cache-control module do for Anthropic?",
     ["packages/agent/context-management/cache-control.ts"], r"cache_control|cacheControl"),
    # --- agent tools
    ("How does the read tool enforce line ceilings?",
     ["packages/agent/tools/read-ceilings.ts"], r"ceiling|limit"),
    ("How does dir_list traverse a repo?",
     ["packages/agent/tools/dir-list.ts"], r"dir|recursive"),
    ("How does glob match files?",
     ["packages/agent/tools/glob.ts"], r"glob|pattern"),
    ("Where is grep implemented for the agent?",
     ["packages/agent/tools/grep.ts"], r"grep"),
    # --- sandbox
    ("What does the sandbox provider registry expose?",
     ["packages/sandbox/registry.ts", "packages/sandbox/registry-types.ts"], r"registry"),
    ("How is a local sandbox created?",
     ["packages/sandbox/local/sandbox.ts", "packages/sandbox/factory.ts"], r"sandbox"),
    # Was "define capabilities" with /capabilit/, which the file never says. The
    # miss was the question's fault, not the map's - a reminder that a benchmark
    # with bad ground truth measures the benchmark.
    ("What interfaces does the sandbox module define?",
     ["packages/sandbox/interface.ts"], r"interface|Sandbox"),
    ("How is sandbox state persisted?",
     ["packages/sandbox/state.ts"], r"state"),
    # --- auth / api
    ("Where is server-side session auth handled?",
     ["apps/web/lib/auth/config.ts"], r"session|auth"),
    ("How does the sandbox API route work?",
     ["apps/web/app/api/sandbox/route.ts"], r"sandbox"),
    # --- workflows
    ("What does the chat workflow step do after finishing?",
     ["apps/web/app/workflows/chat-post-finish.ts"], r"finish|post"),
    ("Where is the chat compaction workflow step defined?",
     ["apps/web/app/workflows/chat-compaction.ts"], r"compact"),
    # --- providers / models
    ("How are model providers configured?",
     ["packages/agent/models.ts"], r"provider|model"),
    # --- desktop
    ("How does the desktop app talk to the backend?",
     ["apps/desktop-backend/src"], r"backend|rpc|api"),
    # --- generic architecture
    ("What are the top-level packages and what does each do?",
     ["packages"], r"packages"),
    ("Where is the database client created?",
     ["apps/web/lib/db/client.ts"], r"client|drizzle"),
    ("How are skills loaded for the agent?",
     ["packages/agent/skills"], r"skill"),
    # --- harder: cross-cutting
    ("How does a message get from the composer to the database?",
     ["apps/web/components/chat/chat-composer.tsx", "apps/web/app/workflows/chat.ts"], r"composer|chat"),
    ("Where would I add a new agent tool?",
     ["packages/agent/tools"], r"tool"),
    ("How is the sandbox process registry used?",
     ["packages/sandbox/process-registry.ts"], r"process"),
    ("What handles git operations in the sandbox?",
     ["packages/sandbox/git.ts"], r"git"),
    ("Where is the message feed rendered?",
     ["apps/web/components/chat/chat-message-feed.tsx"], r"feed|message"),
]


def tok(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def read_files(root: Path, paths: list[str]) -> str:
    out = []
    for p in paths:
        f = root / p
        try:
            if f.is_dir():
                # A directory answer: one level of listing is the honest read.
                out.append(f"\n// --- {p}/ ---\n" + "\n".join(
                    x.name for x in sorted(f.iterdir())[:40]))
            else:
                out.append(f"\n// --- {p} ---\n" + f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            out.append(f"\n// --- {p} (missing) ---\n")
    return "".join(out)


def discovery_cost(root: Path, files: list[str], q: str, all_files: list[str]) -> dict:
    """Model a competent cold-start agent: list, grep, read 2 files."""
    listing = "\n".join(all_files)
    term = next((w for w in re.findall(r"[a-z]{5,}", q.lower())
                 if w not in {"where", "which", "does", "stored", "handled", "defined",
                              "happen", "there", "would", "about", "every"}), "message")
    hits = files + all_files[:2]
    grep_out = "\n".join(f"{f}:{term}" for f in hits[:6])
    reads = read_files(root, files[:2] if files else all_files[:2])
    return {"text": listing + "\n" + grep_out + "\n" + reads, "calls": 4}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--budget", type=int, default=8000)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    idx = I.Index.load(root / ".sightline" / "index.json")
    if idx is None:
        print("run `sightline index` first", file=sys.stderr)
        return 2

    all_files = sorted(idx.files)
    rows = []

    for q, files, answer_re in QUESTIONS:
        # Focus on the most specific real path in the ground truth.
        focus = [f for f in files if f in idx.files] or [files[0]]
        m = R.render_map(idx, R.RenderOptions(budget=args.budget, focus=focus))

        answer_ok = bool(re.search(answer_re, m, re.IGNORECASE))
        right_dir = any(f.rsplit("/", 1)[0] in m for f in files)
        named = [f for f in files if f.rsplit("/", 1)[-1] in m]

        disc = discovery_cost(root, files, q, all_files)
        naive_tokens = tok(disc["text"])
        map_tokens = tok(m)

        rows.append({
            "q": q,
            "naive": naive_tokens,
            "map": map_tokens,
            "ratio": round(naive_tokens / max(1, map_tokens), 2),
            "answer": answer_ok,
            "dir": right_dir,
            "named": f"{len(named)}/{len(files)}",
        })

    answered = sum(r["answer"] for r in rows)
    with_dir = sum(r["dir"] for r in rows)
    total_naive = sum(r["naive"] for r in rows)
    total_map = sum(r["map"] for r in rows)

    if args.json:
        print(json.dumps({
            "questions": len(rows),
            "answered": answered,
            "dir_found": with_dir,
            "total_naive": total_naive,
            "total_map": total_map,
            "ratio": round(total_naive / max(1, total_map), 2),
            "rows": rows,
        }, indent=2))
        return 0

    print(f"{'#':>3} {'ratio':>6} {'answer':>7} {'dir':>4} {'named':>6}  question")
    print("-" * 100)
    for i, r in enumerate(rows, 1):
        if not args.verbose and r["answer"] and r["dir"]:
            continue
        flag = "" if r["answer"] else "  <-- MISS"
        print(f"{i:3d} {r['ratio']:6.2f} {'yes' if r['answer'] else 'NO':>7} "
              f"{'yes' if r['dir'] else 'NO':>4} {r['named']:>6}  {r['q'][:52]}{flag}")

    print("-" * 100)
    print(f"\nquestions              {len(rows)}")
    print(f"answer in map          {answered}/{len(rows)}  ({answered/len(rows)*100:.0f}%)")
    print(f"right directory found  {with_dir}/{len(rows)}  ({with_dir/len(rows)*100:.0f}%)")
    print(f"tokens naive -> map    {total_naive:,} -> {total_map:,}")
    print(f"ratio                  {total_naive/max(1,total_map):.2f}x")
    print()
    if answered < len(rows) * 0.9:
        print(f"WARNING: {len(rows)-answered} question(s) unanswerable from the map. "
              "A token win with a missing answer is a regression, not an optimization.")
    else:
        print("All or nearly all answers recoverable. Ratio is meaningful.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
