"""Render a token-budgeted map.

The output is the product. A map that is too big defeats the purpose; one that is
too small omits the thing the agent needed. So it renders in tiers, and the
caller picks a budget.

Budget calibration, from measurement on real repos (see references/research.md):

    2k tokens   ~ 60 top symbols       orientation: "what kind of repo is this"
    8k tokens   ~ 250 symbols          feature work: enough to plan an edit
    25k tokens  ~ 800 symbols          refactor: whole-module understanding
    60k tokens  ~ full compact map     "give me everything, I have budget"

A 1 GB repo of 1,500 source files produces roughly 12k symbols. The full map at
~14 tokens/symbol is ~170k tokens, which does not fit anywhere. That is why
ranking and truncation are the design, not an optimization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .index import Index, BINARY_EXTS

# Cost model. Source code averages ~3.8 chars/token under BPE; measured on this
# tool's own output at 32,274 chars = ~8,493 tokens (3.80 chars/token), so the
# constant is calibrated rather than assumed. Budget enforcement works on
# CHARACTERS and converts once, because summing per-line int() estimates
# accumulates truncation error and overshoots.
CHARS_PER_TOKEN = 3.8
TOKENS_PER_SYMBOL = 14
TOKENS_PER_FILE_HEADER = 12


@dataclass
class RenderOptions:
    budget: int = 8000
    focus: list[str] | None = None  # paths the agent is working in
    max_files: int = 400
    show_signatures: bool = True
    group_by_dir: bool = True
    min_rank_share: float = 0.0
    # A single file must not monopolise the budget. schema.ts with 40 table
    # exports is important, but 40 lines of it is enough to know it is the
    # schema; the agent can ask for `sightline file` if it needs all of them.
    max_symbols_per_file: int = 18
    # Force the per-file list even when the repo is too large for it to be
    # representative. Directory rollup is the default for big repos.
    force_files: bool = False


def estimate_tokens(text: str) -> int:
    """Cheap, honest estimate: 1 token ~ 3.8 chars for source code."""
    return int(len(text) / CHARS_PER_TOKEN)


def _personalize(idx: Index, focus: list[str] | None) -> dict[str, float]:
    """Bias PageRank toward the agent's working set."""
    if not focus:
        return idx.ranks

    seeds: dict[str, float] = {}
    for f in focus:
        norm = f.lstrip("./")
        if norm in idx.files:
            seeds[norm] = 1.0
            continue
        # Allow a directory or a partial path.
        for p in idx.files:
            if p.startswith(norm):
                seeds[p] = 1.0

    if not seeds:
        return idx.ranks

    from . import graph as G

    class _Info:
        def __init__(self, p):
            self.path = p
            self.imports = idx.files[p]["imports"]

    infos = {p: _Info(p) for p in idx.files}
    g = G.resolve_imports(infos, roots=[idx.root])
    # 0.85 floor: an explicit focus scopes the map. The default 0.35 exists so a
    # task-focused map still shows unrelated modules; when the caller names a
    # directory, that intent should dominate.
    local = G.pagerank(g, personalization=seeds, focus_floor=0.85)

    # Blend: 70% personalized reach, 30% global importance. Pure personalization
    # loses the "this file is load-bearing" signal entirely.
    def in_scope(p: str) -> bool:
        return any(p == s or p.startswith(s) for s in seeds)

    out: dict[str, float] = {}
    for p in idx.files:
        base = 0.7 * local.get(p, 0.0) + 0.3 * idx.ranks.get(p, 0.0)
        # Out-of-scope files stay visible at a heavy discount rather than
        # vanishing, so a focused map can still point outward.
        out[p] = base if in_scope(p) else base * 0.15

    # Co-change: files that historically change together. This is the signal
    # imports cannot see - `chat.ts` and `session-chat-content.tsx` changed
    # together 15 times with no import edge between them. Added, not multiplied,
    # so a file with no structural link still surfaces.
    if idx.cochange:
        for seed in seeds:
            for related, delta in G.cochange_delta(seed, idx.cochange, scale=0.6).items():
                if related in out:
                    out[related] += delta
    return out


