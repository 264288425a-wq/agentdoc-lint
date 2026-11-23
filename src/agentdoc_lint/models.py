"""Core strongly typed data models for agentdoc-lint."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Severity(str, Enum):
    """Diagnostic severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    def rank(self) -> int:
        return {Severity.INFO: 1, Severity.WARNING: 2, Severity.ERROR: 3}[self]


@dataclass(slots=True)
class PathClaim:
    """A repository file or directory path referenced inside an instruction document."""

    file: str
    line: int
    raw_path: str
    normalized: str
    is_markdown_link: bool = False


@dataclass(slots=True)
class CommandClaim:
    """An executable build/test/lint command extracted from instructions."""

    file: str
    line: int
    raw_command: str
    kind: str  # "make" | "js_script" | "python_module" | "py_runner" | "pytest_marker"
    runner: str
    target: str


@dataclass(slots=True)
class SymbolClaim:
    """A Python module/class/function reference extracted from docs or Python snippets."""

    file: str
    line: int
    raw_ref: str
    module: str
    symbol: str | None = None


@dataclass(slots=True)
class EnvClaim:
    """An environment variable name referenced in instructions."""

    file: str
    line: int
    name: str
    raw_text: str


@dataclass(slots=True)
class ToolchainClaim:
    """A prescribed toolchain choice (package manager, linter, typechecker)."""

    file: str
    line: int
    category: str  # "python_pm" | "js_pm" | "python_linter" | "python_typechecker"
    tool: str
    snippet: str


@dataclass(slots=True)
class InstructionDocument:
    """Parsed representation of one coding-agent instruction file."""

    path: Path
    rel_path: str
    kind: str  # "agents" | "claude" | "skill" | "cursor" | "copilot" | "gemini" | "codex"
    raw_text: str
    token_count: int
    frontmatter: dict[str, str] = field(default_factory=dict)
    has_frontmatter: bool = False
    frontmatter_error: str | None = None
    headings: list[tuple[int, str]] = field(default_factory=list)
    path_claims: list[PathClaim] = field(default_factory=list)
    command_claims: list[CommandClaim] = field(default_factory=list)
    symbol_claims: list[SymbolClaim] = field(default_factory=list)
    env_claims: list[EnvClaim] = field(default_factory=list)
    toolchain_claims: list[ToolchainClaim] = field(default_factory=list)


@dataclass(slots=True)
class ScopeManifests:
    """Manifest and toolchain facts collected for one directory scope (root or subproject)."""

    dir_path: str
    pyproject_scripts: set[str] = field(default_factory=set)
    pytest_markers: set[str] = field(default_factory=set)
    makefile_targets: set[str] = field(default_factory=set)
    package_scripts: set[str] = field(default_factory=set)
    detected_toolchains: dict[str, set[str]] = field(default_factory=dict)
    has_pyproject: bool = False
    has_makefile: bool = False
    has_package_json: bool = False


@dataclass(slots=True)
class RepoFacts:
    """Ground-truth facts statically extracted from a repository."""

    root: Path
    all_files: set[str] = field(default_factory=set)
    all_dirs: set[str] = field(default_factory=set)
    python_root_packages: set[str] = field(default_factory=set)
    python_modules: set[str] = field(default_factory=set)
    module_symbols: dict[str, set[str]] = field(default_factory=dict)
    all_python_symbols: dict[str, list[str]] = field(default_factory=dict)
    pyproject_scripts: set[str] = field(default_factory=set)
    pytest_markers: set[str] = field(default_factory=set)
    makefile_targets: set[str] = field(default_factory=set)
    package_scripts: set[str] = field(default_factory=set)
    env_vars: set[str] = field(default_factory=set)
    detected_toolchains: dict[str, set[str]] = field(default_factory=dict)
    scoped_manifests: dict[str, ScopeManifests] = field(default_factory=dict)
    skipped_python_files: list[str] = field(default_factory=list)
    has_pyproject: bool = False
    has_makefile: bool = False
    has_package_json: bool = False

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "files_indexed": len(self.all_files),
            "dirs_indexed": len(self.all_dirs),
            "python_root_packages": sorted(self.python_root_packages),
            "python_modules": sorted(self.python_modules),
            "pyproject_scripts": sorted(self.pyproject_scripts),
            "pytest_markers": sorted(self.pytest_markers),
            "makefile_targets": sorted(self.makefile_targets),
            "package_scripts": sorted(self.package_scripts),
            "env_vars": sorted(self.env_vars),
            "detected_toolchains": {
                k: sorted(v) for k, v in sorted(self.detected_toolchains.items())
            },
            "skipped_python_files": list(self.skipped_python_files),
        }


@dataclass(slots=True)
class LintConfig:
    """Configurable lint budgets and rule filters."""

    max_file_tokens: int = 3000
    max_total_tokens: int = 8000
    ignore_rules: set[str] = field(default_factory=set)
    ignore_dirs: set[str] = field(default_factory=set)
    check_env_vars: bool = True


@dataclass(slots=True)
class Diagnostic:
    """Single actionable lint diagnostic pinned to a file and line."""

    rule_id: str
    severity: Severity
    file: str
    line: int
    message: str
    suggestion: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "suggestion": self.suggestion,
            "snippet": self.snippet,
        }


@dataclass(slots=True)
class LintReport:
    """Complete repository drift audit report."""

    root: str
    tokenizer_name: str
    documents: list[InstructionDocument]
    diagnostics: list[Diagnostic]
    total_tokens: int
    health_score: int
    skipped_files: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for d in self.diagnostics if d.severity == Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for d in self.diagnostics if d.severity == Severity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for d in self.diagnostics if d.severity == Severity.INFO)

    def passes(self, fail_on: Severity | None = Severity.ERROR) -> bool:
        if fail_on is None:
            return True
        threshold = fail_on.rank()
        return all(d.severity.rank() < threshold for d in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "tokenizer": self.tokenizer_name,
            "summary": {
                "documents_scanned": len(self.documents),
                "total_tokens": self.total_tokens,
                "health_score": self.health_score,
                "errors": self.error_count,
                "warnings": self.warning_count,
                "info": self.info_count,
                "skipped_files": len(self.skipped_files),
            },
            "documents": [
                {
                    "path": doc.rel_path,
                    "kind": doc.kind,
                    "tokens": doc.token_count,
                    "path_claims": len(doc.path_claims),
                    "command_claims": len(doc.command_claims),
                    "symbol_claims": len(doc.symbol_claims),
                }
                for doc in self.documents
            ],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
        }
