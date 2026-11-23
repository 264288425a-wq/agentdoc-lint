"""agentdoc-lint: Catch instruction drift in AGENTS.md, CLAUDE.md, SKILL.md, and .cursor/rules."""

from __future__ import annotations

from agentdoc_lint.facts import collect_repo_facts
from agentdoc_lint.models import (
    Diagnostic,
    InstructionDocument,
    LintConfig,
    LintReport,
    RepoFacts,
    Severity,
)
from agentdoc_lint.parser import discover_instruction_files, parse_instruction_file
from agentdoc_lint.rules import lint_repository

__version__ = "0.1.0"

__all__ = [
    "Diagnostic",
    "InstructionDocument",
    "LintConfig",
    "LintReport",
    "RepoFacts",
    "Severity",
    "__version__",
    "collect_repo_facts",
    "discover_instruction_files",
    "lint_repository",
    "parse_instruction_file",
]
