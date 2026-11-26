#!/usr/bin/env python3
"""Large-repo harness: measures what big codebases actually stress.

The 118-repo corpus measured correctness across languages. This measures
**scale**: peak RSS, index size on disk, walk time, and whether a fixed token
budget still says anything useful at 50k files.

Differences from corpus.py:
  - records peak RSS via resource.getrusage (large repos OOM before they get slow)
  - records index size in MB (a 30 MB JSON index is a real artifact to ship)
  - records files-on-disk vs files-indexed (walk skip-ratio: are we mis-skipping?)
  - samples many budgets from one index (rendering is cheap; indexing is not)
  - deletes the clone immediately after indexing to fit a small disk

    python3 bench/large.py --list bench/repos-large.txt --out /tmp/large.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

CLONE_ROOT = Path("/tmp/large")
CLONE_TIMEOUT_S = 60
INDEX_TIMEOUT_S = 300


def clone(slug: str) -> tuple[Path | None, str]:
    dest = CLONE_ROOT / slug.replace("/", "__")
    shutil.rmtree(dest, ignore_errors=True)
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    try:
        r = subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--filter=blob:none",
             "--single-branch", "--no-tags",
             f"https://github.com/{slug}.git", str(dest)],
            capture_output=True, text=True, timeout=CLONE_TIMEOUT_S, env=env,
        )
        if r.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            return None, (r.stderr.strip().splitlines() or ["?"])[-1][:130]
    except subprocess.TimeoutExpired:
        # A 500 MB partial tree can take seconds to delete; do it, then confirm.
        subprocess.run(["rm", "-rf", str(dest)], capture_output=True)
        shutil.rmtree(dest, ignore_errors=True)
        return None, f"clone timeout {CLONE_TIMEOUT_S}s"
    except OSError as e:
        return None, str(e)[:130]
    return dest, ""


def disk_files(p: Path) -> int:
    n = 0
    for _, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if d != ".git"]
        n += len(files)
    return n


def mb(p: Path) -> float:
    try:
        return round(p.stat().st_size / 1e6, 2)
    except OSError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-size-mb", type=int, default=1400)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from sightline import index as I, render as R

    # A previous run killed mid-clone leaves its checkout behind, because SIGKILL
    # does not run `finally`. That silently halves available disk and the next run
    # fails on space rather than on anything diagnosable.
    if CLONE_ROOT.exists():
        for stale in CLONE_ROOT.iterdir():
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

    repos = [l.strip() for l in Path(args.list).read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    if args.limit:
        repos = repos[: args.limit]

    out = Path(args.out)
    done: set[str] = set()
    if out.exists():
        for line in out.read_text().splitlines():
            try:
                rec = json.loads(line)
                if rec.get("error") != "attempted":
                    done.add(rec["repo"])
            except Exception:
                pass

    print(f"{len(repos)} repos, {len(done)} already done", flush=True)

    for i, slug in enumerate(repos, 1):
        if slug in done:
            continue

        free_mb = shutil.disk_usage("/tmp").free / 1e6
        if free_mb < 700:
            print(f"ABORT: only {free_mb:.0f} MB free, need headroom for a clone",
                  file=sys.stderr, flush=True)
            return 3

        # Record the attempt up front. If this repo wedges and the run is killed,
        # the next run skips it instead of retrying the same hang. Without this the
        # corpus could never make progress past its slowest clone.
        with out.open("a") as fh:
            fh.write(json.dumps({"repo": slug, "error": "attempted"}) + "\n")

        t0 = time.time()
        path, err = clone(slug)
        if path is None:
            with out.open("a") as fh:
                fh.write(json.dumps({"repo": slug, "error": err}) + "\n")
            print(f"[{i:3d}/{len(repos)}] {slug:42s} CLONE {err[:44]}", flush=True)
            continue

        clone_s = time.time() - t0
        n_disk = disk_files(path)
        # Size must skip .git. An rglob over a 900 MB shallow clone's .git walks
        # hundreds of thousands of pack files and takes minutes - this, not
        # cloning, is what stalled earlier runs.
        total = 0
        for root_d, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for f in files:
                try:
                    fp = Path(root_d) / f
                    if not fp.is_symlink():
                        total += fp.stat().st_size
                except OSError:
                    continue
        size_mb = round(total / 1e6, 1)

        if size_mb > args.max_size_mb:
            shutil.rmtree(path, ignore_errors=True)
            with out.open("a") as fh:
                fh.write(json.dumps({"repo": slug, "error": "too_large",
                                     "size_mb": size_mb, "files_disk": n_disk}) + "\n")
            print(f"[{i:3d}/{len(repos)}] {slug:42s} SKIP {size_mb} MB", flush=True)
            continue

        n_disk = disk_files(path)
        base_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        try:
            import signal
            signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(
                TimeoutError("index cap")))
            signal.alarm(INDEX_TIMEOUT_S)
            try:
                ti = time.time()
                idx = I.build(str(path), with_cochange=False)
                index_s = round(time.time() - ti, 2)
            finally:
                signal.alarm(0)

            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            cache = path / ".sightline" / "index.json"
            idx.save(cache)
            index_mb = mb(cache)

            # One index, many budgets: rendering is O(budget), not O(repo).
            renders = {}
            for b in (2000, 8000, 25000):
                t = time.time()
                m = R.render_map(idx, R.RenderOptions(budget=b))
                renders[str(b)] = {
                    "tokens": int(len(m) / R.CHARS_PER_TOKEN),
                    "ms": int((time.time() - t) * 1000),
                    "mode": "rollup" if "showing directories" in m else "files",
                }

            conf = idx.stats.get("confidence", {})
            parsed = conf.get("exact", 0) + conf.get("likely", 0)
            rec = {
                "repo": slug,
                "size_mb": size_mb,
                "files_disk": n_disk,
                "clone_s": round(clone_s, 1),
                "index_s": index_s,
                "index_mb": index_mb,
                "peak_rss_mb": round(peak_rss / 1024, 1),
                "files": idx.stats.get("files", 0),
                "loc": idx.stats.get("loc", 0),
                "edges": idx.stats.get("edges", 0),
                "symbols": sum(len(f["symbols"]) for f in idx.files.values()),
                "parsed_pct": round(parsed / max(1, idx.stats.get("files", 1)) * 100, 1),
                "skip_ratio": round((1 - idx.stats.get("files", 0) / max(1, n_disk)) * 100, 1),
                "kfiles_per_s": round(idx.stats.get("files", 0) / max(0.01, index_s), 0),
                "renders": renders,
            }
        except TimeoutError:
            rec = {"repo": slug, "error": f"index timeout {INDEX_TIMEOUT_S}s",
                   "size_mb": size_mb, "files_disk": n_disk}
        except Exception as e:
            rec = {"repo": slug, "error": f"{type(e).__name__}: {e}"[:150],
                   "size_mb": size_mb, "files_disk": n_disk}
        finally:
            shutil.rmtree(path, ignore_errors=True)

        with out.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

        if "error" in rec:
            print(f"[{i:3d}/{len(repos)}] {slug:42s} ERR {rec['error'][:44]}", flush=True)
        else:
            print(
                f"[{i:3d}/{len(repos)}] {slug:42s} {rec['files']:6d}f "
                f"{rec['loc']:>9,}loc {rec['parsed_pct']:5.1f}% "
                f"idx {rec['index_mb']:6.1f}MB rss {rec['peak_rss_mb']:6.0f}MB "
                f"{rec['index_s']:5.1f}s", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
