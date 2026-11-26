"""Index builder: walk, extract, graph, rank, cache.

The cache is the whole point on a 1 GB repo. A full index of 1,520 files takes
seconds; doing it on every agent turn would be worse than reading files. So:
content-hashed per file, incremental, and invalidated only for what changed.

Design constraint from the research: index build cost is a real finding. One
published benchmark measured a 12.6s index and a 113s cross-repo pass for a
competitor. If the index is slower than grepping, the agent will not use it.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import extract, graph

SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "coverage", ".turbo",
    "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", "target",
    ".cache", "vendor", ".idea", ".vscode", "Pods", ".gradle", ".dart_tool",
    # Build outputs that are commonly COMMITTED, so a name-based skip is the only
    # way to catch them:
    "lib", "esm", "cjs", "_build", "obj", "bin", "out", "release", "site-packages",
    "bower_components", "jspm_packages", ".angular", ".nuxt", ".svelte-kit",
    "storybook-static", ".output", ".vercel", ".netlify", ".parcel-cache",
}
# NOTE on `lib`: skipping it globally is wrong for Python (where `lib/` is often
# real source) and for Rust (`src/` convention means `lib.rs`, not `lib/`). The
# skip is therefore conditional - see _should_skip_dir below.

# Compiled-artifact markers.
#
# `"use strict";` alone is NOT one. Plenty of hand-written CommonJS and even
# ESM-era source files open with it: eslint's entire lib/ does, and treating that
# as generated deleted 5 of its real modules and pushed its map accuracy down.
# A marker must be a thing only a transpiler emits.
ARTIFACT_MARKERS = (
    "Object.defineProperty(exports,",     # Babel/tsc CJS interop
    "exports.__esModule",                 # same
    "//# sourceMappingURL=",               # emitted after minification/bundling
    "/* eslint-disable */\n/* tslint:",     # generator preamble
    "__webpack_require__(",                # bundler runtime
    "!function(e,t){",                     # minified UMD opener
)

# Files smaller than this in a directory that also contains an identical-shaped
# sibling set are usually generated stubs, not source.
MIN_MEANINGFUL_BYTES = 60

# Files above this are almost always data, generated, or minified.
MAX_FILE_BYTES = 1_500_000

# Bump when the extractor logic changes. Part of the cache key, so improving a
# parser invalidates the affected entries instead of silently reusing stale
# symbol lists forever. This is exactly the bug that made newly-added C, Kotlin
# and Swift extractors appear to do nothing: the content hash had not changed.
EXTRACTOR_VERSION = "15"


@dataclass
class Index:
    root: str
    files: dict[str, dict] = field(default_factory=dict)
    ranks: dict[str, float] = field(default_factory=dict)
    built_from_commit: str | None = None
    stats: dict = field(default_factory=dict)
    # ("a|b") -> co-occurrence count. Declared, not monkey-patched: an attribute
    # assigned after construction does not survive serialization, which is how it
    # ended up computed-but-unused.
    cochange: dict[str, int] = field(default_factory=dict)

    # -- serialization -----------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "Index":
        d = json.loads(raw)
        return cls(**d)

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Index | None":
        p = Path(path)
        if not p.exists():
            return None
        try:
            return cls.from_json(p.read_text(encoding="utf-8"))
        except Exception:
            return None


def _file_sig(path: Path) -> tuple[str, int]:
    """Cheap change signature: (size, mtime_ns).

    Hashing every file cost 106k open() calls and 1.1s per warm build on a 53k-file
    repo. Every build system uses stat-based signatures for the same reason: it is
    one syscall instead of a full read, and content changes move mtime.

    `verify_hashes=True` on build() restores full SHA-256 comparison when the
    caller needs to be certain (CI, or a filesystem that lies about mtime).
    """
    try:
        st = path.stat()
    except OSError:
        return "", 0
    return f"{st.st_size}:{st.st_mtime_ns}", st.st_size


def _language(path: Path) -> str | None:
    return extract.EXT_LANG.get(path.suffix.lower())


# Binary and archive extensions. Everything else is indexed (size and existence),
# because an allowlist can never reach parity with a directory listing: there is
# always an extension nobody anticipated (.njk, .mts, .cts, .man, .xhtml).
BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff", ".avif",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".mp3", ".mp4", ".webm", ".mov", ".avi", ".wav", ".ogg", ".flac",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a", ".class", ".jar",
    ".pyc", ".pyo", ".wasm", ".pt", ".onnx", ".safetensors", ".db", ".sqlite",
}

_LISTING_EXTS_OLD = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".env", ".cfg", ".conf",
    ".md", ".mdx", ".txt", ".rst", ".lock", ".mod", ".sum", ".gradle",
    ".dockerfile", ".make", ".mk", ".cmake", ".sql", ".graphql", ".proto",
    ".xml", ".plist", ".entitlements", ".gitignore", ".gitattributes",
    ".npmrc", ".nvmrc", ".editorconfig", ".eslintrc", ".prettierrc",
    ".png", ".svg", ".ico", ".jpg", ".webp", ".woff", ".woff2", ".ttf",
    ".pdf", ".zip", ".tar", ".gz",
}
# Directories whose contents are noise even for a listing.
LISTING_SKIP = {"node_modules", ".git", "dist", "build", "coverage", "target",
                "__pycache__", ".next", "vendor", ".cache"}


LITERAL_FILES = {"license", "licence", "copying", "notice", "authors",
                 "contributors", "changelog", "readme", "makefile", "dockerfile",
                 "procfile", "gemfile", "rakefile", "justfile", "brewfile",
                 "vagrantfile", "jenkinsfile", "cmakelists.txt"}


def _worth_name(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    if ext in BINARY_EXTS:
        return False
    return name != ".DS_Store"


def _looks_generated_str(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(400).decode("utf-8", "ignore")
    except OSError:
        return False
    return any(m in head for m in ARTIFACT_MARKERS)


def _worth_listing(p: Path) -> bool:
    """Index every non-binary file. Exclusion, not allowlist.

    A directory listing sees everything, so any allowlist is incomplete by
    construction. eslist had .njk, .mts, .cts, CODEOWNERS, .c8rc - every one a
    file a real question might name.
    """
    if p.suffix.lower() in BINARY_EXTS:
        return False
    if p.name in {".DS_Store"}:
        return False
    return True


def _should_skip_dir(name: str, parent: Path) -> bool:
    """Skip a directory by name, with the exceptions that matter.

    `lib` is the hard case and both answers appear in the wild:

      mui/material-ui   packages/mui-icons-material/lib/  = 10,751 transpiled
                        files, with lib/ next to src/     -> output, skip
      webpack/webpack   lib/                              = the actual source,
                        no src/ sibling                   -> source, keep
      python/ruby       lib/                              = source

    The discriminator that works for both: **lib/ is output if a sibling src/
    exists.** Skipping it unconditionally dropped webpack to 28% parsed, which
    is a worse failure than indexing some artifacts.
    """
    if name not in SKIP_DIRS:
        return False
    if name not in ("lib", "bin", "out", "cjs", "esm", "dist", "build"):
        return True
    return _dir_looks_built(name, parent)


# A build-output NAME is not evidence of build output. eslint has
# `tests/fixtures/config-file/cjs/` holding one hand-written `.eslintrc.cjs`, and
# skipping it by name dropped a real config file. A directory with a handful of
# files is a fixture or a small source dir, never a build tree.
LOOKS_LIKE_OUTPUT_MIN_FILES = 8


def _dir_looks_built(name: str, parent: Path) -> bool:
    """Is a build-output-named directory actually build output?"""
    target = parent / name
    try:
        entries = list(os.scandir(target))
    except OSError:
        return True

    files = [e for e in entries if e.is_file()]

    # A handful of files is a fixture, not a build tree.
    if len(files) < LOOKS_LIKE_OUTPUT_MIN_FILES:
        return False

    if name == "bin":
        # `bin/` is compiled output in .NET/Java/Go and the CLI entry points in
        # Node/Python. Text scripts and no producing manifest means source.
        texty = sum(1 for e in files
                    if Path(e.name).suffix in
                    {".js", ".mjs", ".cjs", ".ts", ".py", ".sh", ".rb"})
        has_proj = any((parent / f).exists() for f in
                       (".csproj", "pom.xml", "build.gradle", "go.mod"))
        return bool(has_proj) or texty == 0

    if name in ("lib", "cjs", "esm", "out"):
        # Output exists because something was built from a sibling source tree.
        if (parent / "src").is_dir() or (parent / "source").is_dir():
            # Python and Ruby keep real modules under lib/ even so.
            exts = {Path(e.name).suffix for e in files}
            return not (exts & {".py", ".pyi", ".rb"})
        return False

    # dist/ and build/ are almost always output when they are large.
    return True


def _looks_generated(path: Path) -> bool:
    """Detect committed build output by content markers only.

    An earlier version also treated files under 60 bytes as generated. That is
    wrong: a 36-byte Python class is real source, and the rule silently dropped
    every small module. Size is not a signal; markers are.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:400]
    except OSError:
        return False
    return any(marker in head for marker in ARTIFACT_MARKERS)


