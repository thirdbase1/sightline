"""Tests for the pure-logic modules. No git, no tree-sitter, no network."""

from __future__ import annotations

import pytest

from sightline import extract, graph, render


class TestExtract:
    def test_typescript_declarations(self):
        src = """
import { db } from "./db";

export function compactMessages(messages, model) {
  return messages;
}

export class Compactor {
  run() {}
}

export const STATS = 1;
interface Config { a: string }
type Tier = 1 | 2;
"""
        syms, conf, _ = extract.extract_typescript(src)
        names = {s.name for s in syms}
        assert "compactMessages" in names
        assert "Compactor" in names
        assert "STATS" in names
        assert "Config" in names
        assert conf == extract.Confidence.LIKELY

    def test_python_class_and_method_parenting(self):
        src = '''
class Compactor:
    def run(self, messages):
        pass

def top_level():
    pass
'''
        syms, conf, _ = extract.extract_python(src)
        kinds = {(s.name, s.kind, s.parent) for s in syms}
        assert ("Compactor", "class", None) in kinds
        assert ("run", "method", "Compactor") in kinds
        assert ("top_level", "function", None) in kinds
        assert conf == extract.Confidence.LIKELY

    def test_go_receiver_detection(self):
        src = """
package main

func (s *Server) Handle() {}
func Helper() {}
type Config struct { A string }
"""
        syms, _, _ = extract.extract_go(src)
        by_name = {s.name: s for s in syms}
        assert by_name["Handle"].kind == "method"
        assert by_name["Handle"].parent == "Server"
        assert by_name["Helper"].kind == "function"

    def test_unparseable_file_reports_none_not_empty(self):
        """A file we cannot parse must be marked, not silently empty."""
        info = extract.extract("data.bin", "\x00\x01\x02 not code", "unknownlang")
        assert info.confidence == extract.Confidence.NONE
        assert info.note  # says why

    def test_empty_source_is_none(self):
        info = extract.extract("x.ts", "", "typescript")
        assert info.confidence == extract.Confidence.NONE

    def test_imports_extracted(self):
        src = 'import a from "./a";\nimport { b } from "@/lib/b";\nconst c = require("./c");'
        imports = extract._extract_imports(src, "typescript")
        assert "./a" in imports
        assert "@/lib/b" in imports
        assert "./c" in imports


class TestGraph:
    def test_resolves_relative_import(self):
        class Info:
            def __init__(self, p, imps):
                self.path = p
                self.imports = imps

        files = {
            "src/a.ts": Info("src/a.ts", ["./b"]),
            "src/b.ts": Info("src/b.ts", []),
        }
        g = graph.resolve_imports(files, roots=[])
        assert "src/b.ts" in g.edges["src/a.ts"]

    def test_unresolved_import_is_dropped_not_guessed(self):
        class Info:
            def __init__(self, p, imps):
                self.path = p
                self.imports = imps

        files = {"a.ts": Info("a.ts", ["some-external-pkg"])}
        g = graph.resolve_imports(files, roots=[])
        assert g.edges["a.ts"] == set()

    def test_self_edge_ignored(self):
        g = graph.Graph()
        g.add_edge("a.ts", "a.ts")
        assert g.edges["a.ts"] == set()

    def test_pagerank_concentrates_on_hubs(self):
        g = graph.Graph()
        # a, b, c all import hub
        for n in ("a", "b", "c"):
            g.add_edge(n, "hub")
        ranks = graph.pagerank(g)
        assert ranks["hub"] > ranks["a"]
        assert abs(sum(ranks.values()) - 1.0) < 0.05

    def test_personalization_shifts_rank(self):
        g = graph.Graph()
        g.add_edge("x", "target_x")
        g.add_edge("y", "target_y")
        base = graph.pagerank(g)
        focused = graph.pagerank(g, personalization={"x": 1.0})
        # Starting from x should lift target_x relative to target_y.
        base_ratio = base["target_x"] / base["target_y"]
        focus_ratio = focused["target_x"] / focused["target_y"]
        assert focus_ratio > base_ratio


