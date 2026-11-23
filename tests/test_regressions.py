"""Regression tests covering security, cross-platform, Monorepo, encoding, and CLI fixes."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agentdoc_lint.cli import _safe_write_stream, main
from agentdoc_lint.facts import (
    _minimal_toml_fallback,
    collect_repo_facts,
    is_safe_repo_file,
    is_within_root,
)
from agentdoc_lint.parser import (
    _extract_markdown_links,
    discover_instruction_files,
    parse_instruction_file,
)
from agentdoc_lint.reporters import render_github_annotations, render_sarif
from agentdoc_lint.rules import lint_repository


class TestRegressions(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_p0_1_blocks_junction_escape_cycles_and_secret_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            outside = base / "outside"
            repo.mkdir()
            outside.mkdir()

            (outside / "secret_mod.py").write_text("def leaked() -> None:\n    pass\n", "utf-8")
            (outside / "AGENTS.md").write_text("# Outside\n", "utf-8")
            (repo / ".env").write_text("SUPER_SECRET=1\n", "utf-8")
            (repo / "AGENTS.md").write_text(
                "# AGENTS.md\n\n## Architecture\n- Root repo.\n\n## Build\n- None.\n",
                "utf-8",
            )

            escape_link = repo / "ext_link"
            cycle_link = repo / "loop_link"
            created_links = False
            if os.name == "nt":
                try:
                    import _winapi

                    _winapi.CreateJunction(str(outside), str(escape_link))
                    _winapi.CreateJunction(str(repo), str(cycle_link))
                    created_links = True
                except OSError:
                    created_links = False
            else:
                os.symlink(outside, escape_link)
                os.symlink(repo, cycle_link)
                created_links = True

            # Always verify out-of-root and secret `.env` guards directly
            self.assertFalse(is_within_root(outside / "secret_mod.py", repo.resolve()))
            self.assertFalse(is_safe_repo_file(repo / ".env", repo.resolve()))
            with self.assertRaises(ValueError):
                parse_instruction_file(repo, outside / "AGENTS.md")

            facts = collect_repo_facts(repo)
            self.assertNotIn("secret_mod", facts.python_modules)
            self.assertNotIn(".env", facts.all_files)
            self.assertNotIn("SUPER_SECRET", facts.env_vars)

            docs = [p.relative_to(facts.root).as_posix() for p in discover_instruction_files(repo)]
            self.assertEqual(docs, ["AGENTS.md"])

            report = lint_repository(repo)
            self.assertEqual(len(report.documents), 1)
            if created_links:
                self.assertNotIn("ext_link", facts.all_dirs)
                self.assertNotIn("loop_link", facts.all_dirs)
            else:
                # Verify directory-pruning branch even when OS sandbox blocks link creation
                fake_link_dir = repo / "linked_sub"
                fake_link_dir.mkdir()
                (fake_link_dir / "hidden.py").write_text("X = 1\n", encoding="utf-8")
                with mock.patch(
                    "agentdoc_lint.facts.is_link_or_junction",
                    side_effect=lambda p: Path(p).name == "linked_sub",
                ):
                    pruned = collect_repo_facts(repo)
                self.assertNotIn("linked_sub", pruned.all_dirs)
                self.assertNotIn("linked_sub.hidden", pruned.python_modules)

    def test_p0_2_missing_or_file_root_returns_exit_code_2_while_empty_dir_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            missing = base / "does-not-exist"
            plain_file = base / "file.txt"
            plain_file.write_text("hi", encoding="utf-8")
            empty_dir = base / "empty"
            empty_dir.mkdir()

            code_missing, _, err_missing = self._run_cli(["check", str(missing)])
            self.assertEqual(code_missing, 2)
            self.assertIn("does not exist", err_missing)

            code_facts, _, _ = self._run_cli(["facts", str(missing)])
            self.assertEqual(code_facts, 2)

            code_file, _, err_file = self._run_cli(["check", str(plain_file)])
            self.assertEqual(code_file, 2)
            self.assertIn("not a directory", err_file)

            code_empty, _, _ = self._run_cli(["check", str(empty_dir)])
            self.assertEqual(code_empty, 0)

    def test_p0_3_markdown_link_scanner_is_linear_and_accurate(self) -> None:
        evil = "[" * 20_000
        start = time.perf_counter()
        links = _extract_markdown_links(evil)
        elapsed = time.perf_counter() - start
        self.assertEqual(links, [])
        self.assertLess(elapsed, 0.25)

        sample = (
            'See [guide](docs/guide.md "title"), ![img](assets/pic.png), '
            "`[ignored](fake.md)`, and [wrapped](<docs/with space.md>)."
        )
        self.assertEqual(
            _extract_markdown_links(sample),
            ["docs/guide.md", "assets/pic.png", "docs/with space.md"],
        )

    def test_p0_4_monorepo_subproject_uses_nearest_package_json_and_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "apps" / "web"
            web.mkdir(parents=True)

            (root / "package.json").write_text(
                json.dumps({"scripts": {"lint": "eslint ."}}),
                encoding="utf-8",
            )
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "# Root\n\n## Architecture\n- Monorepo.\n\n## Build\n"
                "```bash\nnpm run lint\n```\n",
                encoding="utf-8",
            )

            (web / "package.json").write_text(
                json.dumps({"scripts": {"build": "vite build"}}),
                encoding="utf-8",
            )
            (web / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (web / "AGENTS.md").write_text(
                "# Web\n\n```bash\npnpm run build\npnpm run lint\n```\n",
                encoding="utf-8",
            )

            report = lint_repository(root)
            doc2_msgs = [d.message for d in report.diagnostics if d.rule_id == "DOC002"]
            doc5_diags = [d for d in report.diagnostics if d.rule_id == "DOC005"]

            # `build` exists in apps/web/package.json -> must NOT report DOC002
            self.assertFalse(any("`build`" in m for m in doc2_msgs))
            # `lint` only exists at root, NOT in apps/web/package.json -> MUST report DOC002
            self.assertTrue(any("`lint`" in m for m in doc2_msgs))
            # apps/web has pnpm-lock.yaml, root has package-lock.json -> NO DOC005 conflict
            self.assertEqual(doc5_diags, [])

    def test_p0_5_windows_paths_and_relative_link_no_double_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "ok.py").write_text("x = 1\n", encoding="utf-8")
            (root / "docs" / "docs").mkdir(parents=True)
            (root / "docs" / "docs" / "guide.md").write_text("# Nested\n", encoding="utf-8")

            (root / "AGENTS.md").write_text(
                "# AGENTS.md\n\n## Architecture\n"
                "- Good mixed path: `src\\extra/../ok.py`\n"
                "- Bad windows path: `src\\missing.py`\n\n## Build\n- None.\n",
                encoding="utf-8",
            )
            (root / "docs" / "AGENTS.md").write_text(
                "# Docs\n\nRead [guide](guide.md).\n",
                encoding="utf-8",
            )

            report = lint_repository(root)
            broken = {(d.file, d.snippet) for d in report.diagnostics if d.rule_id == "DOC001"}
            self.assertIn(("AGENTS.md", "src\\missing.py"), broken)
            self.assertIn(("docs/AGENTS.md", "guide.md"), broken)
            self.assertNotIn(("AGENTS.md", "src\\extra/../ok.py"), broken)

    def test_p0_6_make_multi_target_and_inline_commands_with_negation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("test lint:\n\techo ok\n", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "# AGENTS.md\n\n## Architecture\n- Make demo.\n\n## Build\n"
                "Run `make lint` before PRs.\n"
                "Also run `make stale` for old checks.\n"
                "Do not run `make deprecated` anymore.\n",
                encoding="utf-8",
            )

            report = lint_repository(root)
            doc2 = [d.message for d in report.diagnostics if d.rule_id == "DOC002"]
            self.assertFalse(any("`lint`" in m for m in doc2))
            self.assertTrue(any("`stale`" in m for m in doc2))
            self.assertFalse(any("`deprecated`" in m for m in doc2))

    def test_p0_7_deep_ast_recursion_error_does_not_abort_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep_expr = " + ".join(["1"] * 4000)
            (root / "long.py").write_text(f"X = {deep_expr}\n", encoding="utf-8")
            (root / "ok.py").write_text("def alive() -> int:\n    return 1\n", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "# AGENTS.md\n\n## Architecture\n- Uses `ok.alive()`.\n\n## Build\n- None.\n",
                encoding="utf-8",
            )

            code, stdout, _stderr = self._run_cli(["check", str(root)])
            self.assertEqual(code, 0)
            self.assertIn("Health Score: 100/100", stdout)

            facts = collect_repo_facts(root)
            self.assertIn("ok", facts.python_modules)
            if facts.skipped_python_files:
                self.assertTrue(any("long.py" in entry for entry in facts.skipped_python_files))

    def test_p1_1_toml_fallback_parses_inline_tables_and_arrays(self) -> None:
        sample = (
            "[project]\n"
            'name = "demo"\n'
            'scripts = { mycli = "pkg.mod:main", other = "pkg.mod:other" }\n\n'
            "[tool.pytest.ini_options]\n"
            "markers = [\n"
            '  "slow: slow integration tests",\n'
            '  "unit: fast unit tests",\n'
            "]\n"
        )
        parsed = _minimal_toml_fallback(sample)
        self.assertEqual(parsed["project"]["scripts"]["mycli"], "pkg.mod:main")
        self.assertEqual(
            parsed["tool"]["pytest"]["ini_options"]["markers"],
            ["slow: slow integration tests", "unit: fast unit tests"],
        )

    def test_p1_3_python_encoding_cookie_and_utf8_bom_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills" / "bom").mkdir(parents=True)
            latin1_src = "# -*- coding: latin-1 -*-\ndef caf\xe9() -> int:\n    return 1\n"
            (root / "mod.py").write_bytes(latin1_src.encode("latin-1"))

            (root / "AGENTS.md").write_bytes(
                (
                    "# AGENTS.md\n\n## Architecture\n- Uses `mod.caf\xe9()`.\n\n"
                    "## Build\n- None.\n"
                ).encode("utf-8-sig")
            )
            (root / "skills" / "bom" / "SKILL.md").write_bytes(
                "---\nname: bom-skill\ndescription: Handles BOM cleanly.\n---\n\nHi.\n".encode(
                    "utf-8-sig"
                )
            )

            report = lint_repository(root)
            self.assertEqual(report.diagnostics, [])

    def test_p1_4_top_level_single_segment_module_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "entry.py").write_text("def run() -> int:\n    return 0\n", encoding="utf-8")
            (root / "AGENTS.md").write_text(
                "# AGENTS.md\n\n## Architecture\n"
                "- Valid: `entry.run()` and `entry:run`\n"
                "- Broken: `entry.missing()` and `entry:gone`\n\n## Build\n- None.\n",
                encoding="utf-8",
            )

            report = lint_repository(root)
            doc3 = [d.snippet for d in report.diagnostics if d.rule_id == "DOC003"]
            self.assertIn("entry.missing()", doc3)
            self.assertIn("entry:gone", doc3)
            self.assertNotIn("entry.run()", doc3)
            self.assertNotIn("entry:run", doc3)

    def test_p1_5_and_p1_6_github_escaping_and_sarif_uri_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "apps" / "web"
            tricky_dir = sub / "docs, #1"
            tricky_dir.mkdir(parents=True)
            (tricky_dir / "AGENTS.md").write_text(
                "# Tricky\n\nMissing `src/missing.py`.\n",
                encoding="utf-8",
            )

            report = lint_repository(sub)
            gh = render_github_annotations(report)
            self.assertIn("file=docs%2C #1/AGENTS.md", gh)

            sarif = json.loads(render_sarif(report, base_dir=root))
            uri = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
                "artifactLocation"
            ]["uri"]
            self.assertEqual(uri, "apps/web/docs%2C%20%231/AGENTS.md")
            self.assertEqual(sarif["runs"][0]["columnKind"], "utf16CodeUnits")

    def test_p1_7_cp1252_stream_safe_write_does_not_crash(self) -> None:
        raw_buf = io.BytesIO()
        cp1252_stream = io.TextIOWrapper(raw_buf, encoding="cp1252")
        _safe_write_stream(cp1252_stream, "Path: \u6f22\u5b57/AGENTS.md", prefer_utf8_buffer=False)
        self.assertIn("AGENTS.md", raw_buf.getvalue().decode("cp1252"))

        raw_json_buf = io.BytesIO()
        cp1252_json_stream = io.TextIOWrapper(raw_json_buf, encoding="cp1252")
        _safe_write_stream(
            cp1252_json_stream,
            '{"file":"\u6f22\u5b57/AGENTS.md"}',
            prefer_utf8_buffer=True,
        )
        parsed = json.loads(raw_json_buf.getvalue().decode("utf-8"))
        self.assertEqual(parsed["file"], "\u6f22\u5b57/AGENTS.md")

    def test_p2_3_suggest_fix_keeps_json_stdout_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text(
                "# AGENTS.md\n\n## Architecture\n- `src/nope.py`\n\n## Build\n- None.\n",
                encoding="utf-8",
            )
            fix_file = root / "suggested.patch"

            with mock.patch(
                "agentdoc_lint.cli.suggest_fixes",
                return_value="--- a/AGENTS.md\n+++ b/AGENTS.md\n",
            ):
                code, stdout, stderr = self._run_cli(
                    ["check", str(root), "--format", "json", "--suggest-fix", "--fail-on", "none"]
                )
                self.assertEqual(code, 0)
                parsed = json.loads(stdout)
                self.assertEqual(parsed["summary"]["errors"], 1)
                self.assertIn("+++ b/AGENTS.md", stderr)

                code2, stdout2, _ = self._run_cli(
                    [
                        "check",
                        str(root),
                        "--format",
                        "json",
                        "--suggest-fix",
                        "--fix-output",
                        str(fix_file),
                        "--fail-on",
                        "none",
                    ]
                )
                self.assertEqual(code2, 0)
                json.loads(stdout2)
                self.assertIn("+++ b/AGENTS.md", fix_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
