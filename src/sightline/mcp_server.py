"""MCP server for sightline. Stdlib only, stdio transport.

Why no SDK: an agent tool that requires `pip install mcp` inside every sandbox is
a tool agents will not have. This speaks the protocol directly over stdin/stdout,
which is ~200 lines and has no dependency the host might lack.

Protocol: MCP 2026-07-28, JSON-RPC 2.0, newline-delimited over stdio.

    python3 -m sightline.mcp_server --root /path/to/repo

Register with any MCP client as a stdio server; see mcp.json.example.

The tool set is deliberately small. An agent picks tools by reading short
descriptions, so five well-named tools with precise schemas beat fifteen
overlapping ones. Every description states *when to reach for it*, because that is
the decision an agent actually gets wrong.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "sightline"
SERVER_VERSION = "0.1.0"

# Populated in main().
_ROOT: Path = Path(".")
_INDEX = None  # lazily built


# --------------------------------------------------------------------- helpers

def _log(msg: str) -> None:
    """Diagnostics go to stderr. stdout is reserved for JSON-RPC frames."""
    sys.stderr.write(f"[sightline] {msg}\n")
    sys.stderr.flush()


def _get_index(root: Path):
    """Load the cached index, or build it. Cached for the process lifetime.

    A cold build of a 53k-file repo took 19.8s on the first tool call, which an
    agent cannot distinguish from a hang. Two mitigations:

      1. Load the saved index directly and skip the walk entirely when the cache
         exists and is fresh. The walk exists to detect changes; an MCP server
         lives for one session, so paying 3s per call for change detection is
         waste. Index once at install, then this is a ~200ms JSON load.
      2. When there is no usable cache, say so in the tool response before doing
         the work, so a slow first call is legible rather than mysterious.
    """
    global _INDEX
    from sightline import index as I

    cache_path = root / ".sightline" / "index.json"
    if _INDEX is not None:
        return _INDEX

    cached = I.Index.load(cache_path)
    if cached is not None and cached.stats.get("extractor_version") == I.EXTRACTOR_VERSION:
        # Trust the saved index. Set CODEMAP_REFRESH=1 to force a re-walk.
        import os
        if os.environ.get("CODEMAP_REFRESH") != "1":
            _INDEX = cached
            return _INDEX

    _INDEX = I.build(str(root), cache=cached, with_cochange=False)
    try:
        _INDEX.save(cache_path)
    except OSError:
        pass
    return _INDEX


def _cold_start_note(root: Path) -> str:
    cache = root / ".sightline" / "index.json"
    if cache.exists():
        return ""
    return (
        "# No index found for this repository, so the first call builds one.\n"
        "# A large repo can take 10-20s; later calls are milliseconds.\n"
        "# Run `sightline index` once at install to avoid this.\n"
    )


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


# ----------------------------------------------------------------------- tools

TOOLS: list[dict[str, Any]] = [
    {
        "name": "sightline_map",
        "title": "Repository map",
        "description": (
            "A token-budgeted structural map of the whole repository: directories, "
            "files, and their declarations, ranked by importance. Call this FIRST in "
            "an unfamiliar repository, before grep. Measured at 3x fewer tokens than "
            "grep-and-read for the same questions. Start with budget 2000; if the "
            "output says it is 'showing directories', either raise the budget or use "
            "root to focus on one area."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "budget": {
                    "type": "integer",
                    "description": "Token budget for the output. 2000 for orientation, "
                                   "8000 for feature work, 25000 for a large repo.",
                    "default": 2000,
                    "minimum": 200,
                    "maximum": 200000,
                },
                "root": {
                    "type": "string",
                    "description": "Optional subdirectory to focus ranking on, e.g. "
                                   "'src/auth'. Re-ranks toward that area without "
                                   "increasing the budget.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sightline_list",
        "title": "File inventory",
        "description": (
            "A complete, compact inventory of one directory: every file with size and "
            "declaration count. Use when you need to know WHAT FILES EXIST, rather "
            "than what the code does. 3x fewer tokens than a directory listing and it "
            "includes the symbol count per file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the repo root. Omit for the "
                                   "whole repository.",
                    "default": ".",
                },
                "budget": {
                    "type": "integer",
                    "description": "Token budget for the output.",
                    "default": 4000,
                    "minimum": 200,
                    "maximum": 200000,
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "sightline_file",
        "title": "File outline",
        "description": (
            "Every declaration in one file, with signatures: functions, classes, "
            "types, constants, plus what it imports. Use INSTEAD OF reading a file "
            "when you only need its API surface. Costs about 40 tokens per file "
            "versus 13,000 to read a 50 KB file."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the repo root.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sightline_find",
        "title": "Find symbol",
        "description": (
            "Search declarations by name or signature across the repository. Returns "
            "file:line and the declaration. Use when you know the NAME of a symbol "
            "and want its location; use grep instead when you are searching for a "
            "string literal or a usage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Substring or regex to match against declaration "
                                   "names and signatures.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results.",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sightline_callers",
        "title": "Find importers",
        "description": (
            "Which files import the file that defines a symbol. Use before editing a "
            "shared function to see the blast radius, in one call instead of a grep "
            "plus a read per hit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Exact declaration name.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum files to list.",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    },
]


# ------------------------------------------------------------------ tool impls

def _tool_map(args: dict) -> dict:
    from sightline import index as I, render as R

    root = _ROOT
    note = _cold_start_note(root)
    idx = _get_index(root)
    focus = [args["root"]] if args.get("root") else None
    out = R.render_map(idx, R.RenderOptions(
        budget=int(args.get("budget", 2000)), focus=focus,
    ))
    return _ok(note + out)


def _tool_list(args: dict) -> dict:
    from sightline import render as R

    path = args.get("path", ".") or "."
    out = R.render_listing_direct(str(_ROOT), path, budget=int(args.get("budget", 4000)))
    return _ok(out)


def _tool_file(args: dict) -> dict:
    from sightline import render as R

    idx = _get_index(_ROOT)
    out = R.render_file(idx, args["path"])
    if out is None:
        return _err(f"not in index: {args['path']}. "
                    f"Try sightline_find, or sightline_list on its directory.")
    return _ok(out)


def _tool_find(args: dict) -> dict:
    import re

    idx = _get_index(_ROOT)
    try:
        pat = re.compile(args["query"], re.IGNORECASE)
    except re.error as e:
        return _err(f"invalid regex {args['query']!r}: {e}")

    hits = []
    for path, meta in idx.files.items():
        for s in meta["symbols"]:
            hay = f"{s['name']} {s.get('signature') or ''}"
            if pat.search(hay):
                hits.append((idx.ranks.get(path, 0.0), path, s))
    hits.sort(key=lambda h: -h[0])

    if not hits:
        return _ok(f"no declaration matching {args['query']!r}. "
                   f"If it might be a string or usage rather than a declaration, "
                   f"use grep.")

    lines = []
    for _rank, path, s in hits[: int(args.get("limit", 30))]:
        parent = f"{s['parent']}." if s.get("parent") else ""
        lines.append(f"{path}:{s['line']}  {s['kind']:9s} {parent}{s['signature'] or s['name']}")
    return _ok("\n".join(lines))


def _tool_callers(args: dict) -> dict:
    from sightline import graph as G

    idx = _get_index(_ROOT)
    symbol = args["symbol"]
    defining = [p for p, m in idx.files.items()
                if any(s["name"] == symbol for s in m["symbols"])]
    if not defining:
        return _ok(f"no file defines {symbol!r}. "
                   f"Use sightline_find to check the name.")

    class _Info:
        def __init__(self, p, imps):
            self.path = p
            self.imports = imps

    infos = {p: _Info(p, m["imports"]) for p, m in idx.files.items()}
    g = G.resolve_imports(infos, roots=[str(_ROOT)])

    lines = []
    for path in defining[:5]:
        importers = sorted(g.reverse.get(path, ()), key=lambda x: -idx.ranks.get(x, 0))
        lines.append(f"{symbol} defined in {path} [{idx.files[path]['confidence']}]")
        lines.append(f"  imported by {len(importers)} file(s):")
        for p in importers[: int(args.get("limit", 20))]:
            lines.append(f"    {p}")
    return _ok("\n".join(lines))


HANDLERS = {
    "sightline_map": _tool_map,
    "sightline_list": _tool_list,
    "sightline_file": _tool_file,
    "sightline_find": _tool_find,
    "sightline_callers": _tool_callers,
}


# ------------------------------------------------------------------ json-rpc io

def _respond(msg_id, result=None, error=None) -> None:
    frame = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        frame["error"] = error
    else:
        frame["result"] = result
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> None:
    method = req.get("method")
    msg_id = req.get("id")
    params = req.get("params") or {}

    # Notifications carry no id and must not be answered.
    if msg_id is None and method and method.startswith("notifications/"):
        return

    if method == "initialize":
        _respond(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Structural map of the repository at this server's root. "
                "Call sightline_map before grep in unfamiliar code."
            ),
        })
        return

    if method == "tools/list":
        _respond(msg_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if handler is None:
            _respond(msg_id, error={"code": -32602,
                                    "message": f"unknown tool: {name}"})
            return
        try:
            _respond(msg_id, handler(args))
        except Exception as e:  # a tool crash must not kill the server
            _log(f"{name} failed: {traceback.format_exc()}")
            _respond(msg_id, _err(f"{type(e).__name__}: {e}"))
        return

    if method == "ping":
        _respond(msg_id, {})
        return

    if msg_id is not None:
        _respond(msg_id, error={"code": -32601,
                                "message": f"method not found: {method}"})


def main(argv: list[str] | None = None) -> int:
    global _ROOT
    import argparse

    ap = argparse.ArgumentParser(prog="sightline-mcp")
    ap.add_argument("--root", default=".", help="repository root to map")
    args = ap.parse_args(argv)

    _ROOT = Path(args.root).resolve()
    _log(f"root={_ROOT}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _respond(None, error={"code": -32700, "message": "parse error"})
            continue
        try:
            _handle(req)
        except Exception:
            _log(traceback.format_exc())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