class TestRender:
    def _idx(self):
        from sightline.index import Index

        return Index(
            root="/x",
            files={
                "src/big.ts": {
                    "hash": "1", "language": "typescript", "loc": 900,
                    "confidence": "likely", "note": "", "imports": [],
                    "symbols": [
                        {"name": f"fn{i}", "kind": "function", "line": i,
                         "signature": f"export function fn{i}()", "exported": True,
                         "parent": None}
                        for i in range(60)
                    ],
                },
                "src/small.ts": {
                    "hash": "2", "language": "typescript", "loc": 20,
                    "confidence": "likely", "note": "", "imports": [],
                    "symbols": [
                        {"name": "only", "kind": "function", "line": 1,
                         "signature": "export function only()", "exported": True,
                         "parent": None}
                    ],
                },
                "src/broken.ts": {
                    "hash": "3", "language": "go", "loc": 10,
                    "confidence": "none", "note": "no func/type matched",
                    "imports": [], "symbols": [],
                },
            },
            ranks={"src/big.ts": 0.5, "src/small.ts": 0.3, "src/broken.ts": 0.2},
            stats={"files": 3, "loc": 930, "edges": 0},
        )

    def test_budget_is_respected(self):
        idx = self._idx()
        for budget in (500, 1000, 2000, 4000):
            out = render.render_map(idx, render.RenderOptions(budget=budget))
            tokens = len(out) / render.CHARS_PER_TOKEN
            assert tokens <= budget * 1.02, f"budget {budget} exceeded: {tokens:.0f}"

    def test_large_file_is_capped(self):
        idx = self._idx()
        out = render.render_map(idx, render.RenderOptions(budget=8000))
        # 60 symbols in big.ts must not all appear.
        assert out.count("export function fn") < 60
        assert "more (sightline file src/big.ts)" in out

    def test_unparsed_file_is_marked_readable(self):
        idx = self._idx()
        out = render.render_map(idx, render.RenderOptions(budget=8000))
        assert "unparsed" in out

    def test_focus_biases_order(self):
        idx = self._idx()
        out = render.render_map(
            idx, render.RenderOptions(budget=8000, focus=["src/small.ts"])
        )
        assert "small.ts" in out

    def test_no_files_produces_valid_output(self):
        from sightline.index import Index

        empty = Index(root="/x", files={}, ranks={}, stats={"files": 0, "loc": 0, "edges": 0})
        out = render.render_map(empty)
        assert "0 files" in out


class TestPhpAndRuby:
    """Both were 0% parsed across 6 real repos before these extractors existed."""

    def test_php_class_and_methods(self):
        from sightline import extract

        src = """<?php
class OrderService extends Base {
    public function handle($order) {}
    private function validate($order) {}
}
function global_helper() {}
"""
        syms, conf, _ = extract.extract_php(src)
        names = {(s.name, s.kind) for s in syms}
        assert ("OrderService", "class") in names
        assert ("handle", "function") in names
        assert ("validate", "function") in names
        assert ("global_helper", "function") in names
        assert conf == extract.Confidence.LIKELY

    def test_php_private_marked_not_exported(self):
        from sightline import extract

        src = "<?php\nclass A {\n  private function secret() {}\n}"
        syms, _, _ = extract.extract_php(src)
        secret = next(s for s in syms if s.name == "secret")
        assert secret.exported is False

    def test_php_empty_file_is_none(self):
        from sightline import extract

        syms, conf, note = extract.extract_php("<?php\n// nothing here")
        assert conf == extract.Confidence.NONE
        assert note

    def test_ruby_class_module_and_def(self):
        from sightline import extract

        src = """class Wallet < Base
  module Helpers
  end
  def balance
  end
  def self.build(owner)
  end
  def valid?
  end
end
"""
        syms, conf, _ = extract.extract_ruby(src)
        names = {s.name for s in syms}
        assert "Wallet" in names
        assert "Helpers" in names
        assert "balance" in names
        assert "build" in names
        assert "valid?" in names
        assert conf == extract.Confidence.LIKELY

    def test_ruby_attr_accessor_expands(self):
        from sightline import extract

        src = "class A\n  attr_accessor :name, :age\nend"
        syms, _, _ = extract.extract_ruby(src)
        names = {s.name for s in syms}
        assert "name" in names
        assert "age" in names

    def test_ruby_private_methods_still_listed(self):
        """Ruby privacy is dynamic; listing them is better than omitting them."""
        from sightline import extract

        src = "class A\n  def visible; end\n  private\n  def hidden; end\nend"
        syms, _, _ = extract.extract_ruby(src)
        assert {s.name for s in syms} >= {"visible", "hidden"}


