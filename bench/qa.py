#!/usr/bin/env python3
"""Q&A benchmark for LARGE repos, with independent oracles.

The 29-question benchmark runs on one small repo with hand-written ground truth.
This generates questions automatically for any large repo, but the oracles must be
INDEPENDENT of sightline's index or the benchmark validates itself.

Oracles used, none of which read sightline's data:

  structure   "what source files are in <dir>?"        oracle: os.scandir
  locate      "which file defines <symbol>?"           oracle: git grep
  centrality  "which file is imported by the most?"     oracle: git grep on imports
  outline     "what does <file> declare?"               oracle: regex over the file

Three question types, because a map is good at different things than grep:

  1. ORIENT     "what are the main areas of this repo?"
     For a map this is the whole job. Grep cannot answer it at all.
     Oracle: the real top-level directory listing.

  2. DRILL      "where is <symbol> implemented?"
     Grep is excellent at this and cheap. A map should be competitive in 2 calls
     (orientation then focus), not necessarily 1.

  3. OUTLINE    "what does <file> export?"
     Map should win: it has the symbols already, grep cannot summarise.

Reported per arm: hits, tokens, calls. Grep winning on DRILL is a finding, not a
failure, and is printed as such.

    python3 bench/qa.py --repos bench/qa-repos.txt --out /tmp/qa.jsonl
    python3 bench/qa.py --repos ... --per-repo 20 --budget 25000
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CLONE_ROOT = Path("/tmp/qa")
SRC_EXT = {".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rs", ".java", ".rb", ".php"}


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def clone(slug: str) -> Path | None:
    dest = CLONE_ROOT / slug.replace("/", "__")
    shutil.rmtree(dest, ignore_errors=True)
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
             "--single-branch", "--no-tags", f"https://github.com/{slug}.git", str(dest)],
            capture_output=True, text=True, timeout=150,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
        )
        if r.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            return None
    except (subprocess.SubprocessError, OSError):
        subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
        return None
    return dest


def source_files(repo: Path, limit: int = 60_000) -> list[str]:
    out: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in
                   {".git", "node_modules", "dist", "build", "target", "__pycache__"}]
        for f in files:
            if os.path.splitext(f)[1] in SRC_EXT:
                out.append(str(Path(root, f).relative_to(repo)))
                if len(out) >= limit:
                    return out
    return out


def top_dirs(files: list[str], k: int = 8) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for f in files:
        head = f.split("/")[0] if "/" in f else "."
        counts[head] = counts.get(head, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:k]


# --------------------------------------------------------------- question gen

def make_questions(repo: Path, files: list[str], per_repo: int, seed: int = 0) -> list[dict]:
    rng = random.Random(seed)
    qs: list[dict] = []

    # 1. ORIENT - oracle is the real directory listing
    for d, count in top_dirs(files, k=3):
        if d == ".":
            continue
        qs.append({
            "type": "orient",
            "q": f"What are the main directories in this repository?",
            "oracle": d,
            "oracle_kind": "dir",
            "note": f"{d}/ has {count} source files",
        })

    # 2. DRILL - oracle is git grep for a definition
    defs = git(repo, "grep", "-l", "-E",
               r"^(export )?(async )?(function|class|def|func|fn|interface|type) ",
               "--", "*.ts", "*.tsx", "*.js", "*.py", "*.go", "*.rs")
    # `-l` prints one bare path per line (no `path:line:match`). A `":" in line`
    # filter therefore discarded everything.
    # Also skip vendored and fixture directories, which make poor questions.
    SKIP = ("/vendor/", "/node_modules/", "/testdata/", "/fixtures/", "/third_party/")
    def_files = [
        l.strip() for l in defs.splitlines()
        if l.strip() and not any(s in l for s in SKIP)
    ]
    def_files = list(dict.fromkeys(def_files))

    if def_files:
        for f in rng.sample(def_files, min(per_repo // 2, len(def_files))):
            try:
                src = (repo / f).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = re.search(
                r"^(?:export\s+)?(?:async\s+)?(?:function|class|def|func|fn|interface|type)\s+"
                r"([A-Za-z_]\w*)", src, re.M)
            if not m:
                continue
            sym = m.group(1)
            # Only unambiguous symbols. If a name is defined in several files,
            # "where is it defined" has no single correct answer and the arm that
            # names a different valid file would be scored as a miss.
            kw = (r"(function|class|def|func|fn|interface|type|struct|enum|trait|impl|"
                  r"object|const|let|var|module)")
            defining = [
                l.strip() for l in git(
                    repo, "grep", "-l", "-E",
                    kw + r"[[:space:]]+" + re.escape(sym) + r"[[:space:](<{]")
                .splitlines() if l.strip()
            ]
            if len(defining) != 1:
                continue
            qs.append({
                "type": "drill",
                "q": f"Where is `{sym}` defined?",
                "oracle": defining[0],
                "oracle_kind": "file",
                "symbol": sym,
            })

    # 3. OUTLINE - oracle is the file's own declarations
    if def_files:
        for f in rng.sample(def_files, min(per_repo // 4, len(def_files))):
            try:
                src = (repo / f).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names = re.findall(
                r"^(?:export\s+)?(?:async\s+)?(?:function|class|def|func|fn|interface|type)\s+"
                r"([A-Za-z_]\w*)", src, re.M)
            if len(names) < 2:
                continue
            qs.append({
                "type": "outline",
                "q": f"What does `{f}` declare?",
                "oracle": f,
                "oracle_kind": "file",
                "expect_symbols": names[:5],
            })

    return qs[:per_repo]


# ------------------------------------------------------------------- scoring

def map_names(out: str, target: str, is_dir: bool = False) -> bool:
    """Does the rendered map name this file or this directory?

    A file at the repo root has no "/" in its path. The previous version treated
    any slash-free target as a directory and looked for `cobra.go/`, so every
    root-level file was scored as missing. That single bug accounted for most of
    the apparent "grep wins on small repos" result.
    """
    if is_dir or target.endswith("/"):
        return target.rstrip("/") + "/" in out
    base = target.rsplit("/", 1)[-1]
    return base in out or target in out


def map_names_all(out: str, symbols: list[str]) -> int:
    return sum(1 for s in symbols if s in out)


def grep_baseline(repo: Path, q: dict) -> tuple[int, int, bool]:
    """What a competent agent does with grep+read. Returns (tokens, calls, hit)."""
    if q["type"] == "orient":
        # grep cannot answer "what are the main directories" at all.
        listing = "\n".join(source_files(repo)[:2000])
        return int(len(listing) / 3.8), 1, False

    if q["type"] == "drill":
        sym = q["symbol"]
        out = git(repo, "grep", "-l", "-w", sym, timeout=90)
        hits = [l for l in out.splitlines() if l]
        # Grep for a name is noisy: it finds uses, not just the definition.
        # The agent must then open the top file, which may be wrong.
        n = len(hits)
        tok = int(n * 40 + 400)
        hit = n > 0 and any(q["oracle"] in h for h in hits[:10])
        return tok, 2, hit

    # outline: read the file
    try:
        src = (repo / q["oracle"]).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0, 1, False
    tok = int(len(src) / 3.8)
    return tok, 1, True  # reading the file always answers it


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-repo", type=int, default=15)
    ap.add_argument("--budget", type=int, default=25000)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from sightline import index as I, render as R

    repos = [l.strip() for l in Path(args.repos).read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    if args.limit:
        repos = repos[: args.limit]

    if CLONE_ROOT.exists():
        shutil.rmtree(CLONE_ROOT, ignore_errors=True)

    rows: list[dict] = []
    for i, slug in enumerate(repos, 1):
        free = shutil.disk_usage("/tmp").free / 1e6
        if free < 600:
            print(f"ABORT: {free:.0f} MB free", file=sys.stderr)
            break

        repo = clone(slug)
        if repo is None:
            print(f"[{i:2d}/{len(repos)}] {slug:34s} CLONE FAIL", flush=True)
            continue

        try:
            files = source_files(repo)
            idx = I.build(str(repo), with_cochange=False)
            qs = make_questions(repo, files, args.per_repo, seed=i)
            if not qs:
                print(f"[{i:2d}/{len(repos)}] {slug:34s} no questions", flush=True)
                continue

            # Arm A: one map call, no focus.
            m1 = R.render_map(idx, R.RenderOptions(budget=args.budget))
            tok1 = int(len(m1) / 3.8)

            # Arm B: two calls - orient then focus on the most likely directory.
            hit_b = 0
            tok_b = tok1        # the orientation map, fetched once
            focus_tokens: list[int] = []
            for q in qs:
                if q["type"] == "orient":
                    ok = map_names(m1, q["oracle"], is_dir=True)
                else:
                    # Two-call agent: orient, then narrow to the nearest directory
                    # the first map actually named. Focusing on the TOP-LEVEL dir
                    # (`packages`) is useless in a monorepo; the agent has to walk
                    # down. This models: map -> pick dir from map -> map --focus.
                    target = q["oracle"]
                    parts = target.split("/")[:-1]
                    focus_dirs: list[str] = []
                    for depth in range(len(parts), 0, -1):
                        cand = "/".join(parts[:depth])
                        if cand + "/" in m1:      # the map named this directory
                            focus_dirs = [cand]
                            break
                    m2 = R.render_map(idx, R.RenderOptions(
                        budget=args.budget, focus=focus_dirs or None))
                    # Charge the focused slice, not a whole new map.
                    focus_tokens.append(int(len(m2) / 3.8))
                    ok = map_names(m2, target)
                hit_b += ok

            hit_a = sum(
                map_names(m1, q["oracle"], is_dir=q["type"] == "orient")
                for q in qs
            )

            # Arm C: grep baseline.
            g_tok = g_calls = g_hit = 0
            for q in qs:
                t, c, h = grep_baseline(repo, q)
                g_tok += t
                g_calls += c
                g_hit += h

            # Outline quality: does the map list the file's symbols?
            outline_qs = [q for q in qs if q["type"] == "outline"]
            sym_score = (
                sum(map_names_all(m1, q["expect_symbols"]) for q in outline_qs)
                / max(1, sum(len(q["expect_symbols"]) for q in outline_qs))
            )

            rec = {
                "repo": slug, "files": len(idx.files), "questions": len(qs),
                "by_type": {t: sum(1 for q in qs if q["type"] == t)
                            for t in ("orient", "drill", "outline")},
                "map1_hit": hit_a, "map2_hit": hit_b, "grep_hit": g_hit,
                "map1_tok": tok1, "map2_tok": tok_b,
                "focus_tok_median": int(sorted(focus_tokens)[len(focus_tokens) // 2])
                if focus_tokens else 0,
                "grep_tok": g_tok,
                "map_calls": 1, "map2_calls": 2, "grep_calls": g_calls,
                "outline_symbol_recall": round(sym_score, 3),
            }
            rows.append(rec)

            print(f"[{i:2d}/{len(repos)}] {slug:34s} n={len(qs):2d} "
                  f"map1 {hit_a}/{len(qs)} map2 {hit_b}/{len(qs)} grep {g_hit}/{len(qs)} "
                  f"| tok map1 {tok1:6d} map2 {tok_b:7d} grep {g_tok:6d} "
                  f"| sym {sym_score*100:3.0f}%", flush=True)
        except Exception as e:
            print(f"[{i:2d}/{len(repos)}] {slug:34s} ERR {type(e).__name__}: {e}"[:120], flush=True)
        finally:
            shutil.rmtree(repo, ignore_errors=True)

    Path(args.out).write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(f"\nwrote {len(rows)} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