def _rollup(idx: Index, ranks: dict[str, float], budget_files: int) -> list[str]:
    """Directory aggregates for the whole repo.

    Measured: a 29k-file repo has only ~20 top-level directories, so a pure
    rollup costs ~3k tokens and leaves 22k of a 25k budget unused. That is a
    wasted budget, not a safety margin. The caller expands the top directories
    with `_expand_dir` until the budget is spent.
    """
    """Collapse directories when the repo is too big for per-file detail.

    Measured on 118 repos: a 2,000-token map is a CONSTANT ~20 files, so coverage
    falls from 55% (<100 files) to 0.6% (1,500+ files). Showing 20 arbitrary files
    from a 7,000-file repo is worse than useless - it implies the rest does not
    exist.

    Below the threshold the map shows a directory's aggregate: file count, LOC,
    and its most important symbols. That answers "what is in here" without
    pretending to answer "what is in file 4,812".
    """
    from collections import defaultdict

    # Choose a grouping depth that yields a readable number of groups.
    #
    # Two failure modes were measured and both are bad:
    #   depth = full parent  -> webpack produced 20,079 "directories", so the
    #                           rollup could never be expanded and a 25k budget
    #                           returned 7k
    #   depth = top level    -> webpack collapsed to one group (`test/` is 98%
    #                           of its files), so the map said "test/: 28,743
    #                           files" and nothing else
    #
    # Pick the shallowest depth that still yields >= 8 groups, capped at 3.
    def groups_at(depth: int) -> dict[str, list[str]]:
        g: dict[str, list[str]] = defaultdict(list)
        for path in idx.files:
            parts = path.split("/")
            if len(parts) <= 1:
                g["."].append(path)          # file at repo root
            else:
                n = min(depth, len(parts) - 1)   # never include the filename
                g["/".join(parts[:n])].append(path)
        return g

    # Pick depth by BALANCE, not group count. Depth 1 on webpack yields 9 groups
    # but `test/` holds 98% of the files, so the map degenerates to one line.
    # A usable grouping has several groups of comparable size.
    by_dir: dict[str, list[str]] = {}
    best_score = -1.0
    for depth in (1, 2):
        cand = groups_at(depth)
        sizes = sorted((len(v) for v in cand.values()), reverse=True)
        if len(sizes) < 2:
            continue
        total = sum(sizes)
        top_share = sizes[0] / total
        # Reward many groups, punish one dominant bucket.
        score = len(sizes) * (1.0 - top_share)
        if score > best_score:
            best_score, by_dir = score, cand
    if not by_dir:
        by_dir = groups_at(1)

    lines: list[str] = []
    for d in sorted(by_dir, key=lambda k: -sum(ranks.get(p, 0) for p in by_dir[k])):
        members = by_dir[d]
        loc = sum(idx.files[p]["loc"] for p in members)
        syms = sum(len(idx.files[p]["symbols"]) for p in members)
        top = sorted(members, key=lambda p: -ranks.get(p, 0))[:3]
        names = ", ".join(p.rsplit("/", 1)[-1] for p in top)
        lines.append(f"{d}/  [{len(members)} files, {loc:,} LOC, {syms} symbols]  top: {names}\n")
    return lines


# Keyed on the file map's identity via id() BUT with an explicit size bound and
# the map object held strongly, so an id cannot be recycled while cached.
#
# The first version keyed on `id(idx)` and never evicted: 23k entries leaked per
# run, and because id() is a memory address, a freed Index could have its id
# reused by a new object - serving one repo's directory index to another.
# WeakKeyDictionary is not an option: Index is a dataclass with eq=True and
# therefore unhashable.
_DIR_INDEX_CACHE: dict[int, tuple[dict, dict[str, list[str]]]] = {}
_DIR_INDEX_MAX = 4


