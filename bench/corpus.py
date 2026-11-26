#!/usr/bin/env python3
"""Corpus harness: clone a repo, index it, measure, delete it.

Disk is the constraint (2.9 GB free), so this never holds more than one clone.
Everything measured is written to JSONL immediately, so an interrupted run keeps
its results.

    python3 bench/corpus.py --list repos.txt --out corpus.jsonl
    python3 bench/corpus.py --list repos.txt --out corpus.jsonl --budget 2000

Each line of repos.txt is `owner/name`. Lines starting with # are ignored.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CLONE_ROOT = Path("/tmp/corpus")


def sh(cmd: list[str], timeout: int = 300, cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as e:
        return 127, str(e)


CLONE_TIMEOUT_S = 90


def clone(slug: str) -> tuple[Path | None, str]:
    """Depth-1, blobless clone with a hard timeout and no partial state.

    A per-repo cap matters more than a generous one: one slow clone previously
    stalled an entire 121-repo run at position 74, and the corpus harness has no
    way to resume around a wedged child. 90s is enough for a blobless depth-1
    clone of most repositories and fails fast on the rest.
    """
    dest = CLONE_ROOT / slug.replace("/", "__")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)

    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1",
           "GIT_CONFIG_NOSYSTEM": "1"}
    import os
    full_env = {**os.environ, **env}

    try:
        r = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
             "--single-branch", "--no-tags",
             f"https://github.com/{slug}.git", str(dest)],
            capture_output=True, text=True, timeout=CLONE_TIMEOUT_S, env=full_env,
        )
        code, out = r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        code, out = 124, f"clone timeout after {CLONE_TIMEOUT_S}s"
    except OSError as e:
        code, out = 127, str(e)

    if code != 0 or not dest.exists():
        # Never leave a partial clone behind; it would poison a later run.
        shutil.rmtree(dest, ignore_errors=True)
        return None, (out.splitlines()[-1] if out else f"clone failed ({code})")[:160]
    return dest, ""


def dir_size_mb(p: Path) -> float:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            continue
    return round(total / 1e6, 1)


def file_count(p: Path) -> int:
    try:
        return sum(1 for _ in p.rglob("*") if _.is_file())
    except OSError:
        return 0


def measure(path: Path, idx, budget: int) -> dict:
    """Top-line metrics for one indexed repo."""
    from sightline import render as R

    files = idx.stats.get("files", 0)
    if files == 0:
        return {"files": 0}

    start = time.time()
    m = R.render_map(idx, R.RenderOptions(budget=budget))
    render_ms = int((time.time() - start) * 1000)

    tokens = int(len(m) / R.CHARS_PER_TOKEN)
    named = sum(1 for line in m.split("\n") if line.strip().endswith("]"))
    conf = idx.stats.get("confidence", {})
    parsed = conf.get("exact", 0) + conf.get("likely", 0)

    # Symbol density: how much structure we actually extracted.
    total_symbols = sum(len(f["symbols"]) for f in idx.files.values())

    return {
        "files": files,
        "loc": idx.stats.get("loc", 0),
        "edges": idx.stats.get("edges", 0),
        "symbols": total_symbols,
        "parsed_pct": round(parsed / max(1, files) * 100, 1),
        "unparsed": conf.get("none", 0),
        "map_tokens": tokens,
        "map_files": named,
        "render_ms": render_ms,
        "within_budget": tokens <= budget,
        "coverage_pct": round(named / max(1, files) * 100, 2),
        "tokens_per_1k_loc": round(tokens / max(1, idx.stats.get("loc", 1)) * 1000, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep", action="store_true", help="do not delete clones")
    args = ap.parse_args()

    from sightline import index as I

    repos = [
        l.strip() for l in Path(args.list).read_text().splitlines()
        if l.strip() and not l.startswith("#")
    ]
    if args.limit:
        repos = repos[: args.limit]

    out = Path(args.out)
    done: set[str] = set()
    if out.exists():
        for line in out.read_text().splitlines():
            try:
                done.add(json.loads(line)["repo"])
            except Exception:
                continue
    print(f"{len(repos)} repos, {len(done)} already done", flush=True)

    skipped = {}

    for i, slug in enumerate(repos, 1):
        if slug in done:
            continue
        t0 = time.time()
        path, err = clone(slug)

        if path is None:
            skipped[slug] = err
            print(f"[{i:3d}/{len(repos)}] {slug:45s} CLONE FAIL  {err[:50]}", flush=True)
            continue

        size = dir_size_mb(path)
        nfiles = file_count(path)

        # Too big to index in the remaining budget? Record and move on.
        if size > 900:
            rec = {"repo": slug, "error": "too_large", "size_mb": size, "files_disk": nfiles}
            shutil.rmtree(path, ignore_errors=True)
            out.open("a").write(json.dumps(rec) + "\n")
            print(f"[{i:3d}/{len(repos)}] {slug:45s} SKIP {size} MB", flush=True)
            continue

        try:
            import signal

            def _alarm(signum, frame):
                raise TimeoutError("index exceeded per-repo cap")

            old = signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(120)  # a repo that takes >2min to index is out of scope
            try:
                idx = I.build(str(path), with_cochange=False)
                stats = measure(path, idx, args.budget)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old)
            stats.update({
                "repo": slug,
                "size_mb": size,
                "files_disk": nfiles,
                "index_s": round(time.time() - t0, 2),
                "budget": args.budget,
            })
        except Exception as e:
            stats = {"repo": slug, "error": f"{type(e).__name__}: {e}"[:160],
                     "size_mb": size, "files_disk": nfiles}

        if not args.keep:
            shutil.rmtree(path, ignore_errors=True)

        with out.open("a") as fh:
            fh.write(json.dumps(stats) + "\n")

        if "error" in stats:
            print(f"[{i:3d}/{len(repos)}] {slug:45s} ERROR {stats['error'][:40]}", flush=True)
        else:
            print(
                f"[{i:3d}/{len(repos)}] {slug:45s} {stats['files']:5d}f "
                f"{stats['loc']:>8,}loc {stats['parsed_pct']:5.1f}%parsed "
                f"{stats['map_tokens']:5d}tok {stats['index_s']:5.1f}s",
                flush=True,
            )

    if skipped:
        print(f"\n{len(skipped)} clone failures", file=sys.stderr)
        for k, v in list(skipped.items())[:5]:
            print(f"  {k}: {v}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
