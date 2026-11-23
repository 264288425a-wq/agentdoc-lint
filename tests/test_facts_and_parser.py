"""Tests for static repository fact extraction and instruction document parsing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentdoc_lint.facts import collect_repo_facts
from agentdoc_lint.parser import discover_instruction_files, parse_instruction_file


class TestFactsAndParser(unittest.TestCase):
    def test_collect_repo_facts_extracts_python_ast_manifests_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "my_pkg").mkdir(parents=True)
            (root / ".github" / "workflows").mkdir(parents=True)

            (root / "pyproject.toml").write_text(
                '[project]\nname = "my-pkg"\nversion = "0.1.0"\n\n'
                '[project.scripts]\nmy-cli = "my_pkg.cli:main"\n\n'
                "[tool.ruff]\nline-length = 100\n\n"
                '[tool.pytest.ini_options]\nmarkers = ["slow: slow integration tests"]\n',
                encoding="utf-8",
            )
            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (root / "Makefile").write_text(
                "test:\n\tpytest\n\nbuild:\n\tpython -m build\n",
                encoding="utf-8",
            )
            (root / "package.json").write_text(
                json.dumps({"scripts": {"lint": "eslint .", "test": "vitest"}}),
                encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (root / ".env.example").write_text("APP_TOKEN=abc\n", encoding="utf-8")
            (root / ".github" / "workflows" / "ci.yml").write_text(
                "name: CI\nenv:\n  CI_CUSTOM_FLAG: '1'\n",
                encoding="utf-8",
            )
            (root / "src" / "my_pkg" / "__init__.py").write_text(
                "from my_pkg.core import Engine\n",
                encoding="utf-8",
            )
            (root / "src" / "my_pkg" / "core.py").write_text(
                "import os\n\nclass Engine:\n    def run(self) -> None:\n"
                '        _ = os.getenv("RUNTIME_API_URL")\n'
                '        _ = os.environ["REQUIRED_KEY"]\n',
                encoding="utf-8",
            )

            facts = collect_repo_facts(root)

            self.assertIn("my_pkg", facts.python_root_packages)
            self.assertIn("my_pkg.core", facts.python_modules)
            self.assertIn("Engine", facts.module_symbols["my_pkg"])
            self.assertIn("Engine.run", facts.module_symbols["my_pkg.core"])
            self.assertIn("my-cli", facts.pyproject_scripts)
            self.assertIn("slow", facts.pytest_markers)
            self.assertEqual(facts.makefile_targets, {"test", "build"})
            self.assertEqual(facts.package_scripts, {"lint", "test"})
            expected_env = {"APP_TOKEN", "RUNTIME_API_URL", "REQUIRED_KEY", "CI_CUSTOM_FLAG"}
            self.assertTrue(expected_env.issubset(facts.env_vars))
            self.assertEqual(facts.detected_toolchains["python_pm"], {"uv"})
            self.assertEqual(facts.detected_toolchains["js_pm"], {"pnpm"})
            self.assertEqual(facts.detected_toolchains["python_linter"], {"ruff"})

    def test_parser_discovers_instruction_files_and_parses_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".cursor" / "rules").mkdir(parents=True)
            (root / ".agents" / "skills" / "testing").mkdir(parents=True)
            (root / ".github").mkdir(parents=True)

            (root / "AGENTS.md").write_text("# AGENTS\n", encoding="utf-8")
            (root / ".cursor" / "rules" / "style.mdc").write_text(
                "---\ndescription: Style\n---\n",
                encoding="utf-8",
            )
            (root / ".github" / "copilot-instructions.md").write_text(
                "# Copilot\n",
                encoding="utf-8",
            )
            skill = root / ".agents" / "skills" / "testing" / "SKILL.md"
            skill.write_text(
                "---\nname: testing\ndescription: Run deterministic unit tests.\n---\n\n"
                "Check `src/app.py` and run:\n```bash\nmake test\n```\n",
                encoding="utf-8",
            )

            discovered = [p.relative_to(root).as_posix() for p in discover_instruction_files(root)]
            self.assertEqual(
                discovered,
                [
                    ".agents/skills/testing/SKILL.md",
                    ".cursor/rules/style.mdc",
                    ".github/copilot-instructions.md",
                    "AGENTS.md",
                ],
            )

            doc = parse_instruction_file(root, skill)
            self.assertEqual(doc.kind, "skill")
            self.assertTrue(doc.has_frontmatter)
            self.assertIsNone(doc.frontmatter_error)
            self.assertEqual(doc.frontmatter["name"], "testing")
            self.assertEqual(doc.path_claims[0].normalized, "src/app.py")
            self.assertEqual(doc.command_claims[0].kind, "make")
            self.assertEqual(doc.command_claims[0].target, "test")

    def test_parser_ignores_negated_toolchain_and_non_path_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / "AGENTS.md"
            agents.write_text(
                "# AGENTS.md\n\n"
                "Always use `uv` instead of `poetry` and never use `black`.\n"
                "Return `application/json` for `sync/async` calls and skip "
                "`v0.1.0` or `<your-file>`.\n",
                encoding="utf-8",
            )
            doc = parse_instruction_file(root, agents)
            claimed_tools = {c.tool for c in doc.toolchain_claims}
            self.assertEqual(claimed_tools, {"uv"})
            self.assertEqual(doc.path_claims, [])


if __name__ == "__main__":
    unittest.main()