class TestRollupCalibration:
    """Directory rollup must fire only when per-file detail is useless.

    Measured across 118 repos: per-file detail answers 100% of a 29-question
    benchmark at 1,213 files but covers 0.3% of a 7,922-file repo. An earlier
    threshold fired at 120 files and broke the benchmark (72% answered) while
    reporting a fake 92x ratio. The failure mode is a token win with a missing
    answer, so the threshold is deliberately conservative.
    """

    def _big(self, n):
        from sightline.index import Index

        files = {
            f"src/mod{i % 20}/file{i}.ts": {
                "hash": str(i), "language": "typescript", "loc": 100,
                "confidence": "likely", "note": "", "imports": [],
                "symbols": [{"name": f"fn{i}", "kind": "function", "line": 1,
                             "signature": f"export function fn{i}()", "exported": True,
                             "parent": None}],
            }
            for i in range(n)
        }
        return Index(
            root="/x", files=files,
            ranks={p: 1.0 / n for p in files},
            stats={"files": n, "loc": n * 100, "edges": 0},
        )

    def test_small_repo_uses_per_file(self):
        idx = self._big(50)
        out = render.render_map(idx, render.RenderOptions(budget=2000))
        assert "showing directories" not in out
        assert "file0.ts" in out

    def test_medium_repo_still_per_file(self):
        """1,213 files must not roll up: it answers 100% of the benchmark."""
        idx = self._big(1213)
        out = render.render_map(idx, render.RenderOptions(budget=2000))
        assert "showing directories" not in out, (
            "rollup at 1,213 files broke a 29-question benchmark from 100% to 72%"
        )

    def test_huge_repo_rolls_up(self):
        idx = self._big(7922)
        out = render.render_map(idx, render.RenderOptions(budget=2000))
        assert "showing directories" in out
        assert "files," in out  # aggregate counts present

    def test_rollup_respects_budget(self):
        idx = self._big(20000)
        out = render.render_map(idx, render.RenderOptions(budget=2000))
        assert len(out) / render.CHARS_PER_TOKEN <= 2000 * 1.02

    def test_force_files_overrides_rollup(self):
        idx = self._big(7922)
        out = render.render_map(idx, render.RenderOptions(budget=2000, force_files=True))
        assert "showing directories" not in out


class TestImportLanguageAliases:
    """The single largest bug the 118-repo corpus found.

    IMPORT_PATTERNS was keyed by exact language name, but `.tsx`, `.jsx`, `.vue`
    and `.svelte` are separate language names that share TypeScript import
    grammar. Only 'typescript' had patterns, so EVERY .tsx file returned
    imports: []. One repo had 1,988 .tsx files; fixing this took import edges
    from 1,585 to 2,965 and caller recall from 81% to 100%.
    """

    def test_tsx_extracts_imports(self):
        from sightline import extract

        src = 'import { db } from "@/lib/db/client";\nimport { users } from "./schema";'
        imports = extract._extract_imports(src, "tsx")
        assert "@/lib/db/client" in imports
        assert "./schema" in imports

    def test_jsx_extracts_imports(self):
        from sightline import extract

        assert extract._extract_imports('import a from "./a";', "jsx") == ["./a"]

    def test_vue_and_svelte_extract_imports(self):
        from sightline import extract

        for lang in ("vue", "svelte", "javascript"):
            got = extract._extract_imports('import x from "./x";', lang)
            assert "./x" in got, f"{lang} returned {got}"

    def test_all_ts_like_languages_have_patterns(self):
        from sightline import extract

        for lang in ("typescript", "tsx", "jsx", "javascript", "vue", "svelte"):
            key = extract.IMPORT_LANGUAGE_ALIASES.get(lang, lang)
            assert key in extract.IMPORT_PATTERNS, f"{lang} -> {key} has no patterns"


