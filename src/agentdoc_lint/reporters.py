"""Output formatters: terminal, GitHub PR annotations, Markdown, JSON, and SARIF 2.1.0."""

from __future__ import annotations

import json
import os
import posixpath
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from agentdoc_lint.models import LintReport, Severity
from agentdoc_lint.rules import RULE_METADATA

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_CYAN = "\033[36m"


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def _style(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_RESET}" if enabled else text


def render_terminal(report: LintReport, *, color: bool = False) -> str:
    """Render diagnostics and charter health score for terminal display."""
    lines: list[str] = []
    if report.health_score >= 85:
        score_color = _GREEN
    elif report.health_score >= 60:
        score_color = _YELLOW
    else:
        score_color = _RED
    title = f"agentdoc-lint Report - Health Score: {report.health_score}/100"
    lines.append(_style(title, _BOLD + score_color, color))
    lines.append(_style("=" * len(title), _DIM, color))
    lines.append(
        f"Root: {report.root}  |  Files: {len(report.documents)}  |  "
        f"Total Tokens: {report.total_tokens:,} ({report.tokenizer_name})"
    )

    if report.documents:
        lines.append("")
        lines.append(_style("Scanned Instruction Files:", _BOLD, color))
        for doc in report.documents:
            lines.append(
                f"  - {doc.rel_path:<36}  [{doc.kind:<7}]  {doc.token_count:>5,} tok  "
                f"(paths:{len(doc.path_claims)} cmds:{len(doc.command_claims)} "
                f"syms:{len(doc.symbol_claims)})"
            )

    lines.append("")
    if not report.diagnostics:
        lines.append(
            _style("All coding-agent instructions match repository facts.", _GREEN + _BOLD, color)
        )
        return "\n".join(lines)

    lines.append(
        _style(
            f"Diagnostics ({report.error_count} errors, {report.warning_count} warnings, "
            f"{report.info_count} info):",
            _BOLD,
            color,
        )
    )
    for d in report.diagnostics:
        sev_col = {
            Severity.ERROR: _RED,
            Severity.WARNING: _YELLOW,
            Severity.INFO: _CYAN,
        }[d.severity]
        badge = _style(f"[{d.severity.value.upper():<7}]", sev_col, color)
        loc = _style(f"{d.file}:{d.line}", _BOLD, color)
        lines.append(f"  {badge} {d.rule_id} {loc} - {d.message}")
        lines.append(f"            Fix: {d.suggestion}")

    return "\n".join(lines)


