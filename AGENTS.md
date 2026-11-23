# AGENTS.md

Instructions for AI coding agents (Codex, Claude Code, Cursor) contributing to `agentdoc-lint`.

## Architecture Overview

- `src/agentdoc_lint/models.py` — Strongly typed dataclasses (`RepoFacts`, `InstructionDocument`, `Diagnostic`, `LintReport`).
- `src/agentdoc_lint/tokenizer.py` — Pluggable token counting (`tiktoken` optional extra + zero-dependency fallback).
- `src/agentdoc_lint/facts.py` — Static repository fact extractor (`agentdoc_lint.facts:collect_repo_facts`) covering filesystem, Python AST, `pyproject.toml`, `Makefile`, `package.json`, lockfiles, and env templates.
- `src/agentdoc_lint/parser.py` — Agent instruction discovery and line-accurate Markdown claim extractor (`agentdoc_lint.parser:parse_instruction_file`).
- `src/agentdoc_lint/rules.py` — Drift rules `DOC001`–`DOC008` and health scoring (`agentdoc_lint.rules:lint_repository`).
- `src/agentdoc_lint/suggester.py` — Opt-in OpenAI API diff generator (`OPENAI_API_KEY`, pure `urllib.request`).
- `src/agentdoc_lint/reporters.py` — Terminal, GitHub PR annotation, Markdown, JSON, and SARIF 2.1.0 formatters.
- `src/agentdoc_lint/cli.py` — CLI entry point (`agentdoc_lint.cli:main`).

## Build & Verification Commands

Run the zero-dependency test suite and self-check from the repository root:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m agentdoc_lint check . --ignore-dir examples --fail-on warning
PYTHONPATH=src python -m agentdoc_lint demo
```

When optional dev tools are installed:

```bash
ruff check src tests
mypy src
```

## Coding Constraints

1. **Zero required runtime dependencies**: Keep `[project.dependencies]` empty in `pyproject.toml`.
2. **Strictly static analysis**: Never execute shell commands or Python modules parsed from target repositories.
3. **Low false positives**: Add negative (false-positive) test cases in `tests/` whenever adding or modifying regex extractors.
4. **Strict typing**: Start every module with `from __future__ import annotations` and keep `mypy` strict checks green.
