import { tool } from "ai";
import { z } from "zod";
import { getSandbox, toDisplayPath } from "./utils";
import { resolveWorkspacePath } from "./path-security";
import { type ToolResult, ok, fail, clampToBudget, explainExecFailure }
  from "./core";

/**
 * sightline — ONE tool, stable schema, evolving capabilities.
 *
 * WHY ONE TOOL AND NOT FIVE
 *
 * A harness registers tool definitions once. When you ship a sixth tool, every
 * client has to change its code before anyone can use it; when you add a
 * parameter to an existing tool, the same. That is the real cost of a tool suite,
 * and it is paid by people who did not write it.
 *
 * Collapsing the surface to one tool with an `action` parameter moves that cost
 * to where it belongs: the binary. A new capability is a new `action` string, and
 * the schema does not change, so no client update is needed. Only a genuinely new
 * *parameter* or a new *tool* forces registration, and those are rare.
 *
 * WHAT THIS CANNOT DO
 *
 * Nothing can add a new parameter to a registered schema without the client
 * changing. So `params` deliberately accepts a loose record: the model passes
 * `{ action: "find", params: { query: "x" } }` and the binary validates. That
 * keeps the schema stable across parameter additions too. The tradeoff is that
 * validation errors arrive at call time instead of being caught by the schema —
 * acceptable, because the binary's error names the fix.
 *
 * TEACHING NEW CAPABILITIES MID-SESSION
 *
 * The description cannot update itself. Two things compensate:
 *   1. `action: "actions"` returns what the installed build supports, so the
 *      model can discover a capability the description did not mention.
 *   2. Every response carries `meta.version`, so a stale description is visible.
 */

const SIGHTLINE_BIN = process.env.SIGHTLINE_BIN ?? "sightline";
const SIGHTLINE_PYTHONPATH = process.env.SIGHTLINE_PYTHONPATH ?? "";

export const ACTIONS = [
  "map",
  "list",
  "file",
  "find",
  "callers",
  "actions",
] as const;

export type SightlineAction = (typeof ACTIONS)[number];

const sightlineInputSchema = z.object({
  action: z
    .string()
    .describe(
      "What to do. One of: map | list | file | find | callers | actions. " +
        "Call action=actions to list what this build supports.",
    ),
  params: z
    .record(z.string(), z.unknown())
    .optional()
    .describe(
      "Action-specific parameters, e.g. { path: 'src/app.ts' } for file, " +
        "{ query: 'Foo' } for find, { budget: 2000 } for map.",
    ),
});

export type SightlineInput = z.infer<typeof sightlineInputSchema>;

export type SightlineResult = ToolResult<{
  action?: string;
  version?: string;
  truncated?: boolean;
}>;

function shellQuote(s: string): string {
  return `'${s.replaceAll("'", `'\\''`)}'`;
}

/** Convert `{ path, budget }` into CLI argv for a given action. */
export function argvFor(action: string, params: Record<string, unknown>): string[] {
  const p = params ?? {};
  const num = (v: unknown, d: number) =>
    typeof v === "number" && Number.isFinite(v) ? String(v) : String(d);
  const str = (v: unknown) => (typeof v === "string" ? v : undefined);

  switch (action) {
    case "map": {
      const args = ["map", "--budget", num(p.budget, 2000)];
      const focus = str(p.focus);
      if (focus) args.push("--focus", focus);
      return args;
    }
    case "list": {
      const args = [
        "list",
        str(p.path) ?? ".",
        "--budget",
        num(p.budget, 4000),
        "--direct",
      ];
      if (p.symbols === false) args.push("--no-symbols");
      return args;
    }
    case "file":
      return ["file", str(p.path) ?? ""];
    case "find":
      return ["find", str(p.query) ?? "", "--limit", num(p.limit, 30)];
    case "callers":
      return ["callers", str(p.symbol) ?? "", "--limit", num(p.limit, 20)];
    case "actions":
      return ["actions"];
    default:
      return [action];
  }
}

export const sightlineTool = () =>
  tool({
    description: `Structural view of the workspace: maps, inventories, file outlines, symbol search, and caller graphs.

WHEN TO USE:
- First in an unfamiliar repo, before grep or read (action=map)
- Choosing which file to read when you do not know the name
- Replacing grep for definitions (find), read for API (file), dir_list (list)

WHEN NOT TO USE:
- You are about to EDIT a file (use read; the body is where the bug is)
- Searching a string literal or usage count (use grep)

action=map|list|file|find|callers. Unknown action? Call action=actions.`,
    inputSchema: sightlineInputSchema,
    execute: async (
      { action, params = {} }: SightlineInput,
      { experimental_context }: { experimental_context?: unknown },
    ): Promise<SightlineResult> => {
      let sandbox;
      try {
        sandbox = await getSandbox(experimental_context, "sightline");
      } catch (error) {
        return fail(
          `Sandbox unavailable: ${error instanceof Error ? error.message : String(error)}.`,
          "use dir_list and grep",
        );
      }

      const cwd = sandbox.workingDirectory;

      // Validate any caller-supplied path through the host's security boundary,
      // exactly like read and dir_list do. A tool that resolves its own paths is
      // a path-traversal bug with a nice name.
      if (typeof params.path === "string") {
        const abs = resolveWorkspacePath(params.path, cwd);
        if (!abs) {
          return fail(
            `'${params.path}' is outside the workspace directory.`,
            "use a workspace-relative path",
          );
        }
        params = { ...params, path: toDisplayPath(abs, cwd) || params.path };
      }

      const args = argvFor(action, params as Record<string, unknown>);
      const env = SIGHTLINE_PYTHONPATH
        ? `PYTHONPATH=${shellQuote(SIGHTLINE_PYTHONPATH)} `
        : "";
      const budget =
        typeof params.budget === "number" ? params.budget : 4000;

      const run = async (argv: string[]) => {
        const cmd = `${env}${SIGHTLINE_BIN} --root ${shellQuote(cwd)} ${argv
          .map(shellQuote)
          .join(" ")}`;
        const r = await sandbox.exec(cmd, { cwd, timeoutMs: 180_000 });
        return `${r.stdout ?? ""}${r.stderr ? `\n${r.stderr}` : ""}`.trim();
      };

      let out: string;
      try {
        out = await run(args);
      } catch (e) {
        const why = explainExecFailure(
          127,
          e instanceof Error ? e.message : String(e),
          SIGHTLINE_BIN,
        );
        return fail(`[${why.kind}] ${why.message}`, why.remedy);
      }

      // First use on a fresh workspace: build the index, then retry once.
      if (/no index|run .*index/i.test(out)) {
        try {
          await run(["index"]);
          out = await run(args);
        } catch {
          return fail(
            "sightline could not build an index for this workspace.",
            "use dir_list and grep",
          );
        }
      }

      // Unknown action: this is the self-teaching path. Return the live list
      // rather than a bare error, so the model can pick a real one.
      if (/invalid choice|unrecognized arguments|usage: sightline/i.test(out)) {
        const live = await run(["actions"]).catch(() => "");
        return fail(
          `Unknown action '${action}'.`,
          live || "call action=actions to list supported actions",
        );
      }

      if (!out) {
        return fail(`sightline returned nothing for action '${action}'.`);
      }

      return ok(
        action === "metrics" || action === "actions"
          ? out
          : clampToBudget(out, budget, "raise budget or narrow with params.focus"),
        { action, truncated: out.includes("omitted by budget") },
      );
    },
  });
