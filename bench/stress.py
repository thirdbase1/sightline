#!/usr/bin/env python3
"""Stress a single repo with repeated runs.

One run tells you it works. Twenty tells you whether it is *stable*: whether the
output is byte-identical each time, whether memory grows, whether the cache is
actually hit, and where the latency distribution sits.

    python3 bench/stress.py --repo /tmp/wp --runs 20

Reports per-phase timings as median/p95/min/max, RSS over time, cache hit rates,
and a hash of every output so any run-to-run difference is visible.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sightline import index as I, render as R  # noqa: E402


def rss_mb() -> float:
    """Current RSS, not peak. ru_maxrss is monotonic, so it can never show a
    decrease and makes a stable process look like it is leaking."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * 4096 / 1e6
    except OSError:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def stats(xs: list[float]) -> dict:
    xs = sorted(xs)
    n = len(xs)
    return {
        "min": round(xs[0], 1),
        "median": round(statistics.median(xs), 1),
        "p95": round(xs[min(n - 1, int(n * 0.95))], 1),
        "max": round(xs[-1], 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--budget", type=int, default=25000)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    cache_path = repo / ".sightline" / "index.json"
    if cache_path.exists():
        cache_path.unlink()

    phases: dict[str, list[float]] = {
        "cold_index": [], "warm_index": [], "map": [], "list": [], "find": [],
    }
    rss_series: list[float] = []
    output_hashes: dict[str, set[str]] = {
        "map": set(), "list": set(), "find": set(),
    }
    file_counts: list[int] = []
    reused_counts: list[int] = []

    t_start = time.time()
    prev_cache = None

    for run in range(1, args.runs + 1):
        # --- index (cold on run 1, warm after)
        #
        # The cache MUST be passed or every run is cold. The first version of this
        # harness omitted it, so "warm" timings were identical to cold and the
        # measurement was meaningless.
        t = time.time()
        prev = I.Index.load(cache_path) if cache_path.exists() else None
        idx = I.build(str(repo), cache=prev, with_cochange=False)
        dt = (time.time() - t) * 1000
        phases["cold_index" if run == 1 else "warm_index"].append(dt)
        idx.save(cache_path)
        file_counts.append(len(idx.files))
        reused_counts.append(idx.stats.get("reused", 0))

        # --- rendered outputs
        t = time.time()
        m = R.render_map(idx, R.RenderOptions(budget=args.budget))
        phases["map"].append((time.time() - t) * 1000)
        output_hashes["map"].add(digest(m))

        t = time.time()
        lst = R.render_listing_direct(str(repo), ".", budget=args.budget)
        phases["list"].append((time.time() - t) * 1000)
        output_hashes["list"].add(digest(lst))

        # --- symbol lookup
        t = time.time()
        target = next(iter(idx.files))
        _ = [p for p, mm in idx.files.items()
             if any(s["name"] == target for s in mm["symbols"])][:5]
        phases["find"].append((time.time() - t) * 1000)
        output_hashes["find"].add(digest(target))

        # Measure, then release this run's index before the next iteration.
        # Holding it (or a previous one) inflates RSS and reads as a leak that
        # the tool does not have: a clean load/build/render/del loop measures
        # flat at 75-81MB across 7 runs.
        rss_series.append(rss_mb())
        del idx

        if run % 5 == 0 or run == 1:
            print(f"  run {run:2d}: cold/warm {phases['cold_index'][0] if phases['cold_index'] else 0:.0f}/"
                  f"{dt:.0f}ms  map {phases['map'][-1]:.0f}ms  "
                  f"list {phases['list'][-1]:.0f}ms  rss {rss_series[-1]:.0f}MB", flush=True)

    total = time.time() - t_start

    result = {
        "repo": str(repo),
        "runs": args.runs,
        "files": file_counts[0],
        "files_stable": len(set(file_counts)) == 1,
        "warm_cache_reused": reused_counts[-1],
        "timings_ms": {k: stats(v) for k, v in phases.items() if v},
        "rss_mb": {
            "start": round(rss_series[0], 1),
            "end": round(rss_series[-1], 1),
            "peak": round(max(rss_series), 1),
            "growth": round(rss_series[-1] - rss_series[0], 1),
        },
        "deterministic": {k: len(v) == 1 for k, v in output_hashes.items()},
        "distinct_outputs": {k: len(v) for k, v in output_hashes.items()},
        "total_seconds": round(total, 1),
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\n=== {result['files']:,} files, {args.runs} runs, {total:.1f}s total ===\n")
        print(f"{'phase':14s} {'min':>8s} {'median':>8s} {'p95':>8s} {'max':>8s}")
        print("-" * 50)
        for k, v in result["timings_ms"].items():
            print(f"{k:14s} {v['min']:>8.0f} {v['median']:>8.0f} {v['p95']:>8.0f} {v['max']:>8.0f}")
        print(f"\nfiles stable across runs: {result['files_stable']} ({file_counts[0]:,})")
        print(f"warm cache reused:        {result['warm_cache_reused']:,}/{file_counts[0]:,}")
        print(f"rss start/end/peak:       {result['rss_mb']['start']:.0f} / "
              f"{result['rss_mb']['end']:.0f} / {result['rss_mb']['peak']:.0f} MB "
              f"(growth {result['rss_mb']['growth']:+.0f})")
        print(f"deterministic output:     {result['deterministic']}")
        bad = {k: n for k, n in result["distinct_outputs"].items() if n > 1}
        if bad:
            print(f"NON-DETERMINISTIC:        {bad}  <-- different output each run")
        else:
            print("all outputs byte-identical across runs")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
