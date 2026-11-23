"""Command-line interface for agentdoc-lint."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from agentdoc_lint import __version__
from agentdoc_lint.facts import collect_repo_facts
from agentdoc_lint.models import LintConfig, Severity
from agentdoc_lint.reporters import (
    render_github_annotations,
    render_json,
    render_markdown,
    render_sarif,
    render_terminal,
    supports_color,
)
from agentdoc_lint.rules import lint_repository
from agentdoc_lint.suggester import DEFAULT_MODEL, suggest_fixes
from agentdoc_lint.tokenizer import TokenCounter, get_token_counter


def _resolve_counter(mode: str) -> TokenCounter:
    if mode == "heuristic":
        return get_token_counter(prefer_tiktoken=False)
    if mode == "tiktoken":
        from agentdoc_lint.tokenizer import TiktokenCounter

        return TiktokenCounter()
    return get_token_counter(prefer_tiktoken=True)


def _safe_write_stream(stream: TextIO, text: str, *, prefer_utf8_buffer: bool = False) -> None:
    """Write text to a stream without crashing on narrow Windows console encodings (e.g. cp1252)."""
    payload = text if text.endswith("\n") else text + "\n"
    if prefer_utf8_buffer:
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            buf.write(payload.encode("utf-8"))
            buf.flush()
            return
    try:
        stream.write(payload)
        stream.flush()
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "utf-8"
        safe = payload.encode(enc, errors="replace").decode(enc, errors="replace")
        stream.write(safe)
        stream.flush()


def _write_output(
    text: str,
    output_path: str | None,
    *,
    machine_format: bool = False,
) -> None:
    payload = text if text.endswith("\n") else text + "\n"
    if output_path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    else:
        _safe_write_stream(sys.stdout, payload, prefer_utf8_buffer=machine_format)


def _create_demo_repo(base: Path) -> None:
    """Create a realistic miniature drifted repository inside a temporary directory."""
    (base / "src" / "demo_app").mkdir(parents=True)
    (base / "skills" / "release").mkdir(parents=True)

    (base / "pyproject.toml").write_text(
        '[project]\nname = "demo-app"\nversion = "0.1.0"\n\n'
        '[project.scripts]\ndemo-cli = "demo_app.cli:main"\n\n'
        "[tool.ruff]\nline-length = 100\n",
        encoding="utf-8",
    )
    (base / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (base / "Makefile").write_text(
        "test:\n\tpytest\n\nlint:\n\truff check .\n",
        encoding="utf-8",
    )
    (base / ".env.example").write_text("APP_DATABASE_URL=sqlite:///dev.db\n", encoding="utf-8")
    (base / "src" / "demo_app" / "__init__.py").write_text("", encoding="utf-8")
    (base / "src" / "demo_app" / "authentication.py").write_text(
        "import os\n\nclass AuthService:\n    def verify_token(self, token: str) -> bool:\n"
        '        return bool(token and os.getenv("APP_DATABASE_URL"))\n',
        encoding="utf-8",
    )
    (base / "src" / "demo_app" / "cli.py").write_text(
        "def main() -> int:\n    return 0\n",
        encoding="utf-8",
    )

    (base / "AGENTS.md").write_text(
        "# AGENTS.md\n\n"
        "## Architecture\n"
        "- Auth logic lives in `src/demo_app/auth.py` via "
        "`demo_app.authentication.AuthService.verify_jwt()`.\n"
        "- Configure `LEGACY_AUTH_SECRET` before running integration checks.\n\n"
        "## Build & Test\n"
        "```bash\n"
        "uv run demo-cli\n"
        "make test-all\n"
        "```\n",
        encoding="utf-8",
    )
    (base / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n"
        "Always use `poetry run pytest` and format with `black .`.\n",
        encoding="utf-8",
    )
    (base / "skills" / "release" / "SKILL.md").write_text(
        "# Release Checklist\n\nRun `make lint` before tagging.\n",
        encoding="utf-8",
    )


def _cmd_check(args: argparse.Namespace) -> int:
    ignore_rules = {r.strip().upper() for r in args.ignore_rule if r.strip()}
    ignore_dirs = {d.strip() for d in args.ignore_dir if d.strip()}
    cfg = LintConfig(
        max_file_tokens=args.max_file_tokens,
        max_total_tokens=args.max_total_tokens,
        ignore_rules=ignore_rules,
        ignore_dirs=ignore_dirs,
        check_env_vars=not args.no_env_check,
    )
    counter = _resolve_counter(args.tokenizer)
    report = lint_repository(args.path, cfg, counter=counter)

    machine_fmt = args.format in ("json", "sarif")
    if args.format == "json":
        rendered = render_json(report)
    elif args.format == "markdown":
        rendered = render_markdown(report)
    elif args.format == "github":
        rendered = render_github_annotations(report)
    elif args.format == "sarif":
        rendered = render_sarif(report, base_dir=args.base_dir)
    else:
        rendered = render_terminal(report, color=(supports_color() and not args.output))

    _write_output(rendered, args.output, machine_format=machine_fmt)

    if report.skipped_files:
        joined = ", ".join(report.skipped_files[:5])
        _safe_write_stream(
            sys.stderr,
            f"agentdoc-lint warning: skipped {len(report.skipped_files)} Python file(s) "
            f"during AST indexing: {joined}",
        )

    if args.suggest_fix and report.diagnostics:
        patch = suggest_fixes(report, model=args.model)
        if args.fix_output:
            _write_output(patch, args.fix_output)
        elif machine_fmt and not args.output:
            # Keep machine-readable stdout strictly valid JSON/SARIF
            _safe_write_stream(
                sys.stderr,
                f"\n--- Suggested Unified Diff (OpenAI API) ---\n{patch}",
            )
        else:
            _safe_write_stream(
                sys.stdout,
                f"\n--- Suggested Unified Diff (OpenAI API) ---\n{patch}",
            )

    if args.fail_on == "none":
        return 0
    return 0 if report.passes(Severity(args.fail_on)) else 1


def _cmd_facts(args: argparse.Namespace) -> int:
    ignore_dirs = {d.strip() for d in args.ignore_dir if d.strip()}
    facts = collect_repo_facts(args.path, extra_ignore_dirs=ignore_dirs)
    _write_output(
        json.dumps(facts.to_summary_dict(), indent=2, ensure_ascii=False),
        args.output,
        machine_format=True,
    )
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="agentdoc-lint-demo-") as tmp:
        demo_root = Path(tmp)
        _create_demo_repo(demo_root)
        report = lint_repository(demo_root, counter=_resolve_counter(args.tokenizer))
        machine_fmt = args.format in ("json", "sarif")
        if args.format == "markdown":
            rendered = render_markdown(report)
        elif args.format == "json":
            rendered = render_json(report)
        elif args.format == "github":
            rendered = render_github_annotations(report)
        elif args.format == "sarif":
            rendered = render_sarif(report)
        else:
            rendered = render_terminal(report, color=supports_color())
        _write_output(rendered, None, machine_format=machine_fmt)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentdoc-lint",
        description=(
            "Verify AGENTS.md, CLAUDE.md, SKILL.md, and .cursor/rules against real codebase facts."
        ),
    )
    parser.add_argument("--version", action="version", version=f"agentdoc-lint {__version__}")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # check
    p_check = sub.add_parser("check", help="Audit a repository's coding-agent instruction files.")
    p_check.add_argument("path", nargs="?", default=".", help="Repository root path (default: .).")
    p_check.add_argument(
        "--format",
        choices=("terminal", "github", "markdown", "json", "sarif"),
        default="terminal",
    )
    p_check.add_argument(
        "--fail-on",
        choices=("none", "info", "warning", "error"),
        default="error",
        help="Minimum diagnostic severity that exits non-zero (default: error).",
    )
    p_check.add_argument("--max-file-tokens", type=int, default=3000)
    p_check.add_argument("--max-total-tokens", type=int, default=8000)
    p_check.add_argument(
        "--ignore-rule",
        action="append",
        default=[],
        help="Rule ID to ignore (e.g. --ignore-rule DOC004). Repeatable.",
    )
    p_check.add_argument(
        "--ignore-dir",
        action="append",
        default=[],
        help="Directory name to exclude from scanning. Repeatable.",
    )
    p_check.add_argument(
        "--no-env-check",
        action="store_true",
        help="Disable DOC004 environment variable verification.",
    )
    p_check.add_argument(
        "--tokenizer",
        choices=("auto", "heuristic", "tiktoken"),
        default="auto",
    )
    p_check.add_argument(
        "--base-dir",
        default=None,
        help="Optional repository root used to prefix relative URIs in SARIF output.",
    )
    p_check.add_argument(
        "--suggest-fix",
        action="store_true",
        help="Use OPENAI_API_KEY to propose a unified diff fixing detected drift.",
    )
    p_check.add_argument(
        "--fix-output",
        default=None,
        help="Write the --suggest-fix unified diff to a separate file.",
    )
    p_check.add_argument("--model", default=DEFAULT_MODEL, help="Model for --suggest-fix.")
    p_check.add_argument("-o", "--output", help="Write report to a file instead of stdout.")

    # facts
    p_facts = sub.add_parser("facts", help="Print statically extracted repository facts as JSON.")
    p_facts.add_argument("path", nargs="?", default=".", help="Repository root path (default: .).")
    p_facts.add_argument("--ignore-dir", action="append", default=[])
    p_facts.add_argument("-o", "--output", help="Write JSON facts to a file.")

    # demo
    p_demo = sub.add_parser("demo", help="Run a self-contained demo showcasing DOC001-DOC007.")
    p_demo.add_argument(
        "--format",
        choices=("terminal", "github", "markdown", "json", "sarif"),
        default="terminal",
    )
    p_demo.add_argument(
        "--tokenizer",
        choices=("auto", "heuristic", "tiktoken"),
        default="auto",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.subcommand == "check":
            return _cmd_check(args)
        if args.subcommand == "facts":
            return _cmd_facts(args)
        if args.subcommand == "demo":
            return _cmd_demo(args)
        parser.print_help()
        return 2
    except Exception as exc:
        _safe_write_stream(sys.stderr, f"agentdoc-lint error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
