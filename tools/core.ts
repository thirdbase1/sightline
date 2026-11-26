/**
 * core.ts — shared primitives for every tool in the suite.
 *
 * WHY THIS EXISTS
 *
 * Entry has 37 tools and only 3 share an output helper. Descriptions range from
 * 304 to 1,892 characters, and 31 tool definitions cost **2,967 tokens on every
 * request** before the agent does anything. At 37 tools with richer schemas that
 * becomes ~6,300.
 *
 * That is the real problem with a tool suite: not that individual tools are bad,
 * but that 37 hand-written definitions drift, duplicate, and tax every single
 * request forever. A suite without a shared core is 37 one-offs that happen to
 * live in one repo.
 *
 * This file defines the four things every tool must share, plus a registry that
 * measures the total cost.
 */

import { z } from "zod";

// ---------------------------------------------------------------------------
// 1. Descriptions: a budget, not a mood
// ---------------------------------------------------------------------------

/**
 * Hard cap on a tool description. Measured: 170 tokens per tool at 400 chars,
 * and a 6-tool suite at 500 chars each is ~800 tokens — acceptable. At 1,892
 * chars (bash.ts today) a single tool costs 500 tokens per request forever.
 *
 * 400 is enough for WHEN TO USE + WHEN NOT TO USE + one usage line. Anything
 * longer is documentation, and documentation belongs in a skill or a docstring,
 * not in the request payload.
 */
export const MAX_DESCRIPTION_CHARS = 400;

/**
 * The description format every tool uses. Machine-checkable: `validateTool`
 * asserts the sections exist, because a tool without "WHEN NOT TO USE" is the
 * tool that gets called for the wrong thing.
 */
export interface ToolDoc {
  /** What it returns, in one clause. */
  summary: string;
  /** 2-4 bullets. The positive cases. */
  use: string[];
  /** 1-3 bullets naming the ALTERNATIVE TOOL. This is the important part. */
  notUse: string[];
  /** Optional: one line on cost or a gotcha. */
  note?: string;
}

export function describe(doc: ToolDoc): string {
  const parts = [
    doc.summary.trim(),
    "WHEN TO USE:",
    ...doc.use.map((u) => `- ${u}`),
    "WHEN NOT TO USE:",
    ...doc.notUse.map((n) => `- ${n}`),
  ];
  if (doc.note) parts.push(doc.note);

  const text = parts.join("\n");
  if (text.length > MAX_DESCRIPTION_CHARS) {
    throw new Error(
      `Tool description is ${text.length} chars, over the ${MAX_DESCRIPTION_CHARS} ` +
        `budget. Every char here is paid on every request. Tighten the bullets ` +
        `or move detail into the tool's docstring.`,
    );
  }
  return text;
}

// ---------------------------------------------------------------------------
// 2. Results: text out, structured in
// ---------------------------------------------------------------------------

/**
 * Every tool returns this. No exceptions, no bare strings, no throws.
 *
 * Two rules encoded here:
 *
 *  - `text` is what the model reads. It is the ONLY thing sent. A tool that also
 *    returns a `formatted` mirror of the same data makes the model pay twice —
 *    dir_list sends both and costs 159 chars/entry instead of 26.
 *
 *  - `meta` never reaches the model. It is for the UI, for telemetry, and for the
 *    next tool call (a handle to pass forward). Keeping it separate is what lets
 *    a tool return rich structure without paying tokens for it.
 */
export interface ToolResult<M extends Record<string, unknown> = Record<string, never>> {
  success: boolean;
  /** Model-visible. Keep it under `maxTokens`. */
  text: string;
  /** Never sent to the model. */
  meta?: M;
  /** Set when the tool could not do the job. `text` should still explain. */
  error?: string;
}

export function ok<T extends Record<string, unknown>>(
  text: string,
  meta?: T,
): ToolResult<T> {
  return { success: true, text: text.trimEnd(), meta };
}

/**
 * A failure the model can act on: what failed, and what to do instead.
 * Never a stack trace, never a bare "error".
 */
export function fail(message: string, alternative?: string): ToolResult {
  const text = alternative ? `${message}\nTry: ${alternative}` : message;
  return { success: false, text, error: message };
}