class TestJava:
    """Java was 2.4% parsed on apache/kafka (6,574 files) before this extractor:
    the largest single-language blind spot in the large-repo corpus."""

    def test_class_method_constructor_all_found(self):
        from sightline import extract

        src = """package x;
public class OrderService extends Base {
    public OrderService(int a) {}
    public void handle(String x) throws IOException {
        if (x != null) { }
    }
    private int count;
}
"""
        syms, conf, _ = extract.extract_java(src)
        names = {(s.name, s.kind) for s in syms}
        assert ("OrderService", "class") in names
        assert ("OrderService", "method") in names, "constructor missed"
        assert ("handle", "method") in names, "concrete method missed"
        assert conf == extract.Confidence.LIKELY

    def test_interface_methods_end_in_semicolon(self):
        from sightline import extract

        src = "interface Baz {\n  void run();\n  int count();\n}"
        syms, _, _ = extract.extract_java(src)
        names = {s.name for s in syms}
        assert "run" in names and "count" in names

    def test_control_flow_not_treated_as_methods(self):
        from sightline import extract

        src = """class A {
    public void real() {
        if (x) { }
        for (int i = 0; i < 2; i++) { }
    }
}
"""
        syms, _, _ = extract.extract_java(src)
        names = {s.name for s in syms}
        assert "real" in names
        assert not (names & {"if", "for", "while", "switch"})

    def test_private_marked_not_exported(self):
        from sightline import extract

        src = "class A {\n  private void secret() {\n  }\n}"
        syms, _, _ = extract.extract_java(src)
        secret = next(s for s in syms if s.name == "secret")
        assert secret.exported is False

    def test_empty_java_is_none(self):
        from sightline import extract

        _, conf, note = extract.extract_java("// nothing")
        assert conf == extract.Confidence.NONE and note