def _dir_index(idx: Index) -> dict[str, list[str]]:
    """Map every directory prefix to its files, built once per index.

    Memoised ON THE INDEX, not in a module global. Three earlier attempts here
    and what each did wrong:

      1. `dict[id(idx)]` with no eviction -> 23k entries leaked per run, and
         because `id()` is a memory address, a freed Index's id can be reused by
         a new object, serving one repo's directory index to another.
      2. `WeakKeyDictionary` -> a weak KEY is not enough when the VALUE references
         the key's own data; the entry keeps itself alive. 390k lists accumulated.
      3. A bounded global holding strong refs to the last 4 indexes -> correct,
         but it deliberately retains up to 4 full indexes, which reads as a
         memory leak in any long-lived process.

    An attribute on the object ties the lifetime to the object with no global
    state and no retention.
    """
    cached = getattr(idx, "dir_index", None)
    if cached is not None:
        return cached
    out: dict[str, list[str]] = {}
    for path in idx.files:
        parts = path.split("/")
        acc = ""
        for part in parts[:-1]:
            acc = f"{acc}/{part}" if acc else part
            out.setdefault(acc, []).append(path)
    idx.dir_index = out
    return out


def _expand_dir(
    idx: Index, d: str, ranks: dict[str, float], limit: int = 25
) -> list[str]:
    """List the highest-ranked files inside one directory."""
    # Match by PREFIX, not by exact parent. A depth-2 group key like
    # `test/fixtures` has no files whose immediate parent is that string, so an
    # equality test returned zero members and the expansion silently did nothing -
    # which is why a 25k budget produced 7k of output.
    if d in (".", ""):
        members = [p for p in idx.files if "/" not in p]
    else:
        # Use the shared prefix index. Scanning idx.files here made map rendering
        # O(groups x files): 3.5s for a 25k-token map on a 53k-file repo.
        members = list(_dir_index(idx).get(d, ()))
    members.sort(key=lambda p: -ranks.get(p, 0))
    out: list[str] = []
    for p in members[:limit]:
        meta = idx.files[p]
        out.append(f"    {p.rsplit('/', 1)[-1]} [{meta['loc']}]\n")
        for sym in meta["symbols"][:4]:
            sig = (sym.get("signature") or sym["name"]).strip()
            if len(sig) > 90:
                sig = sig[:87] + "..."
            out.append(f"      {sig}\n")
    if len(members) > limit:
        out.append(f"    ... +{len(members) - limit} files\n")
    return out


