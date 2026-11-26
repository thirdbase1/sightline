"""CLI. The agent-facing surface is `map` — one call, budgeted output.

    sightline index  [--root .]
    sightline map    [--budget 8000] [--focus src/auth]
    sightline file   <path>
    sightline find   <query>
    sightline callers <symbol>
    sightline stats
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import index as I, render as R


def _cache_path(root: str) -> Path:
    return Path(root).resolve() / ".sightline" / "index.json"


def cmd_index(args) -> int:
    root = args.root
    t0 = time.time()
    cache = I.Index.load(_cache_path(root))
    idx = I.build(root, cache=cache, with_cochange=not args.no_cochange)
    elapsed = time.time() - t0
    idx.save(_cache_path(root))

    stats = dict(idx.stats)
    stats["build_seconds"] = round(elapsed, 2)
    stats["cache_hit"] = bool(cache)
    print(json.dumps(stats, indent=2))
    return 0


def _load(root: str) -> I.Index:
    idx = I.Index.load(_cache_path(root))
    if idx is None:
        sys.stderr.write("no index; run `sightline index` first\n")
        raise SystemExit(2)
    return idx


def cmd_map(args) -> int:
    idx = _load(args.root)
    focus = args.focus or []
    out = R.render_map(idx, R.RenderOptions(budget=args.budget, focus=focus,
                                            max_files=args.max_files))
    print(out)
    return 0


def cmd_list(args) -> int:
    # Direct filesystem path: no index, no extraction, ~40ms. Falls back to the
    # indexed renderer only when symbol counts are needed from the full extractor.
    if args.direct or not _cache_path(args.root).exists():
        print(R.render_listing_direct(args.root, path=args.path,
                                      budget=args.budget,
                                      count_symbols=not args.no_symbols), end="")
        return 0
    idx = _load(args.root)
    print(R.render_listing(idx, path=args.path, budget=args.budget), end="")
    return 0


def cmd_file(args) -> int:
    idx = _load(args.root)
    out = R.render_file(idx, args.path)
    if out is None:
        sys.stderr.write(f"not in index: {args.path}\n")
        return 2
    print(out)
    return 0


def cmd_find(args) -> int:
    idx = _load(args.root)
    pat = re.compile(args.query, re.IGNORECASE)
    hits = []
    for path, meta in idx.files.items():
        for s in meta["symbols"]:
            hay = f"{s['name']} {s.get('signature') or ''}"
            if pat.search(hay):
                hits.append((idx.ranks.get(path, 0.0), path, s))
    hits.sort(key=lambda h: -h[0])
    for rank, path, s in hits[: args.limit]:
        parent = f"{s['parent']}." if s.get("parent") else ""
        print(f"{path}:{s['line']}  {s['kind']:9s} {parent}{s['signature'] or s['name']}")
    if not hits:
        print(f"no symbol matching {args.query!r}")
    return 0


def cmd_callers(args) -> int:
    """Who imports the file defining `symbol`. One traversal, not N reads."""
    idx = _load(args.root)
    defining = [
        p for p, m in idx.files.items()
        if any(s["name"] == args.symbol for s in m["symbols"])
    ]
    if not defining:
        print(f"no file defines {args.symbol!r}")
        return 0

    # Resolve importers through the real graph, not string containment.
    #
    # The original version matched `Path(path).stem in spec`, so `schema.ts`
    # matched anything importing *any* module called `schema`. On a real repo that
    # reported 67 importers where grep finds 51 - a false-positive rate of 31%,
    # and false positives here are worse than misses because the agent then reads
    # files that cannot possibly contain the answer.
    from . import graph as G

    class _Info:
        def __init__(self, p, imps):
            self.path = p
            self.imports = imps

    infos = {p: _Info(p, m["imports"]) for p, m in idx.files.items()}
    g = G.resolve_imports(infos, roots=[idx.root])

    for path in defining[:5]:
        importers = sorted(g.reverse.get(path, ()), key=lambda x: -idx.ranks.get(x, 0))
        print(f"{args.symbol} defined in {path} [{idx.files[path]['confidence']}]")
        print(f"  imported by {len(importers)} file(s):")
        for p in sorted(importers, key=lambda x: -idx.ranks.get(x, 0))[: args.limit]:
            print(f"    {p}")
    return 0


def cmd_actions(args) -> int:
    """List the capabilities this build supports, for self-describing clients.

    A harness registers a fixed tool schema and cannot learn a new one mid-session.
    A single `action` parameter sidesteps that: new capabilities arrive as new
    values, and this command tells the client what the installed build can do.
    """
    print(json.dumps({
        "version": __import__("sightline").__version__,
        "actions": {
            "map": "token-budgeted structural map of the repository",
            "list": "complete inventory of one directory",
            "file": "declarations in one file, with signatures",
            "find": "declarations matching a name or signature",
            "callers": "files importing the file that defines a symbol",
        },
    }, indent=2))
    return 0


def cmd_stats(args) -> int:
    idx = _load(args.root)
    print(json.dumps(idx.stats, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="sightline",
        epilog="Run `sightline actions` for the capabilities this build supports.",
    )
    p.add_argument("--root", default=".")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("index", help="build or update the index")
    i.add_argument("--no-cochange", action="store_true", help="skip git history")
    i.set_defaults(func=cmd_index)

    m = sub.add_parser("map", help="render a token-budgeted map")
    m.add_argument("--budget", type=int, default=8000)
    m.add_argument("--focus", action="append", help="path or dir to bias toward")
    m.add_argument("--max-files", type=int, default=400)
    m.set_defaults(func=cmd_map)

    l = sub.add_parser("list", help="compact complete inventory (beats a listing)")
    l.add_argument("path", nargs="?", default=".")
    l.add_argument("--budget", type=int, default=4000)
    l.add_argument("--direct", action="store_true",
                   help="read the filesystem, skip the index")
    l.add_argument("--no-symbols", action="store_true",
                   help="skip declaration counting (~4x faster)")
    l.set_defaults(func=cmd_list)

    f = sub.add_parser("file", help="full symbols for one file")
    f.add_argument("path")
    f.set_defaults(func=cmd_file)

    d = sub.add_parser("find", help="search symbols by name")
    d.add_argument("query")
    d.add_argument("--limit", type=int, default=30)
    d.set_defaults(func=cmd_find)

    c = sub.add_parser("callers", help="who imports the defining file")
    c.add_argument("symbol")
    c.add_argument("--limit", type=int, default=20)
    c.set_defaults(func=cmd_callers)

    a = sub.add_parser("actions", help="list supported actions (self-describing)")
    a.set_defaults(func=cmd_actions)

    s = sub.add_parser("stats", help="index statistics")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
