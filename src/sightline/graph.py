"""Import graph, PageRank, and change-coupling.

Three signals combine to decide what an agent should look at first:

1. **Structural** — PageRank over the import graph. A file imported by many
   things is load-bearing; changing it has blast radius. This is what aider's
   repo map does and it is the single highest-value signal.
2. **Proximity** — distance from whatever the agent is currently looking at.
   Everything is relevant *to a task*, and a map with no task is just a directory
   listing.
3. **Co-change** — files that historically changed together. This is git history,
   and it captures coupling that no import graph sees (a route and its test, a
   migration and its model). Rare in these tools; cheap to compute; high value.

No embeddings. Deliberate: the published benchmarks on graph-MCP tools show the
wins come from *structure*, while semantic search over code has a worse
precision/recall trade and a much worse cold-start cost. See
`references/research.md` for the evidence and the numbers.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Graph:
    nodes: list[str] = field(default_factory=list)
    edges: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    reverse: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_node(self, n: str) -> None:
        if n not in self.edges:
            self.nodes.append(n)
            self.edges[n] = set()

    def add_edge(self, src: str, dst: str) -> None:
        if src == dst:
            return
        self.add_node(src)
        self.add_node(dst)
        self.edges[src].add(dst)
        self.reverse[dst].add(src)

    def out_degree(self, n: str) -> int:
        return len(self.edges.get(n, ()))

    def in_degree(self, n: str) -> int:
        return len(self.reverse.get(n, ()))


def resolve_imports(
    files: dict[str, FileInfoLike],
    roots: list[str] | None = None,
) -> Graph:
    """Turn raw import specifiers into edges between files in the repo.

    Handles the resolution rules that actually matter and skips the rest
    honestly: relative paths (always), TS path aliases (from tsconfig), Python
    dotted modules, Go module paths. Anything unresolved is dropped rather than
    guessed, because a wrong edge is worse than a missing one.
    """
    g = Graph()
    by_path = {p: f for p, f in files.items()}
    for p in by_path:
        g.add_node(p)

    # Build a suffix index so `@/components/ui/button` can find
    # apps/web/components/ui/button.tsx without a full tsconfig parse.
    suffix_index: dict[str, list[str]] = defaultdict(list)
    for p in by_path:
        stem = p.rsplit(".", 1)[0]
        parts = stem.split("/")
        for i in range(len(parts)):
            suffix_index["/".join(parts[i:])].append(p)

    scopes = _ts_aliases_by_scope(roots or [])

    for path, info in by_path.items():
        for spec in info.imports:
            target = _resolve_one(path, spec, suffix_index, scopes, by_path)
            if target:
                g.add_edge(path, target)
    return g


class FileInfoLike:
    """Structural stand-in to avoid a circular import with extract.py."""

    path: str
    imports: list[str]


def _resolve_one(
    src_path: str,
    spec: str,
    suffix_index: dict[str, list[str]],
    scopes,
    by_path: dict[str, FileInfoLike],
) -> str | None:
    if spec.startswith("."):
        # String concatenation, not pathlib. Path is constructed 684k times on a
        # 53k-file repo and showed up as 2.9s of a 8s warm build.
        parent = src_path.rsplit("/", 1)[0] if "/" in src_path else ""
        base = f"{parent}/{spec}" if parent else spec
        if base.startswith("./"):
            base = base[2:]
        parts = base.split("/")
        out: list[str] = []
        for part in parts:
            if part == "..":
                if out:
                    out.pop()
            elif part not in (".", ""):
                out.append(part)
        return _try_extensions("/".join(out), by_path)

    aliased = _alias_for(src_path, spec, scopes)
    if aliased:
        hit = _try_extensions(aliased, by_path)
        if hit:
            return hit

    # Python: dotted module
    if "." in spec and "/" not in spec:
        cand = spec.replace(".", "/")
        hit = _try_extensions(cand, by_path)
        if hit:
            return hit

    # Last resort: longest suffix match. This is what makes Go module paths and
    # deep package imports work without a resolver per language.
    parts = spec.split("/")
    for i in range(len(parts)):
        cand = "/".join(parts[i:])
        hits = suffix_index.get(cand)
        if hits and len(hits) == 1:
            return hits[0]
    return None


_TRY_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs",
             "/index.ts", "/index.tsx", "/index.js", "/index.py")


def _try_extensions(base: str, by_path: dict[str, FileInfoLike]) -> str | None:
    if base in by_path:
        return base
    for ext in _TRY_EXTS:
        cand = base + ext
        if cand in by_path:
            return cand
    return None


def _ts_aliases_by_scope(roots: list[str]) -> list[tuple[str, dict[str, str]]]:
    """Alias mappings, scoped to the directory whose tsconfig declared them.

    `@/components/x` means different files in `apps/web` and `apps/desktop`, so a
    single global alias table is wrong in any monorepo. Returns
    [(scope_prefix, {alias: target})] sorted longest-prefix-first, so a file is
    resolved against the most specific config that covers it.
    """
    import json

    scopes: list[tuple[str, dict[str, str]]] = []

    for root in roots:
        root_p = Path(root)
        seen: set[Path] = set()
        for pattern in ("tsconfig.json", "*/tsconfig.json", "*/*/tsconfig.json",
                        "apps/*/tsconfig*.json", "packages/*/tsconfig*.json"):
            for cfg in sorted(root_p.glob(pattern)):
                if cfg in seen:
                    continue
                seen.add(cfg)
                try:
                    raw = cfg.read_text()
                    raw = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
                    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
                    data = json.loads(raw)
                except Exception:
                    continue

                opts = data.get("compilerOptions") or {}
                paths = opts.get("paths") or {}
                if not paths:
                    continue

                base = opts.get("baseUrl", ".") or "."
                mapping: dict[str, str] = {}
                for alias, targets in paths.items():
                    if not targets:
                        continue
                    key = alias.rstrip("*").rstrip("/")
                    tgt = targets[0].rstrip("*").rstrip("/")
                    try:
                        resolved = (cfg.parent / base / tgt).resolve().relative_to(
                            root_p.resolve()
                        )
                        mapping[key] = str(resolved).lstrip("./")
                    except (ValueError, OSError):
                        mapping[key] = str(Path(base) / tgt).lstrip("./")

                if mapping:
                    try:
                        scope = str(cfg.parent.resolve().relative_to(root_p.resolve()))
                    except ValueError:
                        scope = "."
                    scopes.append(("" if scope == "." else scope, mapping))

    # Longest scope first so `apps/web` beats `.`.
    scopes.sort(key=lambda kv: -len(kv[0]))
    return scopes


def _alias_for(path: str, spec: str, scopes) -> str | None:
    """Resolve `spec` using the nearest tsconfig that covers `path`."""
    for scope, mapping in scopes:
        if scope and not path.startswith(scope + "/"):
            continue
        for alias, target in mapping.items():
            if spec == alias or spec.startswith(alias + "/"):
                rest = spec[len(alias):].lstrip("/")
                return f"{target}/{rest}".strip("/") if rest else target
    return None


def pagerank(
    g: Graph,
    damping: float = 0.85,
    iterations: int = 30,
    personalization: dict[str, float] | None = None,
    focus_floor: float = 0.35,
) -> dict[str, float]:
    """PageRank with optional personalization toward the agent's working set.

    Personalization turns a static map into a task-aware one: the same repo viewed
    from `auth/session.ts` ranks different files than from the billing module.

    `focus_floor` is the important parameter. A pure personalization set assigns
    exactly zero to unreachable nodes, which sorts whole unrelated modules to the
    bottom and silently drops them from a focused map. An agent working in `auth`
    still needs to know `billing` exists. The floor reserves 35% of teleport mass
    for everything outside the focus set.
    """
    nodes = list(g.nodes)
    count = len(nodes)
    if count == 0:
        return {}

    if personalization:
        focus_total = sum(personalization.values()) or 1.0
        teleport: dict[str, float] = {
            k: focus_floor * v / focus_total
            for k, v in personalization.items()
            if k in g.edges
        }
        rest = [node for node in nodes if node not in personalization]
        if rest:
            share = (1.0 - focus_floor) / len(rest)
            for node in rest:
                teleport[node] = teleport.get(node, 0.0) + share
        elif teleport:
            # Everything is in the focus set; spread the remainder evenly.
            bump = (1.0 - focus_floor) / len(teleport)
            for key in teleport:
                teleport[key] += bump
        uniform = False
    else:
        teleport = {}
        uniform = True

    # Integer-indexed lists instead of dicts. With 53k nodes and 30 iterations the
    # dict version spent 1.7s rebuilding keys; list index lookups are the same
    # algorithm with a third of the allocation.
    index_of = {n: i for i, n in enumerate(nodes)}
    inv = 1.0 / count
    tel = [inv if uniform else teleport.get(n, 0.0) for n in nodes]
    outs_idx = [
        [index_of[d] for d in (g.edges.get(n) or ())] for n in nodes
    ]
    base_flow = (1.0 - damping)

    ranks = [inv] * count
    for _ in range(iterations):
        nxt = [base_flow * t for t in tel]
        dangling = 0.0
        for i, outs in enumerate(outs_idx):
            ri = ranks[i]
            if not outs:
                dangling += ri
                continue
            per = damping * ri / len(outs)
            for j in outs:
                nxt[j] += per
        if dangling:
            add = damping * dangling
            for i, t in enumerate(tel):
                nxt[i] += add * t
        ranks = nxt

    return dict(zip(nodes, ranks))


def co_change(
    repo: str,
    limit_commits: int = 300,
    only_paths: set[str] | None = None,
    min_count: int = 2,
) -> dict[tuple[str, str], int]:
    """Files that historically changed together, from `git log`.

    Returns a sparse map of (a, b) -> co-occurrence count. This catches coupling
    the import graph cannot see: a component and its hook, a module and its test,
    a migration and the model it backfills. Measured on a real repo with 1,080
    commits: 1,387 pairs, and the top pair changed together 15 times while having
    no import edge between them.

    Cost control:
      - `--name-only` avoids diffs entirely (the expensive part)
      - files a commit touched more than 40 times is a rename or a codemod, and
        its pairs are noise, so it is skipped
      - pairs seen once are coincidence; `min_count` drops them
      - `only_paths` filters to indexed files before pairing, which is the single
        biggest saving on a repo with vendored history
    """
    try:
        out = subprocess.run(
            ["git", "-C", repo, "log", f"--max-count={limit_commits}",
             "--name-only", "--no-renames", "--pretty=format:%x00"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return {}

    pairs: dict[tuple[str, str], int] = defaultdict(int)
    for commit in out.split("\x00"):
        raw = [l.strip() for l in commit.splitlines() if l.strip()]
        if len(raw) > 40:
            continue
        if only_paths is not None:
            raw = [f for f in raw if f in only_paths]
        if len(raw) < 2:
            continue
        for i, a in enumerate(raw):
            for b in raw[i + 1:]:
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                pairs[key] += 1

    return {k: v for k, v in pairs.items() if v >= min_count}


def cochange_delta(
    src: str, cochange: dict[str, int], scale: float = 0.6, cap: int = 12
) -> dict[str, float]:
    """Turn pair counts into a rank perturbation for files related to `src`.

    Added to PageRank, not multiplied: a file with no import edge to anything is
    still relevant if it always changes alongside what you are editing, and a
    multiplier would kill it at zero.
    """
    out: dict[str, float] = {}
    for key, count in cochange.items():
        a, b = key.split("|", 1)
        other = b if a == src else a if b == src else None
        if other is None:
            continue
        out[other] = min(cap, count) * scale
    return out