def render_map(idx: Index, opts: RenderOptions | None = None) -> str:
    """Produce the compact map, truncated to the budget."""
    opts = opts or RenderOptions()
    ranks = _personalize(idx, opts.focus)

    def score(path: str) -> float:
        base = ranks.get(path, 0.0)
        low = path.lower()
        # Fixture and test-data trees are numerous and densely interlinked, so
        # raw PageRank ranks them above real modules. In eslint that put 63
        # test fixtures at the top of the map and pushed lib/linter/vfile.js off
        # it entirely. Demote, do not exclude: a test file is still relevant.
        # Order matters: "tests/fixtures/..." must hit fixtures before tests.
        for marker, factor in (
            ("fixtures/", 0.2), ("testdata/", 0.2), ("__fixtures__/", 0.2),
            ("test-ecosystem/", 0.3), ("/tests/", 0.45), ("/test/", 0.45),
            ("/docs/", 0.5), ("/examples/", 0.5), ("/benchmark", 0.4),
            (".test.", 0.65), (".spec.", 0.65),
        ):
            if marker in low:
                base *= factor
                break
        # A file with no rank is not unimportant; it is merely unreachable via
        # imports (entry point, plugin, dynamically loaded). Give it the median
        # so it can still surface instead of being pinned to the floor.
        if path not in ranks:
            base = max(base, median_rank * 0.5)
        return base

    rank_values = sorted(ranks.values())
    median_rank = (
        rank_values[len(rank_values) // 2] if rank_values else 0.0
    )

    ordered = sorted(idx.files.items(), key=lambda kv: score(kv[0]), reverse=True)
    ordered = ordered[: opts.max_files]

    # Coverage guard: if per-file detail would show under ~8% of the repo, a file
    # list misleads. Roll up to directories instead, which at least maps the
    # territory. `--files` forces the old behaviour.
    # Calibrated on the 118-repo corpus. Per-file detail is the right default
    # until coverage drops below ~3%, because a file list is what answers
    # questions; directories only say where things are. At 1,213 files a
    # 2,000-token map still covers ~2% but answers 100% of the benchmark;
    # at 7,922 it covers 0.3% and stops naming anything reachable. The threshold
    # is therefore on the ratio, not on file count, and deliberately conservative:
    # a wrong rollup costs correctness, which is worse than a long map.
    approx_shown = max(1, opts.budget // 40)
    coverage = approx_shown / max(1, len(idx.files))
    if not opts.force_files and len(idx.files) > 400 and coverage < 0.03:
        # Tell the caller what budget would actually give them file names.
        # Measured across 92 large repos: per-file detail needs roughly
        # 40 tokens per file, and the crossover is ~25k for an 8k-file repo.
        needed = int(len(idx.files) * 40)
        if needed <= 250_000:
            hint = f"need --budget {max(1, needed // 1000)}k for per-file detail"
        else:
            hint = ("too large for per-file detail at any practical budget; "
                    "use --focus <dir>")
        hint = "# " + hint
        header = (f"# {len(idx.files)} files, {idx.stats.get('loc', 0):,} LOC, "
                  f"{idx.stats.get('edges', 0):,} edges - too large for per-file detail "
                  f"at {opts.budget} tokens, showing directories\n"
                  f"{hint}\n")
        body = _rollup(idx, ranks, opts.budget)
        out = header + "\n" + "".join(body)

        # Spend the leftover budget expanding the top directories rather than
        # returning a 3k-token answer to a 25k-token request.
        # Two-pass allocation: round 1 gives every group a small expansion,
        # round 2 spends the remainder on the groups with the most room. A single
        # pass that `continue`s on an oversized block wastes the whole budget,
        # which is what capped a 25k-token map at 11k.
        limit = (opts.budget - 40) * CHARS_PER_TOKEN
        if len(out) < limit * 0.85:
            used = len(header)
            budget_left = limit - used
            lines_by_group: list[tuple[str, list[str]]] = [
                (ln.split("/  [", 1)[0], [ln]) for ln in body
            ]

            # Round 1: allocate per-group depth from the budget. 6 was arbitrary
            # and with 115 groups it capped the whole map at ~8k of a 25k budget.
            per_group = max(4, min(60, int(budget_left / max(1, len(lines_by_group)) / 90)))
            rentries: list[tuple[str, list[str]]] = []
            for d, head in lines_by_group:
                block = _expand_dir(idx, d, ranks, limit=per_group)
                cost = sum(len(b) for b in block)
                if cost <= max(0, budget_left):
                    rentries.append((d, head + block))
                    budget_left -= cost
                else:
                    rentries.append((d, head))

            # Round 2: top up groups, most important first, while budget lasts.
            for i, (d, lines) in enumerate(rentries):
                if budget_left < 400:
                    break
                block = _expand_dir(idx, d, ranks, limit=40)
                head = lines[0]
                already = max(0, len(lines) - 1)
                extra = block[already * 7:]  # best-effort skip of printed lines
                cost = sum(len(b) for b in extra)
                if cost <= budget_left and cost > 0:
                    rentries[i] = (d, lines + extra)
                    budget_left -= cost

            out = header + "\n" + "".join(ln for _, ls in rentries for ln in ls)
        # Trim to budget
        if len(out) / CHARS_PER_TOKEN > opts.budget:
            kept: list[str] = [header, "\n"]
            used = len(header)
            limit = (opts.budget - 60) * CHARS_PER_TOKEN
            for line in body:
                if used + len(line) > limit:
                    break
                kept.append(line)
                used += len(line)
            out = "".join(kept) + f"\n# truncated at {opts.budget} tokens\n"
        return out

    lines: list[str] = []
    # Work in characters; a map that reports its own size must fit inside the
    # budget it was given, footer included.
    char_budget = max(1200, (opts.budget - 90) * CHARS_PER_TOKEN)
    used_chars = 0
    used = 0
    rendered_files = 0
    omitted_symbols = 0

    header = (
        f"# {idx.stats.get('files', 0)} files, {idx.stats.get('loc', 0):,} LOC, "
        f"{idx.stats.get('edges', 0):,} import edges\n"
    )
    lines.append(header)
    used_chars += len(header)

    current_dir = None

    for path, meta in ordered:
        if used_chars >= char_budget:
            break

        conf = meta["confidence"]
        syms = meta["symbols"]

        # A file we could not parse gets one line saying so. The agent must know
        # to read it rather than trust an empty symbol list.
        if conf == "none":
            line = f"{path}  [{meta['loc']} LOC, unparsed: {meta.get('note') or 'read it'}]\n"
            if used_chars + len(line) > char_budget:
                break
            lines.append(line)
            used_chars += len(line)
            rendered_files += 1
            continue

        directory = path.rsplit("/", 1)[0] if "/" in path else "."
        if opts.group_by_dir and directory != current_dir:
            current_dir = directory
            dline = f"\n{directory}/\n"
            if used_chars + len(dline) > char_budget:
                break
            lines.append(dline)
            used_chars += len(dline)

        name = path.rsplit("/", 1)[-1]
        head = f"  {name} [{meta['loc']}]\n"
        if used_chars + len(head) > char_budget:
            break

        # Cap per file, then report what was capped so the agent knows to ask
        # for more rather than assuming the list is complete.
        shown = syms[: opts.max_symbols_per_file]
        capped = max(0, len(syms) - len(shown))

        body_lines: list[str] = []
        for s in shown:
            sig = (s.get("signature") or s["name"]) if opts.show_signatures else s["name"]
            sig = re.sub(r"\s+", " ", sig).strip()
            if len(sig) > 110:
                sig = sig[:107] + "..."
            body_lines.append(f"    {sig}\n")

        accepted: list[str] = []
        block_chars = len(head)
        for bl in body_lines:
            if used_chars + block_chars + len(bl) > char_budget:
                capped += len(body_lines) - len(accepted)
                break
            accepted.append(bl)
            block_chars += len(bl)

        omitted_symbols += capped
        if capped:
            tail = f"    ... +{capped} more (sightline file {path})\n"
            if used_chars + block_chars + len(tail) <= char_budget:
                accepted.append(tail)
                block_chars += len(tail)

        lines.append(head)
        lines.extend(accepted)
        used_chars += block_chars
        rendered_files += 1

    used = estimate_tokens("".join(lines))

    footer = (
        f"\n# rendered {rendered_files}/{len(idx.files)} files, "
        f"~{used:,} tokens. "
        f"{omitted_symbols} symbols omitted by budget."
    )
    if omitted_symbols:
        footer += " Ask for a deeper slice of one directory, or narrow with `focus`."
    lines.append(footer)

    return "".join(lines)



def render_listing(
    idx: Index,
    path: str = ".",
    budget: int = 4000,
) -> str:
    """Complete, compact file inventory. Beats a directory listing on cost.

    A conventional `dir_list` payload measures **159 chars per entry**: it sends
    the same finding twice (a JSON array and a formatted mirror), repeats the
    absolute path prefix on every line, and carries no indication of what is
    inside any file.

    This emits one line per entry, relative paths, grouped by immediate parent,
    with size and symbol count. Measured at ~26 chars/entry: **6x denser**, and it
    says how much code each file contains, which a listing cannot.

        lib/
          api.js                          1K    1 sym
          linter/
            linter.js                    49K   24 syms
    """
    import collections

    def human(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1e6:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.0f}K"
        return str(n) if n else ""

    want = path.strip("/")
    prefix = "" if want in (".", "") else want + "/"

    groups: dict[str, list[str]] = collections.defaultdict(list)
    for fp, meta in idx.files.items():
        if prefix and not fp.startswith(prefix):
            continue
        rest = fp[len(prefix):]
        parent = rest.rsplit("/", 1)[0] if "/" in rest else ""
        name = rest.rsplit("/", 1)[-1]
        size = human(meta.get("size", 0))
        n = len(meta["symbols"])
        sym = f"{n} sym" if n else ""
        groups[parent].append(f"    {name:<36}{size:>6}{sym:>9}\n")

    lines: list[str] = [f"{prefix}\n"] if prefix else []
    used = len(lines[0]) if lines else 0
    limit = (budget - 40) * CHARS_PER_TOKEN
    shown = 0
    total = sum(len(v) for v in groups.values())

    for parent in sorted(groups):
        head = f"  {parent}/\n" if parent else ""
        block = [head] + sorted(groups[parent])
        cost = sum(len(b) for b in block)
        if used + cost > limit:
            break
        lines.extend(block)
        used += cost
        shown += len(groups[parent])

    lines.append(f"# {shown}/{total} entries, ~{int(used / CHARS_PER_TOKEN):,} tokens\n")
    if shown < total:
        lines.append(f"# narrow: sightline list {prefix}some-subdir  or raise --budget\n")
    return "".join(lines)



def render_listing_direct(
    root: str | Path,
    path: str = ".",
    budget: int = 4000,
    count_symbols: bool = True,
) -> str:
    """Inventory with NO index and NO extraction. Filesystem only.

    Latency was the one dimension where a directory listing beat sightline: 0.21s
    warm, 0.75s cold, versus instant. This path reads the tree directly, counts
    declarations with one cheap regex pass over the first 200 lines, and emits the
    same compact format. Measured at ~0.04s on a 2,300-file repo.
    """
    import os
    import re as _re
    from pathlib import Path as _P

    DECL = _re.compile(
        rb"^[ \t]*(?:export[ \t]+)?(?:async[ \t]+)?(?:function|class|interface|"
        rb"type|enum|def|func|fn|struct|trait|impl|const|let|var)[ \t]+",
        _re.MULTILINE,
    )

    def human(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1e6:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.0f}K"
        return str(n) if n else ""

    base = _P(root) / path.strip("/") if path.strip("/") not in ("", ".") else _P(root)
    if str(path).strip("/") not in ("", "."):
        header = str(path).strip("/") + "/\n"
    else:
        header = ""

    import collections
    groups: dict[str, list[str]] = collections.defaultdict(list)
    total = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames
                       if d not in {"node_modules", ".git", ".sightline", "dist",
                                    "build", "coverage", "target", "__pycache__"}]
        rel_dir = os.path.relpath(dirpath, base)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        for f in sorted(filenames):
            fp = os.path.join(dirpath, f)
            ext = os.path.splitext(f)[1].lower()
            if ext in BINARY_EXTS:
                continue
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            # Symbol counts are filled in later, for shown files only. Reading
            # every file head costs ~30ms on a 400-file directory and most of it
            # is discarded by the budget.
            groups[rel_dir].append((f, human(sz), fp, ext))
            total += 1

    SRC = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py",
           ".go", ".rs", ".java", ".rb", ".php", ".kt", ".swift"}

    def row(name: str, size: str, fp: str, ext: str) -> str:
        # Sequential, deliberately. A 16-thread pool measured WORSE (183ms vs
        # 92ms) on this 2-core box: thread overhead exceeds any IO overlap.
        nsym = 0
        if count_symbols and ext in SRC:
            try:
                with open(fp, "rb") as fh:
                    nsym = len(DECL.findall(fh.read(4096)))
            except OSError:
                pass
        return f"    {name:<36}{size:>6}{(str(nsym) + ' sym') if nsym else '':>9}\n"

    lines = [header] if header else []
    used = len(header)
    limit = (budget - 40) * CHARS_PER_TOKEN
    shown = 0
    for g in sorted(groups):
        head = f"  {g}/\n" if g else ""
        rows = [row(*e) for e in sorted(groups[g])]
        block = [head] + rows
        cost = sum(len(b) for b in block)
        if used + cost > limit:
            break
        lines.extend(block)
        used += cost
        shown += len(groups[g])

    lines.append(f"# {shown}/{total} entries, ~{int(used / CHARS_PER_TOKEN):,} tokens\n")
    if shown < total:
        lines.append("# narrow: sightline list <subdir>  or raise --budget\n")
    return "".join(lines)


def render_file(idx: Index, path: str) -> str | None:
    """Full symbol list for one file, ignoring the global budget."""
    meta = idx.files.get(path.lstrip("./"))
    if not meta:
        return None
    lines = [f"{path} [{meta['loc']} LOC, {meta['confidence']}]\n"]
    if meta.get("imports"):
        lines.append("  imports: " + ", ".join(meta["imports"][:12]) + "\n")
    for s in meta["symbols"]:
        kind = s["kind"]
        parent = f"{s['parent']}." if s.get("parent") else ""
        lines.append(f"  {kind:9s} {parent}{s['signature'] or s['name']}\n")
    return "".join(lines)
