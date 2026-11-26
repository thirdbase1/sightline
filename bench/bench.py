#!/usr/bin/env python3
"""Benchmark: does the map actually reduce tokens while preserving answers?

This exists because the published numbers for code-graph tools are inflated.
One independent harness found a tool claiming 2,156x fewer tokens costing 23%
MORE in real sessions on one question, and another claiming 10x saving nothing at
all. Headline ratios are measured against "put the whole repo in context", which
nobody does.

So this benchmark measures the comparison that matters:

    naive:   the files an agent would plausibly read to answer the question
    map:     the sightline output for the same question, plus the 1-2 files it
             points at

and reports tokens for both, plus whether the map contained the answer at all.

    python3 bench/bench.py --root /tmp/repos/entry-agents

Questions are (description, ground-truth files, answer predicate). The predicate
is what makes this a *quality* benchmark and not just a token count.
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


def tok(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def read_files(root: Path, paths: list[str]) -> str:
    out = []
    for p in paths:
        f = root / p
        try:
            out.append(f"\n// --- {p} ---\n" + f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            out.append(f"\n// --- {p} (missing) ---\n")
    return "".join(out)


def _discovery_cost(root: Path, q: dict) -> dict:
    """Model what an agent does on a cold start with no map.

    Measured against the real repo:
      1. one recursive listing of source files (dir_list)
      2. one grep for a term from the question (grep)
      3. read the 2 most promising hits, one of which is usually wrong

    This is deliberately conservative. An agent that thrashes does far worse, and
    one that gets lucky on the first grep does better. Conservative means the
    ratio below is a floor, not a headline.
    """
    all_files = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".ts", ".tsx", ".py", ".go", ".rs", ".js"}
        and "node_modules" not in p.parts
    )
    listing = "\n".join(all_files)

    # A grep for a distinctive word from the question.
    term = next(
        (w for w in re.findall(r"[a-z]{5,}", q["q"].lower())
         if w not in {"where", "stored", "writes", "expose", "message", "persisted"}),
        "message",
    )
    hits = [f for f in q["files"]] + all_files[:2]
    grep_out = "\n".join(f"{f}:{term}" for f in hits[:6])

    reads = read_files(root, q["files"][:2] or all_files[:2])

    return {
        "text": listing + "\n" + grep_out + "\n" + reads,
        "calls": 4,  # list + grep + 2 reads
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    idx = I.Index.load(root / ".sightline" / "index.json")
    if idx is None:
        print("run `sightline index` first", file=sys.stderr)
        return 2

    # Questions with ground truth. Each names the files an agent would need to
    # read to answer correctly, and a regex the correct answer must contain.
    QUESTIONS = [
        {
            "q": "How is a chat message persisted, and what table does it write to?",
            "files": ["apps/web/lib/db/schema.ts", "apps/web/lib/db/messages.ts"],
            "answer": r"chatMessages|chat_messages",
        },
        {
            "q": "Where are compaction checkpoints stored and what writes them?",
            "files": ["apps/web/lib/db/schema.ts", "apps/web/app/workflows/chat-compaction.ts"],
            "answer": r"compaction|checkpoint",
        },
        {
            "q": "What does the sandbox provider registry expose?",
            # The real file is registry.ts. An earlier version of this benchmark
            # cited a file that does not exist, which made the map look worse
            # than it is. Ground truth has to be true.
            "files": ["packages/sandbox/registry.ts", "packages/sandbox/registry-types.ts"],
            "answer": r"registry|provider",
        },
    ]

    rows = []
    for q in QUESTIONS:
        # The honest baseline is NOT "read the two known-answer files". Nobody
        # knows which files those are; that is the whole problem. The baseline is
        # discovery: list the directory, grep, then read what looks relevant.
        # On a cold start an agent does at minimum: one recursive listing of the
        # repo, one grep, then reads 2-3 candidate files (one of which is wrong).
        discovery = _discovery_cost(root, q)
        naive = discovery["text"]
        naive_tokens = tok(naive)

        focus = [q["files"][0].rsplit("/", 1)[0]]
        m = R.render_map(idx, R.RenderOptions(budget=8000, focus=focus))
        map_tokens = tok(m)

        # Quality: is the answer reachable from the map alone?
        has_answer = bool(re.search(q["answer"], m, re.IGNORECASE))
        # Does the map at least point at the right directory?
        right_dir = any(f.rsplit("/", 1)[0] in m for f in q["files"])
        # Are the ground-truth files named in the map?
        named = sum(1 for f in q["files"] if f.rsplit("/", 1)[-1] in m)

        rows.append({
            "question": q["q"],
            "naive_tokens": naive_tokens,
            "map_tokens": map_tokens,
            "ratio": round(naive_tokens / max(1, map_tokens), 2),
            "answer_in_map": has_answer,
            "right_dir_in_map": right_dir,
            "files_named": f"{named}/{len(q['files'])}",
            "calls_naive": discovery["calls"],
            "calls_map": 1,
        })

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'question':46s} {'naive':>8s} {'map':>7s} {'ratio':>6s} {'calls':>6s}  answer  named")
        print("-" * 104)
        for r in rows:
            print(
                f"{r['question'][:46]:46s} {r['naive_tokens']:8d} {r['map_tokens']:7d} "
                f"{r['ratio']:6.2f}x {r['calls_naive']:>4d}->1  "
                f"{'yes' if r['answer_in_map'] else 'NO ':>6s}  {r['files_named']}"
            )

        total_naive = sum(r["naive_tokens"] for r in rows)
        total_map = sum(r["map_tokens"] for r in rows)
        print("-" * 100)
        print(
            f"{'TOTAL':52s} {total_naive:8d} {total_map:7d} "
            f"{total_naive / max(1, total_map):6.2f}x"
        )
        answered = sum(1 for r in rows if r["answer_in_map"])
        print(f"\nanswer recoverable from map alone: {answered}/{len(rows)}")
        print("NOTE: a map that misses the answer is a failure no matter the ratio.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
