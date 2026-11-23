# agentdoc-lint

**Stop instruction drift in `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, and `.cursor/rules`.**  
A zero-dependency Python CLI, `pre-commit` hook, and GitHub Action that fact-checks AI coding-agent instructions directly against your repository's real files, Python AST, and build manifests.

[![CI](https://github.com/264288425a-wq/agentdoc-lint/actions/workflows/ci.yml/badge.svg)](https://github.com/264288425a-wq/agentdoc-lint/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/agentdoc-lint.svg)](https://pypi.org/project/agentdoc-lint/)
[![PyPI downloads](https://img.shields.io/pypi/dm/agentdoc-lint.svg)](https://pypi.org/project/agentdoc-lint/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## Why `agentdoc-lint`?

Teams write `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, and `.cursor/rules` to guide coding agents like **Codex**, **Claude Code**, and **Cursor**. Two months of refactoring later, those instruction files silently rot:

- Paths like `src/core/auth.py` were renamed to `src/core/authentication.py`
- `make test-unit` or `npm run check:types` was deleted from `Makefile` / `package.json`
- Python symbols like `AuthService.verify_jwt` no longer exist in the codebase
- `AGENTS.md` says "use `uv` and `ruff`" while `CLAUDE.md` still says "use `poetry` and `black`"
- Instruction files quietly balloon to 6,000+ tokens, hurting instruction adherence

Stale instructions cause coding agents to run broken commands, hallucinate legacy modules, and waste tokens fighting your own docs.

`agentdoc-lint` verifies your agent docs **statically, deterministically, and offline** with **zero required runtime dependencies**.

---

## Architecture

```text
  AGENTS.md / CLAUDE.md / SKILL.md / .cursor/rules
                        │
                        ▼
               [ Markdown Parser ]  (paths, shell commands, Python symbols, env vars, toolchains)
                        │
                        ▼
               [ Drift Engine ]  ◀──── [ Static Codebase Facts ]
               (DOC001 – DOC008)        ├─ Filesystem index + fuzzy rename matcher
                        │               ├─ Python AST index (modules, classes, functions, methods)
                        │               ├─ pyproject.toml / Makefile / package.json / lockfiles
                        │               └─ .env.example & GitHub Actions env keys
                        ▼
        Terminal / GitHub PR Annotations / Markdown / JSON / SARIF 2.1.0
                        │
                        └─ Optional `--suggest-fix` (OpenAI API unified diff patch)
```

---

## Installation

```bash
# Zero-dependency core install (Python 3.10+ stdlib)
pip install agentdoc-lint

# Or run immediately with uvx
uvx agentdoc-lint check .

# Optional: exact OpenAI BPE counting via tiktoken
pip install "agentdoc-lint[tiktoken]"
```

---

## Quick Start

### 1. Run the built-in drift demo

```bash
agentdoc-lint demo
```

### 2. Audit your repository

```bash
# Human-readable terminal report with health score (0-100)
agentdoc-lint check .

# Inspect extracted codebase facts (paths, scripts, AST symbols, env vars)
agentdoc-lint facts .

# GitHub Actions PR inline annotations (::error / ::warning)
agentdoc-lint check . --format github

# Export SARIF 2.1.0 for GitHub Code Scanning
agentdoc-lint check . --format sarif --output agentdoc-lint.sarif
```

### 3. Optional AI patch suggestions (`--suggest-fix`)

All core linting runs 100% offline. When you want a ready-to-review unified diff fixing drifted docs, set `OPENAI_API_KEY` and pass `--suggest-fix`:

```bash
OPENAI_API_KEY=sk-... agentdoc-lint check . --suggest-fix
```

---

## Built-in Lint Rules

| Rule ID | Severity | What It Verifies |
| :--- | :--- | :--- |
| `DOC001` | `error` | **Broken Path**: File or directory referenced in agent instructions does not exist (suggests closest renamed match) |
| `DOC002` | `error` | **Stale Command**: `make <target>`, `npm/pnpm/yarn run <script>`, `python -m <local_mod>`, or `uv/poetry run <script>` does not exist in repo manifests |
| `DOC003` | `error` | **Stale Python Symbol**: Referenced `module.ClassName`, `module:func`, or `from local_pkg import Symbol` does not match Python AST |
| `DOC004` | `warning` | **Undocumented Env Var**: Referenced app env var is absent from `.env.example`, source AST (`os.getenv`), and CI workflows |
| `DOC005` | `warning` | **Toolchain Contradiction**: Conflicting package manager / linter rules across instruction files or vs. repo lockfiles (`uv.lock`, `pnpm-lock.yaml`, etc.) |
| `DOC006` | `warning` | **Context Token Bloat**: Single instruction file or combined instruction footprint exceeds token budget |
| `DOC007` | `error` | **Invalid Skill Frontmatter**: `SKILL.md` missing YAML frontmatter or required `name`/`description` fields |
| `DOC008` | `info` | **Missing Charter Sections**: Root `AGENTS.md`/`CLAUDE.md` missing build/verification commands or architecture guidance |

---

## `pre-commit` & GitHub Actions Integration

### `.pre-commit-config.yaml`

```yaml
repos:
  - repo: https://github.com/264288425a-wq/agentdoc-lint
    rev: v0.1.0
    hooks:
      - id: agentdoc-lint
```

### GitHub Actions

```yaml
name: Agent Docs Drift Check
on: [pull_request]

jobs:
  agentdoc-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: 264288425a-wq/agentdoc-lint@v0.1.0
        with:
          fail-on: "error"
          sarif-output: "agentdoc-lint.sarif"
```

---

## Roadmap

- [x] Auto-discovery of `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, `.cursor/rules`, and Copilot instructions
- [x] Python AST symbol & module verification + `pyproject.toml` / `Makefile` / `package.json` fact checking
- [x] Cross-file & lockfile toolchain conflict detection (`DOC005`)
- [x] GitHub PR inline annotations, Markdown, JSON, and SARIF 2.1.0 reporters
- [x] Opt-in OpenAI API `--suggest-fix` unified diff generator
- [ ] TypeScript / Go / Rust AST symbol extractors
- [ ] `agentdoc-lint init` starter generator inferred from real repo manifests
- [ ] Git diff-scoped mode (`--changed-since origin/main`) for large monorepos

---

## Contributing & Security

- See [CONTRIBUTING.md](CONTRIBUTING.md) for local setup and test commands.
- See [SECURITY.md](SECURITY.md) for our static-analysis threat model and vulnerability reporting.
- Licensed under the [MIT License](LICENSE).
