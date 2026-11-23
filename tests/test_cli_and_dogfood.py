"""CLI integration tests and self-dogfood check on agentdoc-lint itself."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from agentdoc_lint.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestCliAndDogfood(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_demo_command_succeeds(self) -> None:
        code, stdout, stderr = self._run_cli(["demo"])
        self.assertEqual(code, 0, msg=stderr)
        self.assertIn("DOC001", stdout)
        self.assertIn("DOC002", stdout)
        self.assertIn("DOC003", stdout)
        self.assertIn("DOC007", stdout)

    def test_facts_command_outputs_valid_json(self) -> None:
        code, stdout, stderr = self._run_cli(["facts", str(REPO_ROOT), "--ignore-dir", "examples"])
        self.assertEqual(code, 0, msg=stderr)
        data = json.loads(stdout)
        self.assertIn("agentdoc_lint", data["python_root_packages"])
        self.assertIn("agentdoc-lint", data["pyproject_scripts"])
        self.assertIn("OPENAI_API_KEY", data["env_vars"])

    def test_drifted_example_fails_error_gate(self) -> None:
        example_dir = REPO_ROOT / "examples" / "drifted-repo"
        code, stdout, _ = self._run_cli(["check", str(example_dir), "--fail-on", "error"])
        self.assertEqual(code, 1)
        self.assertIn("DOC001", stdout)

    def test_dogfood_self_check_passes_cleanly(self) -> None:
        code, stdout, stderr = self._run_cli(
            [
                "check",
                str(REPO_ROOT),
                "--ignore-dir",
                "examples",
                "--fail-on",
                "warning",
            ]
        )
        self.assertEqual(code, 0, msg=f"Self-check failed!\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
        self.assertIn("Health Score: 100/100", stdout)


if __name__ == "__main__":
    unittest.main()
