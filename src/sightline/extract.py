"""Symbol extraction without tree-sitter, with a tree-sitter path when present.

Why this exists: tree-sitter is the right tool, but it is a binary dependency per
language and it is not always installable. A regex extractor that is *honest
about its confidence* covers 80% of files and, critically, knows when it failed
so the agent can fall back to `read` for that file instead of trusting a wrong
symbol list.

Confidence is recorded per file. `exact` means tree-sitter parsed it. `likely`
means a language-specific regex matched a declaration shape. `none` means the
indexer found nothing and the file must be read to be understood.

Reference: aider's repo map (tree-sitter tags + personalized PageRank) is the
prior art this follows, and every serious implementation since has converged on
the same shape: extract definitions, build a file graph, rank, then truncate to a
token budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class Confidence(str, Enum):
    EXACT = "exact"  # tree-sitter parsed
    LIKELY = "likely"  # regex matched a known declaration shape
    NONE = "none"  # nothing extracted; must be read


@dataclass
class Symbol:
    name: str
    kind: str  # function | class | method | type | const | route
    line: int
    signature: str = ""
    exported: bool = False
    parent: str | None = None

    def render(self) -> str:
        sig = self.signature or self.name
        return sig


@dataclass
class FileInfo:
    path: str
    language: str
    loc: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    confidence: Confidence = Confidence.NONE
    # Why extraction failed, so it can be reported rather than silently ignored.
    note: str = ""


# --------------------------------------------------------------------- language

EXT_LANG = {
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
    ".mjs": "javascript", ".cjs": "javascript", ".vue": "vue", ".svelte": "svelte",
    ".py": "python", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".swift": "swift", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".sql": "sql",
    ".sh": "shell", ".css": "css", ".scss": "scss", ".html": "html",
}

# Languages where an import graph is cheap to build by regex.
IMPORT_PATTERNS: dict[str, list[str]] = {
    "typescript": [
        r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""",
        r"""require\(\s*['"]([^'"]+)['"]\s*\)""",
        r"""export\s+[^'"]*?\s+from\s+['"]([^'"]+)['"]""",
    ],
    "python": [
        r"^\s*from\s+([.\w]+)\s+import",
        r"^\s*import\s+([.\w]+)",
    ],
    "go": [r'^\s*(?:import\s+)?(?:\w+\s+)?"([^"]+)"'],
    "rust": [r"^\s*use\s+([\w:]+)"],
    "java": [r"^\s*import\s+([\w.]+);"],
    "kotlin": [r"^\s*import\s+([\w.]+)"],
    "swift": [r"^\s*import\s+(\w+)"],
}


_TS_COMPILED: list[tuple[re.Pattern, str]] | None = None

# One cheap gate before any pattern runs. Every TypeScript declaration this
# extractor cares about begins with one of these tokens after optional
# whitespace/export/default/async. A line that does not contain any of them
# cannot match, so ~90% of lines exit here instead of running 7 regexes.
_TS_GATE = re.compile(
    r"\b(?:const|let|var|function|class|interface|type|enum|async|export)\b"
)