// ---------------------------------------------------------------------------
// 3. Budgets: a tool that overshoots its budget cannot be trusted
// ---------------------------------------------------------------------------

export const CHARS_PER_TOKEN = 3.8;

export function estTokens(text: string): number {
  return Math.ceil(text.length / CHARS_PER_TOKEN);
}

/**
 * Truncate to a token budget on a line boundary, and SAY SO.
 *
 * Every truncating tool must append the marker. Silent truncation is the bug that
 * made a `sightline_map` at 2,000 tokens look like a complete inventory of a 53k-file
 * repo: the model cannot tell "this is all of it" from "this is the first 5%".
 */
export function clampToBudget(
  text: string,
  maxTokens: number,
  hint = "narrow the path or raise the budget",
): string {
  if (estTokens(text) <= maxTokens) return text;

  const maxChars = Math.floor(maxTokens * CHARS_PER_TOKEN) - 120;
  const cut = text.slice(0, maxChars);
  const lastNewline = cut.lastIndexOf("\n");
  const body = lastNewline > maxChars * 0.5 ? cut.slice(0, lastNewline) : cut;

  return `${body}\n# truncated at ~${maxTokens} tokens. To see more: ${hint}`;
}

// ---------------------------------------------------------------------------
// 4. Sandbox access: the host's rules, not ours
// ---------------------------------------------------------------------------

/**
 * Tools never touch the filesystem directly. They go through the sandbox, behind
 * the host's path security. A tool that resolves paths itself is a path-traversal
 * bug with a nice name.
 *
 * This wrapper exists so the pattern is one line at every call site, instead of
 * each tool remembering to check. `resolveWorkspacePath` returning null must be a
 * model-readable refusal, not a throw.
 */
export interface SandboxLike {
  workingDirectory: string;
  exec(
    cmd: string,
    opts?: { cwd?: string; timeoutMs?: number },
  ): Promise<{ stdout?: string; stderr?: string; exitCode?: number }>;
}

export type PathResolver = (
  requested: string,
  workingDirectory: string,
) => string | null;

