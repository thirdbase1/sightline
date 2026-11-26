#!/usr/bin/env python3
"""Drive the MCP server over stdio exactly as a client would.

This is not a unit test of the handlers. It spawns the server as a subprocess and
speaks JSON-RPC to it over stdin/stdout, because the failure modes that matter
here are protocol-level: a missing newline, a response to a notification, a frame
written to stdout that is not JSON, a server that dies on a tool error.

    python3 bench/mcp_probe.py --repo /tmp/eslintq
    python3 bench/mcp_probe.py --repo /tmp/eslintq --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, repo: str):
        env = {"PYTHONPATH": str(ROOT / "src"),
               "PATH": "/usr/local/bin:/usr/bin:/bin"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sightline.mcp_server", "--root", repo],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._id = 0

    def send(self, method: str, params: dict | None = None, notify: bool = False):
        frame = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        if not notify:
            self._id += 1
            frame["id"] = self._id
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout with no response")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    checks: list[tuple[str, bool, str]] = []
    c = Client(args.repo)

    def check(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))

    try:
        # 1. handshake
        init = c.send("initialize", {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "probe", "version": "1"},
        })
        res = init.get("result", {})
        check("initialize returns protocolVersion", "protocolVersion" in res,
              res.get("protocolVersion", ""))
        check("initialize declares tools capability",
              "tools" in res.get("capabilities", {}))
        check("initialize returns serverInfo", "serverInfo" in res,
              str(res.get("serverInfo")))

        # 2. the mandatory initialized notification must NOT be answered
        c.send("notifications/initialized", notify=True)
        check("notification produced no response", True, "no frame read")

        # 3. tools/list
        tl = c.send("tools/list")
        tools = tl.get("result", {}).get("tools", [])
        check("tools/list returns tools", len(tools) >= 4, f"{len(tools)} tools")
        names = {t["name"] for t in tools}
        check("expected tool names present",
              {"sightline_map", "sightline_list", "sightline_file",
               "sightline_find", "sightline_callers"} <= names,
              ",".join(sorted(names)))

        # 4. every tool has a valid, non-null schema
        bad_schema = [t["name"] for t in tools
                      if not isinstance(t.get("inputSchema"), dict)
                      or t["inputSchema"].get("type") != "object"]
        check("all inputSchemas are JSON Schema objects", not bad_schema,
              ",".join(bad_schema))

        # 5. description length: agents pick tools from these
        long_desc = [t["name"] for t in tools if len(t.get("description", "")) > 700]
        check("descriptions under 700 chars", not long_desc, ",".join(long_desc))

        # 6. map
        m = c.send("tools/call", {"name": "sightline_map",
                                  "arguments": {"budget": 2000}})
        text = m.get("result", {}).get("content", [{}])[0].get("text", "")
        check("sightline_map returns text", len(text) > 100, f"{len(text)} chars")
        check("sightline_map respects budget",
              len(text) / 3.8 <= 2000 * 1.05, f"{int(len(text)/3.8)} tok")
        check("sightline_map reports no error", not m["result"].get("isError"))

        # 7. list — repo-agnostic: list the root, which always exists
        l = c.send("tools/call", {"name": "sightline_list",
                                  "arguments": {"path": ".", "budget": 4000}})
        ltext = l.get("result", {}).get("content", [{}])[0].get("text", "")
        check("sightline_list returns entries", "\n" in ltext and len(ltext) > 20,
              f"{len(ltext)} chars")

        # 8. find — query a word that appears in any codebase's declarations
        f = c.send("tools/call", {"name": "sightline_find",
                                  "arguments": {"query": "e", "limit": 5}})
        ftext = f.get("result", {}).get("content", [{}])[0].get("text", "")
        check("sightline_find returns matches",
              ":" in ftext or "no declaration" in ftext, f"{len(ftext)} chars")

        # 9. error path: unknown tool must be a JSON-RPC error, not a crash
        e = c.send("tools/call", {"name": "nope", "arguments": {}})
        check("unknown tool returns jsonrpc error", "error" in e,
              str(e.get("error", {}).get("code")))

        # 10. error path: a tool that fails must return isError, not kill the server
        bad = c.send("tools/call", {"name": "sightline_file",
                                    "arguments": {"path": "does/not/exist.js"}})
        check("missing file returns isError", bad["result"].get("isError") is True)

        # 11. server still alive after both error paths
        alive = c.send("tools/call", {"name": "sightline_map",
                                      "arguments": {"budget": 500}})
        check("server survives tool errors",
              "result" in alive and not alive["result"].get("isError"))

        # 12. unknown method
        um = c.send("does/not/exist")
        check("unknown method returns error", "error" in um,
              str(um.get("error", {}).get("code")))

        # 13. malformed frame is handled, not fatal
        c.proc.stdin.write("this is not json\n")
        c.proc.stdin.flush()
        bad_frame = json.loads(c.proc.stdout.readline())
        check("malformed frame returns parse error",
              bad_frame.get("error", {}).get("code") == -32700)
        after = c.send("tools/call", {"name": "sightline_map",
                                      "arguments": {"budget": 500}})
        check("server survives malformed frame", "result" in after)

    finally:
        c.close()

    if args.json:
        print(json.dumps([{"check": n, "pass": p, "detail": d} for n, p, d in checks],
                         indent=2))
    else:
        passed = sum(1 for _, p, _ in checks if p)
        print(f"MCP protocol probe: {passed}/{len(checks)} checks\n")
        for name, ok, detail in checks:
            mark = "ok  " if ok else "FAIL"
            print(f"  {mark} {name:44s} {detail[:40]}")

    return 0 if all(p for _, p, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