def _escape_gh_data(value: str) -> str:
    """Escape message body for GitHub Actions workflow commands."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_gh_property(value: str) -> str:
    """Escape property values (`file`, `title`) for GitHub Actions workflow commands."""
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def render_github_annotations(report: LintReport) -> str:
    """Render native GitHub Actions workflow commands (`::error`, `::warning`, `::notice`)."""
    level_map = {
        Severity.ERROR: "error",
        Severity.WARNING: "warning",
        Severity.INFO: "notice",
    }
    lines: list[str] = []
    for d in report.diagnostics:
        level = level_map[d.severity]
        file_prop = _escape_gh_property(d.file)
        title_prop = _escape_gh_property(d.rule_id)
        msg = _escape_gh_data(f"{d.message} Fix: {d.suggestion}")
        lines.append(f"::{level} file={file_prop},line={d.line},title={title_prop}::{msg}")
    if not lines:
        lines.append(
            f"::notice title=agentdoc-lint::All {len(report.documents)} instruction files passed "
            f"(Health Score {report.health_score}/100)."
        )
    return "\n".join(lines)


def render_markdown(report: LintReport) -> str:
    """Render a PR comment / Job Summary Markdown report."""
    status = "PASS" if report.passes(Severity.ERROR) else "FAIL"
    lines: list[str] = [
        f"## agentdoc-lint Drift Report ({status} - Score `{report.health_score}/100`)",
        "",
        "| Metric | Value |",
        "| :--- | :--- |",
        f"| **Instruction Files Scanned** | `{len(report.documents)}` |",
        f"| **Combined Token Footprint** | `{report.total_tokens:,}` (`{report.tokenizer_name}`) |",
        (
            f"| **Errors / Warnings / Info** | `{report.error_count}` / "
            f"`{report.warning_count}` / `{report.info_count}` |"
        ),
        "",
    ]

    if report.documents:
        lines.append("### Scanned Files")
        lines.append("")
        lines.append("| File | Kind | Tokens | Paths | Commands | Symbols |")
        lines.append("| :--- | :--- | ---: | ---: | ---: | ---: |")
        for doc in report.documents:
            lines.append(
                f"| `{doc.rel_path}` | `{doc.kind}` | {doc.token_count:,} | "
                f"{len(doc.path_claims)} | {len(doc.command_claims)} | {len(doc.symbol_claims)} |"
            )
        lines.append("")

    if report.diagnostics:
        lines.append("### Drift Diagnostics")
        lines.append("")
        lines.append("| Rule | Severity | Location | Finding | Suggested Fix |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for d in report.diagnostics:
            lines.append(
                f"| `{d.rule_id}` | `{d.severity.value}` | `{d.file}:{d.line}` | "
                f"{d.message} | {d.suggestion} |"
            )
        lines.append("")
    else:
        lines.append("_No instruction drift detected._\n")

    return "\n".join(lines)


def render_json(report: LintReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


def _sarif_relative_uri(rel_file: str, report_root: str, base_dir: str | Path | None) -> str:
    """Build an RFC 3986 percent-encoded relative artifact URI (prefixed if under `base_dir`)."""
    uri_path = rel_file.replace("\\", "/")
    if base_dir is not None:
        base_real = Path(os.path.realpath(base_dir))
        root_real = Path(os.path.realpath(report_root))
        if root_real != base_real and root_real.is_relative_to(base_real):
            prefix = root_real.relative_to(base_real).as_posix()
            uri_path = posixpath.join(prefix, uri_path)
    return urllib.parse.quote(uri_path, safe="/")


def render_sarif(report: LintReport, *, base_dir: str | Path | None = None) -> str:
    """Render diagnostics as a valid SARIF 2.1.0 document for GitHub Code Scanning."""
    level_map = {
        Severity.ERROR: "error",
        Severity.WARNING: "warning",
        Severity.INFO: "note",
    }
    rules = [
        {
            "id": rule_id,
            "name": meta["name"],
            "shortDescription": {"text": meta["short"]},
            "help": {"text": meta["help"]},
        }
        for rule_id, meta in sorted(RULE_METADATA.items())
    ]

    uri_base = Path(os.path.realpath(base_dir if base_dir is not None else report.root)).as_uri()
    if not uri_base.endswith("/"):
        uri_base += "/"

    results: list[dict[str, Any]] = []
    for d in report.diagnostics:
        encoded_uri = _sarif_relative_uri(d.file, report.root, base_dir)
        results.append(
            {
                "ruleId": d.rule_id,
                "level": level_map[d.severity],
                "message": {"text": f"{d.message} Fix: {d.suggestion}"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {
                                "uri": encoded_uri,
                                "uriBaseId": "%SRCROOT%",
                            },
                            "region": {"startLine": max(1, d.line)},
                        }
                    }
                ],
            }
        )

    sarif: dict[str, Any] = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "agentdoc-lint",
                        "version": "0.1.0",
                        "informationUri": "https://github.com/264288425a-wq/agentdoc-lint",
                        "rules": rules,
                    }
                },
                "columnKind": "utf16CodeUnits",
                "originalUriBaseIds": {
                    "%SRCROOT%": {"uri": uri_base},
                },
                "results": results,
            }
        ],
    }
    return json.dumps(sarif, indent=2, ensure_ascii=False)