export function shellQuote(s: string): string {
  return `'${s.replaceAll("'", `'\\''`)}'`;
}

/**
 * Run a command inside the workspace, with the host's path rules applied first.
 *
 * Returns a ToolResult so callers do not need their own error branch. The failure
 * text names the fallback tool, because "sightline is unavailable" alone leaves the
 * model stuck.
 */
/**
 * Turn an exec failure into something the model can act on.
 *
 * Measured failure: a whole session ran with every sightline call returning
 * `sightline: command not found` (exit 127). The tools were registered and
 * "working" — the binary was not on PATH. An agent cannot diagnose that from a
 * raw shell string, so the failure kinds are named explicitly here.
 */
export function explainExecFailure(
  exitCode: number,
  output: string,
  binName: string,
): { kind: string; message: string; remedy: string } {
  const out = output.trim();

  if (exitCode === 127) {
    return {
      kind: "not_installed",
      message: `${binName} is not installed in this sandbox (exit 127).`,
      remedy:
        `Install it once with: pip install -e <path-to-sightline>\n` +
        `Or set CODEMAP_PYTHONPATH to a checkout and use ` +
        `python3 -m sightline. Until then, use the built-in tools.`,
    };
  }

  if (/no such file or directory/i.test(out) && /python3|node/i.test(out)) {
    return {
      kind: "runtime_missing",
      message: `The interpreter for ${binName} is missing (${out}).`,
      remedy: "Install Python 3.10+ in the sandbox image, then retry.",
    };
  }

  if (/no index|run .* index/i.test(out)) {
    return {
      kind: "no_index",
      message: "No index exists for this workspace yet.",
      remedy: `Run: ${binName} index\n(the first call builds it; later calls are fast)`,
    };
  }

  if (exitCode === 124 || /timed out/i.test(out)) {
    return {
      kind: "timeout",
      message: `${binName} timed out.`,
      remedy:
        "Narrow the request: pass a smaller path, a lower budget, or a focus area.",
    };
  }

  return {
    kind: "failed",
    message: out || `${binName} exited ${exitCode} with no output.`,
    remedy: "Fall back to the built-in equivalent tool.",
  };
}

export async function runInWorkspace(
  sandbox: SandboxLike,
  resolve: PathResolver,
  requestedPath: string | undefined,
  command: (safeRelativePath: string) => string,
  opts: { fallback?: string; timeoutMs?: number; binName?: string } = {},
): Promise<ToolResult> {
  const requested = requestedPath ?? ".";
  const abs = resolve(requested, sandbox.workingDirectory);
  if (!abs) {
    return fail(
      `'${requested}' is outside the workspace directory.`,
      "use a workspace-relative path",
    );
  }

  const rel =
    abs === sandbox.workingDirectory
      ? "."
      : abs.slice(sandbox.workingDirectory.length + 1);

  try {
    const r = await sandbox.exec(command(rel), {
      cwd: sandbox.workingDirectory,
      timeoutMs: opts.timeoutMs ?? 120_000,
    });
    const out = `${r.stdout ?? ""}${r.stderr ? `\n${r.stderr}` : ""}`.trim();

    if ((r.exitCode ?? 0) !== 0 && !r.stdout) {
      const why = explainExecFailure(
        r.exitCode ?? 1,
        out,
        opts.binName ?? "sightline",
      );
      return fail(`[${why.kind}] ${why.message}`, why.remedy);
    }
    return ok(out);
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (/ENOENT|not found/i.test(msg)) {
      const why = explainExecFailure(127, msg, opts.binName ?? "sightline");
      return fail(`[${why.kind}] ${why.message}`, why.remedy);
    }
    return fail(
      `command failed: ${msg}`,
      opts.fallback ?? "fall back to the built-in equivalent",
    );
  }
}

// ---------------------------------------------------------------------------
// 5. The registry: measure the suite, do not assume it
// ---------------------------------------------------------------------------

export interface RegisteredTool {
  name: string;
  description: string;
  inputSchema: z.ZodTypeAny;
  execute: (input: never, ctx: unknown) => Promise<ToolResult>;
  /** Which built-in tool this replaces, for migration and comparison. */
  replaces?: string[];
}

/**
 * Holds the suite and can report its total prompt cost.
 *
 * The number this returns is the one that matters: every token here is spent on
 * every request, whether or not the tool is called. A suite should be able to
 * state it.
 */
export class ToolSuite {
  private tools = new Map<string, RegisteredTool>();

  add(tool: RegisteredTool): this {
    if (this.tools.has(tool.name)) {
      throw new Error(`duplicate tool name: ${tool.name}`);
    }
    const len = tool.description.length;
    if (len > MAX_DESCRIPTION_CHARS) {
      throw new Error(
        `${tool.name}: description ${len} chars exceeds ${MAX_DESCRIPTION_CHARS}`,
      );
    }
    this.tools.set(tool.name, tool);
    return this;
  }

  get(name: string): RegisteredTool | undefined {
    return this.tools.get(name);
  }

  list(): RegisteredTool[] {
    return [...this.tools.values()];
  }

  /** Approximate tokens this suite costs per request, before any call. */
  promptCost(): { tokens: number; byTool: Array<{ name: string; tokens: number }> } {
    const byTool = this.list().map((t) => {
      // name + description + a JSON-schema echo of the input shape. The schema is
      // usually the larger half; a zod object of 3 optional strings is ~200 chars.
      const approx = t.name.length + t.description.length + 220;
      return { name: t.name, tokens: estTokens("x".repeat(approx)) };
    });
    return {
      tokens: byTool.reduce((a, b) => a + b.tokens, 0),
      byTool: byTool.sort((a, b) => b.tokens - a.tokens),
    };
  }

  /** Tools whose replacement set overlaps, i.e. candidates for merging. */
  overlaps(): Array<[string, string, string[]]> {
    const out: Array<[string, string, string[]]> = [];
    const all = this.list();
    for (let i = 0; i < all.length; i++) {
      for (let j = i + 1; j < all.length; j++) {
        const shared = (all[i].replaces ?? []).filter((r) =>
          (all[j].replaces ?? []).includes(r),
        );
        if (shared.length) out.push([all[i].name, all[j].name, shared]);
      }
    }
    return out;
  }
}
