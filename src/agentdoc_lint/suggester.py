"""Opt-in OpenAI API patch suggester using pure Python stdlib `urllib.request`."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from agentdoc_lint.models import LintReport

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5-mini"

TransportFn = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


def _default_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"OpenAI API HTTP {exc.code}: {body}") from exc


def build_fix_prompt(report: LintReport) -> str:
    """Build a compact, grounded prompt from failing diagnostics and affected doc snippets."""
    lines = [
        "You are maintaining repository instruction files (AGENTS.md, CLAUDE.md, SKILL.md).",
        "Fix ONLY the verified drift diagnostics below and return a clean unified diff.",
        "Do not invent paths, commands, or symbols not grounded in the suggestions.",
        "",
        "Diagnostics:",
    ]
    for d in report.diagnostics[:25]:
        lines.append(
            f"- [{d.severity.value}] {d.rule_id} {d.file}:{d.line}: {d.message} "
            f"(Suggestion: {d.suggestion})"
        )

    affected_files = {d.file for d in report.diagnostics[:25]}
    lines.append("")
    lines.append("Affected file excerpts:")
    for doc in report.documents:
        if doc.rel_path not in affected_files:
            continue
        lines.append(f"\n--- FILE: {doc.rel_path} ---")
        doc_lines = doc.raw_text.splitlines()
        for idx, text in enumerate(doc_lines[:120], start=1):
            lines.append(f"{idx:3d}: {text}")
    return "\n".join(lines)


def suggest_fixes(
    report: LintReport,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    api_base: str | None = None,
    timeout: float = 30.0,
    transport: TransportFn | None = None,
) -> str:
    """Call an OpenAI-compatible Chat Completions endpoint to draft a unified diff fix."""
    if not report.diagnostics:
        return "No instruction drift diagnostics found; no patch needed."

    resolved_key = api_key or os.getenv("OPENAI_API_KEY")
    if not resolved_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export OPENAI_API_KEY to use --suggest-fix."
        )

    base = (api_base or os.getenv("OPENAI_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    url = f"{base}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {resolved_key}",
    }
    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a precise documentation maintenance assistant. "
                    "Output concise unified diffs fixing verified instruction drift."
                ),
            },
            {"role": "user", "content": build_fix_prompt(report)},
        ],
    }

    send = transport or _default_transport
    response = send(url, headers, payload, timeout)
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first_choice = choices[0]
        if isinstance(first_choice, dict):
            message = first_choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content.strip()
    raise RuntimeError(f"Unexpected API response shape: {response}")