def walk(root: str, max_files: int = 60_000) -> list[Path]:
    """Collect source files, skipping vendored and generated trees.

    Depth-first with an explicit stack, because a recursive glob on a 1 GB tree
    with deep `node_modules` nests far enough to blow the recursion limit.
    """
    out: list[str] = []
    stack = [root]

    while stack and len(out) < max_files:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError):
            continue
        for e in entries:
            # Dotfiles are real files. `.eslintrc.js`, `.env.example`,
            # `.prettierrc` are exactly what a "where is the config" question
            # needs, and dir_list sees them. Only skip the VCS and tool caches.
            if e.name in {".git", ".hg", ".svn", ".DS_Store", ".sightline"}:
                continue
            if e.is_dir(follow_symlinks=False):
                if _should_skip_dir(e.name, Path(current)):
                    continue
                stack.append(e.path)
            elif e.is_file(follow_symlinks=False):
                ext = os.path.splitext(e.name)[1].lower()
                known = extract.EXT_LANG.get(ext)
                if known is None and not _worth_name(e.name):
                    continue
                try:
                    if e.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                if _looks_generated_str(e.path):
                    continue
                out.append(e.path)
    return out


def build(root: str, cache: Index | None = None, with_cochange: bool = True,
          verify_hashes: bool = False) -> Index:
    """Build or incrementally update an index.

    Incremental: a file whose content hash matches the cache is reused verbatim,
    including its extracted symbols. On a warm cache with a few changed files
    this is milliseconds.
    """
    root_p = Path(root).resolve()
    root_s = str(root_p)
    prev = cache.files if cache else {}
    same_extractor = bool(cache and cache.stats.get("extractor_version") == EXTRACTOR_VERSION)
    prev_hash = {p: f.get("hash") for p, f in prev.items()} if same_extractor else {}

    files: dict[str, dict] = {}
    changed = reused = 0
    conf_counts = {"exact": 0, "likely": 0, "none": 0}

    root_prefix = root_s.rstrip("/") + "/"
    for abs_str in walk(root_s):
        abs_path = Path(abs_str)
        rel = abs_str[len(root_prefix):] if abs_str.startswith(root_prefix) else abs_str
        try:
            sig, _size = _file_sig(abs_path)
        except OSError:
            continue
        digest = _file_hash(abs_path) if verify_hashes else sig

        if rel in prev and prev_hash.get(rel) == digest:
            files[rel] = prev[rel]
            reused += 1
            conf_counts[prev[rel]["confidence"]] = conf_counts.get(prev[rel]["confidence"], 0) + 1
            continue

        try:
            src = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        lang = _language(abs_path) or "none"
        info = extract.extract(rel, src, lang)
        try:
            nbytes = abs_path.stat().st_size
        except OSError:
            nbytes = 0
        files[rel] = {
            "hash": digest,
            "size": nbytes,
            "language": info.language,
            "loc": info.loc,
            "confidence": info.confidence.value,
            "note": info.note,
            "imports": info.imports,
            "symbols": [asdict(s) for s in info.symbols],
        }
        changed += 1
        conf_counts[info.confidence.value] = conf_counts.get(info.confidence.value, 0) + 1

    # Graph + rank
    class _Info:
        def __init__(self, d):
            self.path = d
            self.imports = files[d]["imports"]

    infos = {p: _Info(p) for p in files}
    g = graph.resolve_imports(infos, roots=[root_s])
    ranks = graph.pagerank(g)

    co: dict[str, int] = {}
    if with_cochange:
        pairs = graph.co_change(root_s, limit_commits=300, only_paths=set(files))
        for (a, b), count in pairs.items():
            co[f"{a}|{b}"] = count

    idx = Index(
        root=root_s,
        files=files,
        ranks={k: round(v, 8) for k, v in ranks.items()},
        stats={
            "extractor_version": EXTRACTOR_VERSION,
            "files": len(files),
            "changed": changed,
            "reused": reused,
            "edges": sum(len(v) for v in g.edges.values()),
            "confidence": conf_counts,
            "cochange_pairs": len(co),
            "loc": sum(f["loc"] for f in files.values()),
        },
    )
    idx.cochange = co
    return idx