class TestArtifactDetection:
    """Found on mui/material-ui: 10,751 committed build files in
    packages/mui-icons-material/lib/ dropped its parsed rate to 54% and inflated
    the file count from 7k to 28k. The map spent its whole budget on transpiled
    duplicates of files that also exist as source."""

    def test_cjs_prologue_detected_as_generated(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.js"
            f.write_text('"use strict";\nObject.defineProperty(exports,"__esModule",{value:!0});')
            assert I._looks_generated(f) is True

    def test_sourcemap_comment_detected(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "y.js"
            f.write_text("var a=1;\n//# sourceMappingURL=y.js.map")
            assert I._looks_generated(f) is True

    def test_real_source_not_flagged(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "z.py"
            f.write_text("class Foo:\n    def bar(self): pass\n")
            assert I._looks_generated(f) is False

    def test_short_source_is_not_generated(self):
        """A 36-byte Python class is real source. An earlier size heuristic
        (< 60 bytes = generated) silently dropped every small module."""
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "tiny.py"
            f.write_text("x=1\n")
            assert I._looks_generated(f) is False

    def test_lib_is_skipped_for_js_but_not_python(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # JS-style lib/: transpiled output
            lib = root / "pkg" / "lib"
            lib.mkdir(parents=True)
            # >= 8 files: a real build tree, not a 2-file fixture. The rule now
            # requires size because eslint has tests/fixtures/.../cjs/ with one
            # hand-written file, and skipping by name alone dropped it.
            for i in range(10):
                (lib / f"m{i}.js").write_text("x")
            (root / "pkg" / "src").mkdir()
            (root / "pkg" / "src" / "a.ts").write_text("export const a = 1")
            assert I._should_skip_dir("lib", root / "pkg") is True

            # Python-style lib/: real source
            (root / "py" / "lib").mkdir(parents=True)
            (root / "py" / "lib" / "core.py").write_text("class A: pass")
            assert I._should_skip_dir("lib", root / "py") is False


class TestLibDiscriminator:
    """`lib/` is source in some projects and build output in others. Both appear
    in the large corpus:

      mui/material-ui  packages/mui-icons-material/lib/next to src/ -> output
      webpack/webpack  lib/ with no src/ sibling                  -> source

    Skipping unconditionally dropped webpack to 28% parsed.
    """

    def test_lib_with_sibling_src_is_output(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "pkg"
            (root / "src").mkdir(parents=True)
            (root / "src" / "a.ts").write_text("export const a = 1")
            (root / "lib").mkdir()
            for i in range(10):
                (root / "lib" / f"m{i}.js").write_text('"use strict";exports.a=1')
            assert I._should_skip_dir("lib", root) is True

    def test_lib_without_src_is_source(self):
        """webpack: lib/ IS the source. Keeping it matters."""
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "lib").mkdir()
            (root / "lib" / "WebpackError.js").write_text("class WebpackError extends Error {}")
            assert I._should_skip_dir("lib", root) is False

    def test_python_lib_with_src_still_kept(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "py"
            (root / "src").mkdir(parents=True)
            (root / "src" / "a.py").write_text("x = 1")
            (root / "lib").mkdir()
            (root / "lib" / "core.py").write_text("class A: pass")
            assert I._should_skip_dir("lib", root) is False

    def test_other_skip_dirs_unconditional(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            for name in ("node_modules", "dist", "target", "__pycache__"):
                assert I._should_skip_dir(name, Path(d)) is True


class TestQAHarness:
    """The benchmark harness had three bugs that produced plausible numbers while
    measuring nothing. These lock the fixes in."""

    def test_git_grep_l_output_has_no_colons(self):
        """`git grep -l` prints bare paths. A ':' in line filter silently dropped
        every result, so drill/outline questions were never generated."""
        sample = "src/a.rs\nsrc/b.rs\n"
        lines = [l for l in sample.splitlines() if ":" in l]
        assert lines == [], "confirms the bug shape"
        bare = [l for l in sample.splitlines() if l.strip()]
        assert len(bare) == 2

    def test_qa_uses_posix_grep_patterns(self):
        """git grep does not accept \\s or \\b; POSIX classes are required, and
        struct/enum/trait/func/fn must be in the keyword list or Rust and Go
        symbols never disambiguate."""
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "bench" / "qa.py").read_text()
        assert "[[:space:]]" in text, "POSIX character class missing"
        for kw in ("struct", "enum", "trait", "func", "fn"):
            assert kw in text, f"{kw} missing from the keyword list"


class TestUseStrictIsNotGenerated:
    """`"use strict";` was in ARTIFACT_MARKERS. eslint's hand-written `lib/`
    opens every file with it, so two-thirds of that repo was silently deleted
    (600 files indexed instead of 1,521) and its map accuracy fell to 4/10.

    A marker must be something only a transpiler emits.
    """

    def test_use_strict_source_is_not_generated(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "vfile.js"
            f.write_text('/** @fileoverview */\n"use strict";\n\nclass VFile {}\n')
            assert I._looks_generated(f) is False, "real CommonJS source deleted"

    def test_babel_interop_still_detected(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "out.js"
            f.write_text('"use strict";\nObject.defineProperty(exports,"__esModule",{value:!0});')
            assert I._looks_generated(f) is True

    def test_webpack_runtime_detected(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "bundle.js"
            f.write_text("(()=>{__webpack_require__(1);})()")
            assert I._looks_generated(f) is True


class TestSmallDirIsNotBuildOutput:
    """A build-output NAME is not evidence of a build tree.

    eslint has tests/fixtures/config-file/cjs/ containing one hand-written
    .eslintrc.cjs. Skipping `cjs/` by name deleted it. A directory with a handful
    of files is a fixture.
    """

    def test_two_file_cjs_dir_is_kept(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "a.ts").write_text("x")
            (root / "cjs").mkdir()
            (root / "cjs" / ".eslintrc.cjs").write_text("module.exports={};")
            assert I._should_skip_dir("cjs", root) is False

    def test_large_cjs_dir_is_build_output(self):
        import tempfile
        from pathlib import Path

        from sightline import index as I

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "a.ts").write_text("x")
            (root / "cjs").mkdir()
            for i in range(12):
                (root / "cjs" / f"m{i}.js").write_text("x")
            assert I._should_skip_dir("cjs", root) is True


class TestMCPServer:
    """The MCP server is the difference between "a tool on disk" and "a tool the
    agent has". These check the protocol contract without spawning a process;
    bench/mcp_probe.py does the subprocess version."""

    def test_tool_schemas_are_valid(self):
        from sightline import mcp_server as M

        for t in M.TOOLS:
            assert isinstance(t["inputSchema"], dict), t["name"]
            assert t["inputSchema"]["type"] == "object", t["name"]
            assert isinstance(t["description"], str) and t["description"], t["name"]
            assert len(t["name"]) <= 128
            # Agents choose tools from the description; a wall of text is ignored.
            assert len(t["description"]) < 700, t["name"]

    def test_tool_names_are_unique_and_prefixed(self):
        from sightline import mcp_server as M

        names = [t["name"] for t in M.TOOLS]
        assert len(names) == len(set(names))
        assert all(n.startswith("sightline_") for n in names)

    def test_every_tool_has_a_handler(self):
        from sightline import mcp_server as M

        for t in M.TOOLS:
            assert t["name"] in M.HANDLERS, f"{t['name']} has no handler"

    def test_no_parameters_tool_uses_valid_empty_schema(self):
        """MCP requires an object schema, never null."""
        from sightline import mcp_server as M

        for t in M.TOOLS:
            sch = t["inputSchema"]
            assert sch != {"type": "null"}
            if not sch.get("properties"):
                assert sch.get("additionalProperties") is False

    def test_required_fields_are_declared(self):
        from sightline import mcp_server as M

        for t in M.TOOLS:
            sch = t["inputSchema"]
            for req in sch.get("required", []):
                assert req in sch.get("properties", {}), f"{t['name']}.{req}"


class TestExplicitFocusScopes:
    """`--focus lib` at a small budget must show lib, not the whole repo.

    Before this, focus only mildly re-ranked: on a 2,264-file repo, four of five
    rollup lines were tests/ and docs/, which makes the flag look broken. An
    explicit focus is an instruction, not a hint.
    """

    def _idx(self):
        from sightline.index import Index

        def f(i, d):
            return {
                "hash": str(i), "size": 1000, "language": "typescript", "loc": 100,
                "confidence": "likely", "note": "", "imports": [],
                "symbols": [{"name": f"fn{i}", "kind": "function", "line": 1,
                             "signature": f"export function fn{i}()",
                             "exported": True, "parent": None}],
            }

        files = {}
        for i in range(40):
            files[f"lib/m{i}.ts"] = f(i, "lib")
        for i in range(40):
            files[f"tests/m{i}.ts"] = f(100 + i, "tests")
        return Index(
            root="/x", files=files,
            ranks={p: 1.0 / len(files) for p in files},
            stats={"files": len(files), "loc": len(files) * 100, "edges": 0},
        )

    def test_focus_excludes_other_trees_from_small_map(self):
        idx = self._idx()
        out = render.render_map(idx, render.RenderOptions(budget=1200, focus=["lib"]))
        assert "lib" in out
        # tests/ must not occupy the visible lines when budget is tight
        visible = [l for l in out.split("\n") if l.strip() and not l.startswith("#")]
        test_lines = [l for l in visible if "tests" in l]
        assert len(test_lines) <= 1, f"tests dominated a lib-focused map: {visible[:6]}"

    def test_no_focus_still_shows_everything(self):
        idx = self._idx()
        out = render.render_map(idx, render.RenderOptions(budget=4000))
        assert "lib" in out
        assert "tests" in out
