"""Tests for lint rules DOC001-DOC008, reporters, and mocked OpenAI suggester."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentdoc_lint.models import LintConfig, Severity
from agentdoc_lint.reporters import (
    render_github_annotations,
    render_json,
    render_markdown,
    render_sarif,
    render_terminal,
)
from agentdoc_lint.rules import lint_repository
from agentdoc_lint.suggester import suggest_fixes


class TestRulesAndReporters(unittest.TestCase):
    def _build_drifted_workspace(self, root: Path) -> None:
        (root / "src" / "acme").mkdir(parents=True)
        (root / "skills" / "broken").mkdir(parents=True)

        (root / "pyproject.toml").write_text(
            '[project]\nname = "acme"\nversion = "0.1.0"\n\n'
            '[project.scripts]\nacme-run = "acme.main:run"\n\n'
            "[tool.ruff]\nline-length = 100\n\n"
            '[tool.pytest.ini_options]\nmarkers = ["unit: fast tests"]\n',
            encoding="utf-8",
        )
        (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        (root / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
        (root / "package.json").write_text(
            json.dumps({"scripts": {"build": "vite build"}}),
            encoding="utf-8",
        )
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
        (root / ".env.example").write_text("ACME_API_URL=http://localhost\n", encoding="utf-8")
        (root / "src" / "acme" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "acme" / "main.py").write_text(
            "class Client:\n    def connect(self) -> bool:\n        return True\n\n"
            "def run() -> int:\n    return 0\n",
            encoding="utf-8",
        )

        (root / "AGENTS.md").write_text(
            "# AGENTS.md\n\n"
            "## Architecture\n"
            "- Entry point: `src/acme/app.py` and `acme.main.Client.old_connect()`.\n"
            "- Set `OBSOLETE_SECRET_KEY` before running.\n\n"
            "## Commands\n"
            "```bash\n"
            "make test-all\n"
            "pnpm run typecheck\n"
            "python -m acme.legacy\n"
            "uv run acme-old\n"
            "pytest -m e2e\n"
            "```\n"
            "```python\n"
            "from acme.main import MissingHelper\n"
            "```\n",
            encoding="utf-8",
        )
        (root / "CLAUDE.md").write_text(
            "# CLAUDE.md\n\nUse `poetry run pytest` and `npm run build`.\n",
            encoding="utf-8",
        )
        (root / "skills" / "broken" / "SKILL.md").write_text(
            "# Missing frontmatter skill\n",
            encoding="utf-8",
        )

    def test_all_drift_rules_fire_with_accurate_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_drifted_workspace(root)

            report = lint_repository(root, LintConfig(max_file_tokens=10, max_total_tokens=20))
            rule_ids = {d.rule_id for d in report.diagnostics}

            expected_rules = (
                "DOC001",
                "DOC002",
                "DOC003",
                "DOC004",
                "DOC005",
                "DOC006",
                "DOC007",
                "DOC008",
            )
            for expected in expected_rules:
                self.assertIn(expected, rule_ids, f"Expected rule {expected} to trigger")

            self.assertFalse(report.passes(Severity.ERROR))
            self.assertLess(report.health_score, 50)

            sym_diags = [d for d in report.diagnostics if d.rule_id == "DOC003"]
            self.assertTrue(any("Client.connect" in d.suggestion for d in sym_diags))

    def test_clean_repo_scores_100_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src" / "cleanpkg").mkdir(parents=True)
            (root / "skills" / "qa").mkdir(parents=True)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "cleanpkg"\nversion = "0.1.0"\n\n'
                "[tool.ruff]\nline-length = 100\n",
                encoding="utf-8",
            )
            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (root / "Makefile").write_text("test:\n\tpytest\n", encoding="utf-8")
            (root / ".env.example").write_text("CLEAN_API_KEY=demo\n", encoding="utf-8")
            (root / "src" / "cleanpkg" / "__init__.py").write_text(
                "from cleanpkg.runner import execute\n",
                encoding="utf-8",
            )
            (root / "src" / "cleanpkg" / "runner.py").write_text(
                "def execute() -> int:\n    return 0\n",
                encoding="utf-8",
            )
            (root / "AGENTS.md").write_text(
                "# AGENTS.md\n\n"
                "## Architecture\n"
                "- Core module: `src/cleanpkg/runner.py` "
                "(`cleanpkg.runner:execute`, `cleanpkg.execute`).\n"
                "- Uses `CLEAN_API_KEY`.\n\n"
                "## Verification\n"
                "```bash\n"
                "uv run pytest\n"
                "make test\n"
                "ruff check .\n"
                "```\n",
                encoding="utf-8",
            )
            (root / "skills" / "qa" / "SKILL.md").write_text(
                "---\nname: qa\ndescription: Quality assurance checks.\n---\n\nRun `make test`.\n",
                encoding="utf-8",
            )

            report = lint_repository(root)
            self.assertEqual(report.diagnostics, [])
            self.assertEqual(report.health_score, 100)
            self.assertTrue(report.passes(Severity.INFO))

    def test_reporters_and_sarif_schema_validity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_drifted_workspace(root)
            report = lint_repository(root)

            term = render_terminal(report, color=False)
            self.assertIn("agentdoc-lint Report", term)
            self.assertIn("DOC001", term)

            gh = render_github_annotations(report)
            self.assertIn("::error file=AGENTS.md,line=", gh)

            md = render_markdown(report)
            self.assertIn("## agentdoc-lint Drift Report", md)
            self.assertIn("`DOC001`", md)

            parsed_json = json.loads(render_json(report))
            self.assertGreater(parsed_json["summary"]["errors"], 0)

            sarif = json.loads(render_sarif(report))
            self.assertEqual(sarif["version"], "2.1.0")
            self.assertEqual(sarif["runs"][0]["columnKind"], "utf16CodeUnits")
            self.assertGreater(len(sarif["runs"][0]["results"]), 0)
            self.assertEqual(len(sarif["runs"][0]["tool"]["driver"]["rules"]), 8)

    def test_suggest_fixes_uses_mocked_transport_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_drifted_workspace(root)
            report = lint_repository(root)

            captured: dict[str, Any] = {}

            def fake_transport(
                url: str,
                headers: dict[str, str],
                payload: dict[str, Any],
                timeout: float,
            ) -> dict[str, Any]:
                captured["url"] = url
                captured["auth"] = headers["Authorization"]
                captured["model"] = payload["model"]
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "--- a/AGENTS.md\n+++ b/AGENTS.md\n"
                                    "@@ -4 +4 @@\n-src/acme/app.py\n+src/acme/main.py"
                                )
                            }
                        }
                    ]
                }

            diff_text = suggest_fixes(
                report,
                api_key="sk-test-123",
                model="gpt-5-mini",
                transport=fake_transport,
            )
            self.assertEqual(captured["url"], "https://api.openai.com/v1/chat/completions")
            self.assertEqual(captured["auth"], "Bearer sk-test-123")
            self.assertIn("+++ b/AGENTS.md", diff_text)


if __name__ == "__main__":
    unittest.main()