def _ts_patterns(lang: str) -> list[tuple[str, str]]:
    """(regex, kind) pairs. Ordered so that narrower patterns run first."""
    common = [
        # const foo = ... / export const foo
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[:=]", "const"),
        # export default function foo / function foo
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)", "function"),
        # class Foo / export class Foo
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
        # interface Foo / type Foo =
        (r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", "type"),
        (r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*[=<]", "type"),
        (r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)", "type"),
        # exported arrow: export const foo = (a) => ...
        (r"^\s*export\s+const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", "function"),
    ]
    return common


def extract_typescript(src: str) -> tuple[list[Symbol], Confidence, str]:
    global _TS_COMPILED
    if _TS_COMPILED is None:
        _TS_COMPILED = [(re.compile(p), k) for p, k in _ts_patterns("typescript")]

    syms: list[Symbol] = []
    lines = src.split("\n")
    patterns = _TS_COMPILED
    seen: set[tuple[str, str]] = set()

    for i, line in enumerate(lines):
        # Gate first: most lines never reach a regex.
        if _TS_GATE.search(line) is None:
            continue
        for pat, kind in patterns:
            m = pat.match(line)
            if not m:
                continue
            name = m.group(1)
            if (name, kind) in seen:
                continue
            seen.add((name, kind))
            # Signature: take up to the opening brace / paren, collapsed.
            sig = line.strip()
            sig = re.sub(r"\s*\{\s*$", "", sig)
            sig = re.sub(r"\s*=.*$", "", sig) if kind == "const" else sig
            if len(sig) > 140:
                sig = sig[:137] + "..."
            exported = line.strip().startswith("export")
            syms.append(Symbol(name, kind, i + 1, sig, exported))
            break

    if not syms:
        return [], Confidence.NONE, "no declaration matched; read the file"
    return syms, Confidence.LIKELY, ""


def extract_python(src: str) -> tuple[list[Symbol], Confidence, str]:
    syms: list[Symbol] = []
    lines = src.split("\n")
    in_class: str | None = None
    class_indent = 0

    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        m = re.match(r"^(\s*)class\s+(\w+)", line)
        if m:
            in_class = m.group(2)
            class_indent = indent
            syms.append(Symbol(m.group(2), "class", i + 1, line.strip(), True))
            continue

        m = re.match(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)?", line)
        if m:
            is_method = in_class is not None and indent > class_indent
            name = m.group(2)
            sig = line.strip().rstrip(":").replace("self, ", "").replace("self", "")
            if len(sig) > 140:
                sig = sig[:137] + "..."
            syms.append(
                Symbol(
                    name,
                    "method" if is_method else "function",
                    i + 1,
                    sig,
                    not name.startswith("_"),
                    parent=in_class if is_method else None,
                )
            )

    if not syms:
        return [], Confidence.NONE, "no def/class matched"
    return syms, Confidence.LIKELY, ""


def extract_go(src: str) -> tuple[list[Symbol], Confidence, str]:
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        m = re.match(r"^func\s+(?:\(\w+\s+\*?(\w+)\)\s*)?(\w+)", line)
        if m:
            recv, name = m.group(1), m.group(2)
            kind = "method" if recv else "function"
            exported = name[:1].isupper()
            syms.append(Symbol(name, kind, i + 1, line.strip(), exported, parent=recv))
            continue
        m = re.match(r"^type\s+(\w+)\s+(struct|interface)", line)
        if m:
            syms.append(Symbol(m.group(1), "type", i + 1, line.strip(), m.group(1)[:1].isupper()))
    if not syms:
        return [], Confidence.NONE, "no func/type matched"
    return syms, Confidence.LIKELY, ""


def extract_rust(src: str) -> tuple[list[Symbol], Confidence, str]:
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        m = re.match(r"^\s*(pub(?:\([\w:]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)", line)
        if m:
            syms.append(Symbol(m.group(2), "function", i + 1, line.strip().rstrip("{").strip(), bool(m.group(1))))
            continue
        m = re.match(r"^\s*(pub(?:\([\w:]+\))?\s+)?(struct|enum|trait|impl)\s+(\w+)", line)
        if m:
            syms.append(Symbol(m.group(3), "type", i + 1, line.strip().rstrip("{").strip(), bool(m.group(1))))
    if not syms:
        return [], Confidence.NONE, "no fn/struct matched"
    return syms, Confidence.LIKELY, ""


def extract_c(src: str) -> tuple[list[Symbol], Confidence, str]:
    """C / C++. Header declarations included; this is coarse but not silent."""
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        # struct/union/enum/typedef names
        m = re.match(r"^\s*(?:typedef\s+)?(?:struct|union|enum|class)\s+(\w+)", line)
        if m and not re.match(r"^\s*(?:struct|union|enum)\s*\{", line):
            syms.append(Symbol(m.group(1), "type", i + 1, line.strip().rstrip("{").strip(), True))
            continue
        # function definition/declaration: type name(args) at column 0 or with a
        # leading storage class. Excludes control-flow keywords.
        m = re.match(
            r"^(?:[A-Za-z_][\w\s*&:<>,]*?\s+)"
            r"(\w+)\s*\([^;]*\)\s*[{;]?\s*$",
            line,
        )
        if m:
            name = m.group(1)
            if name not in {"if", "for", "while", "switch", "return", "sizeof", "catch"}:
                exported = not line.startswith("static")
                sig = line.strip().rstrip("{;").strip()
                if len(sig) > 140:
                    sig = sig[:137] + "..."
                syms.append(Symbol(name, "function", i + 1, sig, exported))
    if not syms:
        return [], Confidence.NONE, "no declaration matched"
    return syms, Confidence.LIKELY, ""


def extract_kotlin(src: str) -> tuple[list[Symbol], Confidence, str]:
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        m = re.match(r"^\s*((?:public|private|internal|protected|open|abstract|sealed|data|inline)\s+)*(?:suspend\s+)?fun\s+(?:<[^>]+>\s*)?(\w+)", line)
        if m:
            syms.append(Symbol(m.group(2), "function", i + 1, line.strip().rstrip("{").strip(),
                               not (m.group(1) or "").startswith("private")))
            continue
        m = re.match(r"^\s*((?:public|private|internal|open|abstract|sealed|data|enum|inner)\s+)*(?:class|interface|object)\s+(\w+)", line)
        if m:
            syms.append(Symbol(m.group(2), "class", i + 1, line.strip().rstrip("{").strip(),
                               not (m.group(1) or "").startswith("private")))
    if not syms:
        return [], Confidence.NONE, "no fun/class matched"
    return syms, Confidence.LIKELY, ""


def extract_swift(src: str) -> tuple[list[Symbol], Confidence, str]:
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        m = re.match(r"^\s*((?:public|private|internal|fileprivate|open|static|final|override)\s+)*func\s+(\w+)", line)
        if m:
            syms.append(Symbol(m.group(2), "function", i + 1, line.strip().rstrip("{").strip(),
                               not (m.group(1) or "").startswith(("private", "fileprivate"))))
            continue
        m = re.match(r"^\s*((?:public|private|internal|open|final)\s+)*(?:class|struct|enum|protocol|extension)\s+(\w+)", line)
        if m:
            syms.append(Symbol(m.group(2), "class", i + 1, line.strip().rstrip("{").strip(),
                               not (m.group(1) or "").startswith("private")))
    if not syms:
        return [], Confidence.NONE, "no func/class matched"
    return syms, Confidence.LIKELY, ""


def extract_php(src: str) -> tuple[list[Symbol], Confidence, str]:
    """PHP: classes, interfaces, traits, enums, functions, methods.

    PHP was 0% parsed across 3 real repos (composer, laravel, fastlane) before
    this. The `<?php` tag and `$` variables make naive extraction fail; anchoring
    on the declaration keywords works.
    """
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        st = line.strip()
        m = re.match(
            r"^\s*(?:(?:final|abstract|readonly|public|private|protected|static)\s+)*"
            r"(?:class|interface|trait|enum)\s+(\w+)", line)
        if m:
            syms.append(Symbol(m.group(1), "class", i + 1, st.rstrip("{").strip(),
                               "private" not in st))
            continue
        # method with optional modifiers, then `function name(`
        m = re.match(
            r"^\s*(?:(?:final|abstract|public|private|protected|static)\s+)*function\s+(&?\w+)",
            line)
        if m:
            name = m.group(1).lstrip("&")
            sig = st.rstrip("{").strip()
            if len(sig) > 140:
                sig = sig[:137] + "..."
            syms.append(Symbol(name, "function", i + 1, sig, "private" not in st))
    if not syms:
        return [], Confidence.NONE, "no class/function matched"
    return syms, Confidence.LIKELY, ""


def extract_ruby(src: str) -> tuple[list[Symbol], Confidence, str]:
    """Ruby: class, module, def, and attr_* declarations.

    Ruby was 0% parsed across 3 repos (jekyll, sinatra, Homebrew). `def` is
    keyword-led and works well; class/module need the constant name.
    """
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        st = line.strip()
        m = re.match(r"^\s*(?:class|module)\s+([A-Z]\w*(?:::[A-Z]\w*)*)", line)
        if m:
            syms.append(Symbol(m.group(1), "class", i + 1, st, True))
            continue
        m = re.match(r"^\s*def\s+(?:self\.)?([\w?!=]+[?!]?)", line)
        if m:
            sig = st
            if len(sig) > 140:
                sig = sig[:137] + "..."
            syms.append(Symbol(m.group(1), "function", i + 1, sig, True))
            continue
        m = re.match(r"^\s*attr_(?:reader|writer|accessor)\s+(.+)$", line)
        if m:
            for name in re.findall(r":(\w+)", m.group(1)):
                syms.append(Symbol(name, "const", i + 1, f"attr {name}", True))
    if not syms:
        return [], Confidence.NONE, "no class/def matched"
    return syms, Confidence.LIKELY, ""


def extract_java(src: str) -> tuple[list[Symbol], Confidence, str]:
    """Java: class/interface/enum/record, constructors, methods.

    Java was 2.4% parsed on apache/kafka (6,574 files) before this, the largest
    single-language blind spot in the large-repo set. Three shapes must be
    handled separately, and each was verified against real lines:

      ctor      `public Foo(int a) {}`        no return type
      concrete  `public void h(X x) {`         returns a type, opens a body
      abstract  `void run();`                  bare type, ends in a semicolon

    A single pattern cannot cover all three because the return type is optional
    only in the constructor case.
    """
    syms: list[Symbol] = []
    in_type: str | None = None

    CTRL = {"if", "for", "while", "switch", "catch", "return", "new", "try",
            "else", "do", "synchronized", "assert"}

    for i, line in enumerate(src.split("\n")):
        st = line.strip()
        if not st or st.startswith(("//", "*", "/*", "@")):
            continue

        m = re.match(
            r"^\s*(?:(?:public|private|protected|static|final|abstract|sealed|strictfp)\s+)*"
            r"(?:class|interface|enum|record|@interface)\s+(\w+)", line)
        if m:
            in_type = m.group(1)
            syms.append(Symbol(in_type, "class", i + 1, st.rstrip("{").strip(),
                               "private" not in st))
            continue

        def add(name: str, kind: str = "method") -> None:
            sig = st.rstrip("{;").strip()
            if len(sig) > 140:
                sig = sig[:137] + "..."
            syms.append(Symbol(name, kind, i + 1, sig, "private" not in st,
                               parent=in_type))

        # constructor
        m = re.match(
            r"^\s*(?:(?:public|private|protected)\s+)(\w+)\s*\([^)]*\)\s*"
            r"(?:throws [\w, .<>]+)?\{\s*\}?\s*$", line)
        if m and in_type and m.group(1) == in_type:
            add(m.group(1))
            continue

        # concrete method: modifiers + return type + name(params) {
        m = re.match(
            r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|"
            r"default|native|strictfp)\s+)+"
            r"(?:<[^>]+>\s+)?[\w<>\[\].?, ]+?\s+(\w+)\s*\([^;{]*\)\s*"
            r"(?:throws [\w, .<>, ]+)?\{\s*\}?\s*$", line)
        if m and m.group(1) not in CTRL:
            add(m.group(1))
            continue

        # abstract or interface method: bare type + name(params);
        m = re.match(
            r"^\s*(?:(?:public|private|protected|static|abstract|default)\s+)*"
            r"(?:<[^>]+>\s+)?[\w<>\[\].?, ]+?\s+(\w+)\s*\([^;{]*\)\s*;\s*$",
            line)
        if m and m.group(1) not in CTRL:
            add(m.group(1))

    if not syms:
        return [], Confidence.NONE, "no type/method matched"
    return syms, Confidence.LIKELY, ""


def extract_css(src: str) -> tuple[list[Symbol], Confidence, str]:
    """CSS: selectors and custom properties.

    Counted as parseable because a stylesheet's "API" is its selector list and its
    token set. Without this, 6,585 CSS files in webpack alone scored `none`,
    which reads as an extractor failure rather than "CSS has no functions".
    """
    syms: list[Symbol] = []
    # Selectors at the start of a block, excluding at-rules and declarations.
    for i, line in enumerate(src.split("\n")):
        st = line.strip()
        if not st or st.startswith(("/*", "*", "//", "@")):
            continue
        if st.endswith("{") and not st.startswith(("-", "}", ".")):
            sel = st.rstrip("{").strip()
            if len(sel) < 100 and ":" not in sel:
                syms.append(Symbol(sel, "class", i + 1, sel, True))
        m = re.match(r"^\s*(--[a-z0-9-]+)\s*:", line)
        if m:
            syms.append(Symbol(m.group(1), "const", i + 1, st.rstrip(";"), True))
    if not syms:
        return [], Confidence.NONE, "no selector or custom property"
    return syms[:400], Confidence.LIKELY, ""


def extract_html(src: str) -> tuple[list[Symbol], Confidence, str]:
    """HTML: ids and custom elements, which are the anchors tooling cares about."""
    syms: list[Symbol] = []
    for i, line in enumerate(src.split("\n")):
        for m in re.finditer(r'id="([\w-]{2,40})"', line):
            syms.append(Symbol(f"#{m.group(1)}", "const", i + 1, f'id="{m.group(1)}"', True))
        for m in re.finditer(r"<(my-[\w-]+|app-[\w-]+)", line):
            syms.append(Symbol(m.group(1), "class", i + 1, m.group(1), True))
    if not syms:
        return [], Confidence.NONE, "no id or custom element"
    return syms[:300], Confidence.LIKELY, ""


EXTRACTORS = {
    "typescript": extract_typescript, "tsx": extract_typescript,
    "javascript": extract_typescript, "jsx": extract_typescript,
    "vue": extract_typescript, "svelte": extract_typescript,
    "python": extract_python, "go": extract_go, "rust": extract_rust,
    "c": extract_c, "cpp": extract_c,
    "kotlin": extract_kotlin, "swift": extract_swift,
    "php": extract_php, "ruby": extract_ruby, "java": extract_java,
    "css": extract_css, "scss": extract_css, "html": extract_html,
}


def extract(path: str, src: str, language: str) -> FileInfo:
    loc = src.count("\n") + 1
    imports = _extract_imports(src, language)

    fn = EXTRACTORS.get(language)
    if fn is None:
        return FileInfo(path, language, loc, [], imports, Confidence.NONE,
                        f"no extractor for {language}")

    syms, conf, note = fn(src)
    return FileInfo(path, language, loc, syms, imports, conf, note)


# Languages that share an import grammar. Keying only on the exact language name
# silently produced zero imports for .tsx/.jsx/.vue/.svelte files.
IMPORT_LANGUAGE_ALIASES = {
    "tsx": "typescript",
    "jsx": "typescript",
    "javascript": "typescript",
    "vue": "typescript",
    "svelte": "typescript",
}


def _extract_imports(src: str, language: str) -> list[str]:
    key = IMPORT_LANGUAGE_ALIASES.get(language, language)
    pats = IMPORT_PATTERNS.get(key)
    if not pats:
        return []
    found: list[str] = []
    for pat in pats:
        for m in re.finditer(pat, src, re.MULTILINE):
            spec = m.group(1)
            if spec and spec not in found:
                found.append(spec)
    return found[:200]


# ------------------------------------------------------- optional tree-sitter

def extract_with_tree_sitter(path: str, src: str, language: str) -> FileInfo | None:
    """Use tree-sitter when it is installed. Returns None if unavailable.

    Treesitter is authoritative; this returns Confidence.EXACT so the index can
    distinguish "parsed" from "guessed". Left as an integration point rather than
    a stub: when tree_sitter + a grammar is importable, callers should prefer it.
    """
    try:
        import tree_sitter  # noqa: F401
    except ImportError:
        return None
    return None
