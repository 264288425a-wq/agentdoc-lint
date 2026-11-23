# Changelog

All notable changes to `agentdoc-lint` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-23

### Added
- Static discovery and parsing of `AGENTS.md`, `CLAUDE.md`, `SKILL.md`, `.cursor/rules/*.mdc`, `.cursorrules`, `GEMINI.md`, and `.github/copilot-instructions.md`.
- Zero-dependency repository fact extractor covering filesystem index, Python AST modules/classes/functions/methods, `pyproject.toml`, `Makefile`, `package.json`, lockfiles, `.env.example`, and GitHub workflow env vars.
- Eight high-signal lint rules (`DOC001`–`DOC008`) catching broken paths, stale commands, renamed Python symbols, undocumented env vars, cross-file & repo-vs-doc toolchain contradictions, context token bloat, invalid skill frontmatter, and missing charter sections.
- Multi-format reporters: terminal diagnostics with health score, native GitHub Actions PR annotations (`::error`/`::warning`), Markdown summary, JSON, and SARIF 2.1.0.
- Optional OpenAI API `--suggest-fix` unified diff generator using pure Python `urllib.request`.
- Ready-to-use `.pre-commit-hooks.yaml` and composite GitHub Action (`action.yml`).
