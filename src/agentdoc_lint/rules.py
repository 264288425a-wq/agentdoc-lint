"""Drift validation engine implementing rules DOC001-DOC008."""

from __future__ import annotations

import difflib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from agentdoc_lint.facts import collect_repo_facts
from agentdoc_lint.models import (
    Diagnostic,
    InstructionDocument,
    LintConfig,
    LintReport,
    RepoFacts,
    ScopeManifests,
    Severity,
)
from agentdoc_lint.parser import discover_instruction_files, parse_instruction_file
from agentdoc_lint.tokenizer import TokenCounter, get_token_counter

RULE_METADATA: dict[str, dict[str, str]] = {
    "DOC001": {
        "name": "BrokenRepositoryPath",
        "short": "File or directory path referenced in agent instructions does not exist.",
        "help": (
            "Update or remove stale paths after refactoring so coding agents do not chase "
            "deleted files."
        ),
    },
    "DOC002": {
        "name": "StaleCommandOrScript",
        "short": "Build, test, or script command in instructions does not exist in manifests.",
        "help": (
            "Verify Make targets, package.json scripts, Python modules, and pyproject entry "
            "points match reality."
        ),
    },
    "DOC003": {
        "name": "StalePythonSymbol",
        "short": "Referenced Python module, class, or function does not exist in repo AST.",
        "help": "Update renamed or removed Python symbols referenced in agent instructions.",
    },
    "DOC004": {
        "name": "UndocumentedEnvVar",
        "short": "Environment variable mentioned in instructions is not declared in code/configs.",
        "help": (
            "Document active env vars in .env.example or remove obsolete variable names from "
            "agent docs."
        ),
    },
    "DOC005": {
        "name": "ToolchainContradiction",
        "short": "Instruction files contradict each other or repo lockfiles on toolchain choice.",
        "help": (
            "Standardize package manager, linter, and typechecker guidance within each "
            "project scope."
        ),
    },
    "DOC006": {
        "name": "InstructionContextBloat",
        "short": "Instruction file or combined agent charter exceeds recommended token budget.",
        "help": (
            "Keep root charters concise and move deep domain procedures into on-demand "
            "SKILL.md files."
        ),
    },
    "DOC007": {
        "name": "InvalidSkillFrontmatter",
        "short": "SKILL.md or instruction frontmatter is missing required metadata or malformed.",
        "help": (
            "Include valid YAML frontmatter with non-empty `name` and `description` fields "
            "in every SKILL.md."
        ),
    },
    "DOC008": {
        "name": "MissingCharterSections",
        "short": "Root agent charter omits essential build/test or architecture overview sections.",
        "help": (
            "Add concise Build/Test commands and a repository layout map to root "
            "AGENTS.md / CLAUDE.md."
        ),
    },
}

_COMMON_PY_RUNNER_TOOLS: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "py",
        "pytest",
        "ruff",
        "mypy",
        "pyright",
        "black",
        "flake8",
        "isort",
        "bandit",
        "pre-commit",
        "uvicorn",
        "gunicorn",
        "fastapi",
        "flask",
        "celery",
        "alembic",
        "coverage",
        "tox",
        "nox",
        "twine",
        "sphinx-build",
        "mkdocs",
        "pip",
        "hatch",
        "maturin",
    }
)

_PLACEHOLDER_ENV_PREFIXES: tuple[str, ...] = (
    "YOUR_",
    "MY_",
    "EXAMPLE_",
    "FOO_",
    "BAR_",
    "TEST_DUMMY_",
)


def _ancestor_dirs(rel_file: str) -> list[str]:
    """Return directory chain from `rel_file`'s parent up to repository root `.`."""
    parent = Path(rel_file).parent.as_posix()
    if parent in ("", "."):
        return ["."]
    parts = parent.split("/")
    dirs = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]
    dirs.append(".")
    return dirs


def _nearest_scope(
    rel_file: str,
    facts: RepoFacts,
    predicate: Callable[[ScopeManifests], bool],
) -> ScopeManifests | None:
    """Walk upward from `rel_file`'s directory to `.` and return first matching manifest scope."""
    for directory in _ancestor_dirs(rel_file):
        scope = facts.scoped_manifests.get(directory)
        if scope is not None and predicate(scope):
            return scope
    return None


def _project_scope_key(rel_file: str, facts: RepoFacts) -> str:
    """Return the nearest project root directory for `rel_file` (for Monorepo isolation)."""
    scope = _nearest_scope(
        rel_file,
        facts,
        lambda s: (
            s.has_pyproject
            or s.has_package_json
            or s.has_makefile
            or bool(s.detected_toolchains)
        ),
    )
    return scope.dir_path if scope is not None else "."


def _closest_suggestion(needle: str, haystack: set[str], cutoff: float = 0.55) -> str | None:
    if not haystack:
        return None
    matches = difflib.get_close_matches(needle, sorted(haystack), n=1, cutoff=cutoff)
    return matches[0] if matches else None


def _closest_path_suggestion(needle: str, haystack: set[str]) -> str | None:
    """Rank candidate renamed paths using parent directory + basename prefix/similarity."""
    if not haystack:
        return None
    needle_p = Path(needle)
    needle_parent = needle_p.parent.as_posix()
    needle_stem = needle_p.stem.lower()
    needle_suffix = needle_p.suffix.lower()

    best_score = 0.0
    best_candidate: str | None = None

    for cand in sorted(haystack):
        cand_p = Path(cand)
        cand_parent = cand_p.parent.as_posix()
        cand_stem = cand_p.stem.lower()
        cand_suffix = cand_p.suffix.lower()

        full_ratio = difflib.SequenceMatcher(None, needle.lower(), cand.lower()).ratio()
        stem_ratio = difflib.SequenceMatcher(None, needle_stem, cand_stem).ratio()
        score = full_ratio * 0.45 + stem_ratio * 0.55
        if cand_parent == needle_parent:
            score += 0.18
        if needle_suffix and cand_suffix == needle_suffix:
            score += 0.08
        if needle_stem and (
            cand_stem.startswith(needle_stem) or needle_stem.startswith(cand_stem)
        ):
            score += 0.25

        if score > best_score:
            best_score = score
            best_candidate = cand

    return best_candidate if best_score >= 0.65 else None


def _check_paths(doc: InstructionDocument, facts: RepoFacts) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    all_entries = facts.all_files | facts.all_dirs
    doc_dir = Path(doc.rel_path).parent.as_posix()

    seen: set[tuple[int, str]] = set()
    for claim in doc.path_claims:
        key = (claim.line, claim.normalized)
        if key in seen:
            continue
        seen.add(key)

        candidate = claim.normalized
        exists = candidate in all_entries
        # Only try doc-relative fallback for inline code paths; Markdown links are already resolved
        if not exists and not claim.is_markdown_link and doc_dir not in ("", "."):
            local_candidate = f"{doc_dir}/{candidate}"
            if local_candidate in all_entries:
                exists = True

        if not exists:
            suggestion_match = _closest_path_suggestion(candidate, all_entries)
            if suggestion_match:
                suggestion = f"Did you mean `{suggestion_match}`? Update `{claim.raw_path}`."
            else:
                suggestion = (
                    f"Verify whether `{claim.raw_path}` was renamed or deleted and update "
                    "the reference."
                )
            diagnostics.append(
                Diagnostic(
                    rule_id="DOC001",
                    severity=Severity.ERROR,
                    file=doc.rel_path,
                    line=claim.line,
                    message=f"Referenced path `{claim.raw_path}` does not exist in repository.",
                    suggestion=suggestion,
                    snippet=claim.raw_path,
                )
            )
    return diagnostics


def _check_commands(doc: InstructionDocument, facts: RepoFacts) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[int, str, str]] = set()
    doc_dir = Path(doc.rel_path).parent.as_posix()

    make_scope = _nearest_scope(doc.rel_path, facts, lambda s: s.has_makefile)
    pkg_scope = _nearest_scope(doc.rel_path, facts, lambda s: s.has_package_json)
    py_scope = _nearest_scope(doc.rel_path, facts, lambda s: s.has_pyproject)

    for claim in doc.command_claims:
        dedup = (claim.line, claim.kind, claim.target)
        if dedup in seen:
            continue
        seen.add(dedup)

        if claim.kind == "make":
            if make_scope is not None and claim.target not in make_scope.makefile_targets:
                match = _closest_suggestion(claim.target, make_scope.makefile_targets)
                avail = ", ".join(sorted(make_scope.makefile_targets)[:8]) or "none"
                hint = (
                    f"Did you mean `make {match}`?"
                    if match
                    else f"Available Makefile targets: {avail}."
                )
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC002",
                        severity=Severity.ERROR,
                        file=doc.rel_path,
                        line=claim.line,
                        message=(
                            f"Makefile target `{claim.target}` does not exist "
                            f"(`{claim.raw_command}`)."
                        ),
                        suggestion=hint,
                        snippet=claim.raw_command,
                    )
                )

        elif claim.kind == "js_script":
            if pkg_scope is not None and claim.target not in pkg_scope.package_scripts:
                match = _closest_suggestion(claim.target, pkg_scope.package_scripts)
                avail = ", ".join(sorted(pkg_scope.package_scripts)[:8]) or "none"
                hint = (
                    f"Did you mean `{claim.runner} run {match}`?"
                    if match
                    else f"Available package.json scripts: {avail}."
                )
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC002",
                        severity=Severity.ERROR,
                        file=doc.rel_path,
                        line=claim.line,
                        message=(
                            f"package.json script `{claim.target}` does not exist "
                            f"(`{claim.raw_command}`)."
                        ),
                        suggestion=hint,
                        snippet=claim.raw_command,
                    )
                )

        elif claim.kind == "python_module":
            top_pkg = claim.target.split(".", 1)[0]
            if top_pkg in facts.python_root_packages and claim.target not in facts.python_modules:
                match = _closest_suggestion(claim.target, facts.python_modules)
                hint = (
                    f"Did you mean `python -m {match}`?"
                    if match
                    else "Update the module path to match your current Python package layout."
                )
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC002",
                        severity=Severity.ERROR,
                        file=doc.rel_path,
                        line=claim.line,
                        message=(
                            f"Local Python module `{claim.target}` not found "
                            f"(`{claim.raw_command}`)."
                        ),
                        suggestion=hint,
                        snippet=claim.raw_command,
                    )
                )

        elif claim.kind == "py_runner":
            target = claim.target.replace("\\", "/")
            if target.endswith(".py") or "/" in target:
                norm = target.lstrip("./")
                exists = norm in facts.all_files or (
                    doc_dir not in ("", ".") and f"{doc_dir}/{norm}" in facts.all_files
                )
                if not exists:
                    match = _closest_path_suggestion(norm, facts.all_files)
                    hint = (
                        f"Did you mean `{claim.runner} run {match}`?"
                        if match
                        else f"Update or remove missing script path `{target}`."
                    )
                    diagnostics.append(
                        Diagnostic(
                            rule_id="DOC002",
                            severity=Severity.ERROR,
                            file=doc.rel_path,
                            line=claim.line,
                            message=(
                                f"Script file `{target}` referenced by `{claim.runner} run` "
                                "does not exist."
                            ),
                            suggestion=hint,
                            snippet=claim.raw_command,
                        )
                    )
            elif (
                py_scope is not None
                and py_scope.pyproject_scripts
                and target not in _COMMON_PY_RUNNER_TOOLS
                and target not in py_scope.pyproject_scripts
            ):
                match = _closest_suggestion(target, py_scope.pyproject_scripts)
                avail = ", ".join(sorted(py_scope.pyproject_scripts))
                hint = (
                    f"Did you mean `{claim.runner} run {match}`?"
                    if match
                    else f"Registered pyproject.toml scripts: {avail}."
                )
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC002",
                        severity=Severity.ERROR,
                        file=doc.rel_path,
                        line=claim.line,
                        message=(
                            f"CLI entry point `{target}` is not defined in pyproject.toml scripts."
                        ),
                        suggestion=hint,
                        snippet=claim.raw_command,
                    )
                )

        elif (
            claim.kind == "pytest_marker"
            and py_scope is not None
            and py_scope.pytest_markers
            and claim.target not in py_scope.pytest_markers
        ):
            match = _closest_suggestion(claim.target, py_scope.pytest_markers)
            avail = ", ".join(sorted(py_scope.pytest_markers))
            hint = (
                f"Did you mean `pytest -m {match}`?"
                if match
                else f"Configured pytest markers: {avail}."
            )
            diagnostics.append(
                Diagnostic(
                    rule_id="DOC002",
                    severity=Severity.ERROR,
                    file=doc.rel_path,
                    line=claim.line,
                    message=(
                        f"Pytest marker `{claim.target}` is not configured in pyproject.toml."
                    ),
                    suggestion=hint,
                    snippet=claim.raw_command,
                )
            )

    return diagnostics


def _check_symbols(doc: InstructionDocument, facts: RepoFacts) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[int, str]] = set()

    for claim in doc.symbol_claims:
        root_pkg = claim.module.split(".", 1)[0]
        if root_pkg not in facts.python_root_packages:
            continue

        dedup = (claim.line, claim.raw_ref)
        if dedup in seen:
            continue
        seen.add(dedup)

        mod = claim.module
        sym = claim.symbol
        if mod not in facts.python_modules and "." in mod:
            parent_mod, maybe_cls = mod.rsplit(".", 1)
            if parent_mod in facts.python_modules and sym:
                candidate_sym = f"{maybe_cls}.{sym}"
                if candidate_sym in facts.module_symbols.get(parent_mod, set()):
                    continue
                mod = parent_mod
                sym = candidate_sym

        if mod not in facts.python_modules:
            match = _closest_suggestion(mod, facts.python_modules)
            hint = (
                f"Did you mean module `{match}`?"
                if match
                else "Update module path to match current Python package structure."
            )
            diagnostics.append(
                Diagnostic(
                    rule_id="DOC003",
                    severity=Severity.ERROR,
                    file=doc.rel_path,
                    line=claim.line,
                    message=f"Python module `{claim.module}` in `{claim.raw_ref}` does not exist.",
                    suggestion=hint,
                    snippet=claim.raw_ref,
                )
            )
            continue

        if sym is not None:
            submod = f"{mod}.{sym}"
            mod_syms = facts.module_symbols.get(mod, set())
            if sym not in mod_syms and submod not in facts.python_modules:
                match = _closest_suggestion(sym, mod_syms)
                if not match:
                    match = _closest_suggestion(sym, set(facts.all_python_symbols.keys()))
                hint = (
                    f"Did you mean `{match}`?"
                    if match
                    else f"Symbol `{sym}` was not found in `{mod}` AST definitions."
                )
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC003",
                        severity=Severity.ERROR,
                        file=doc.rel_path,
                        line=claim.line,
                        message=(
                            f"Python symbol `{sym}` not found in module `{mod}` "
                            f"(`{claim.raw_ref}`)."
                        ),
                        suggestion=hint,
                        snippet=claim.raw_ref,
                    )
                )

    return diagnostics


def _check_env_vars(doc: InstructionDocument, facts: RepoFacts) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if not (facts.env_vars or facts.python_modules):
        return diagnostics

    seen: set[str] = set()
    for claim in doc.env_claims:
        if claim.name in seen:
            continue
        seen.add(claim.name)
        if claim.name.startswith(_PLACEHOLDER_ENV_PREFIXES):
            continue
        if claim.name not in facts.env_vars:
            match = _closest_suggestion(claim.name, facts.env_vars, cutoff=0.6)
            hint = (
                f"Did you mean `{match}`?"
                if match
                else (
                    f"Add `{claim.name}` to `.env.example` or verify whether it is still "
                    "used in code."
                )
            )
            diagnostics.append(
                Diagnostic(
                    rule_id="DOC004",
                    severity=Severity.WARNING,
                    file=doc.rel_path,
                    line=claim.line,
                    message=(
                        f"Environment variable `{claim.name}` is not referenced in code, "
                        "`.env.example`, or workflows."
                    ),
                    suggestion=hint,
                    snippet=claim.raw_text,
                )
            )
    return diagnostics


def _nearest_toolchain_scope(
    rel_file: str,
    facts: RepoFacts,
    category: str,
) -> ScopeManifests | None:
    """Return nearest ancestor scope declaring at least one toolchain in `category`."""
    for directory in _ancestor_dirs(rel_file):
        scope = facts.scoped_manifests.get(directory)
        if scope is not None and scope.detected_toolchains.get(category):
            return scope
    return None


def _check_toolchains(documents: list[InstructionDocument], facts: RepoFacts) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    # 1. Scoped repo lockfile/config vs doc contradiction
    for doc in documents:
        reported_doc_cat: set[tuple[str, str]] = set()
        for claim in doc.toolchain_claims:
            tc_scope = _nearest_toolchain_scope(doc.rel_path, facts, claim.category)
            if tc_scope is None:
                continue
            canonical = tc_scope.detected_toolchains.get(claim.category, set())
            if len(canonical) == 1 and claim.tool not in canonical:
                expected = next(iter(canonical))
                key = (claim.category, claim.tool)
                if key not in reported_doc_cat:
                    reported_doc_cat.add(key)
                    diagnostics.append(
                        Diagnostic(
                            rule_id="DOC005",
                            severity=Severity.WARNING,
                            file=doc.rel_path,
                            line=claim.line,
                            message=(
                                f"Document prescribes `{claim.tool}`, but repository "
                                f"lockfiles/configs use `{expected}`."
                            ),
                            suggestion=(
                                f"Align `{doc.rel_path}` with `{expected}` to avoid failed "
                                "agent commands."
                            ),
                            snippet=claim.snippet,
                        )
                    )

    # 2. Cross-file contradiction within the same nearest project scope
    by_scope_cat: dict[tuple[str, str], dict[str, tuple[str, int, str]]] = defaultdict(dict)
    for doc in documents:
        scope_key = _project_scope_key(doc.rel_path, facts)
        for claim in doc.toolchain_claims:
            by_scope_cat[(scope_key, claim.category)].setdefault(
                claim.tool, (doc.rel_path, claim.line, claim.snippet)
            )

    for (_scope_key, category), tools_map in by_scope_cat.items():
        if len(tools_map) >= 2:
            ordered = sorted(tools_map.items(), key=lambda item: (item[1][0], item[1][1]))
            primary_tool, (primary_file, primary_line, _) = ordered[0]
            for other_tool, (other_file, other_line, snippet) in ordered[1:]:
                if other_file == primary_file:
                    continue
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC005",
                        severity=Severity.WARNING,
                        file=other_file,
                        line=other_line,
                        message=(
                            f"Cross-file {category} contradiction: `{other_file}` prescribes "
                            f"`{other_tool}` while `{primary_file}:{primary_line}` prescribes "
                            f"`{primary_tool}`."
                        ),
                        suggestion=(
                            f"Pick one canonical tool (`{primary_tool}` or `{other_tool}`) "
                            "across all agent instruction files in this scope."
                        ),
                        snippet=snippet,
                    )
                )

    return diagnostics


def _check_frontmatter_and_structure(
    doc: InstructionDocument,
    config: LintConfig,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    if doc.frontmatter_error:
        diagnostics.append(
            Diagnostic(
                rule_id="DOC007",
                severity=Severity.ERROR,
                file=doc.rel_path,
                line=1,
                message=doc.frontmatter_error,
                suggestion="Fix the YAML frontmatter delimiters (`---`) and `key: value` syntax.",
            )
        )
    elif doc.kind == "skill":
        if not doc.has_frontmatter:
            diagnostics.append(
                Diagnostic(
                    rule_id="DOC007",
                    severity=Severity.ERROR,
                    file=doc.rel_path,
                    line=1,
                    message=(
                        "SKILL.md is missing required YAML frontmatter (`name` and `description`)."
                    ),
                    suggestion=(
                        "Add a `---` YAML frontmatter block at line 1 with `name` and "
                        "`description`."
                    ),
                )
            )
        else:
            missing = [
                k for k in ("name", "description") if not doc.frontmatter.get(k, "").strip()
            ]
            if missing:
                diagnostics.append(
                    Diagnostic(
                        rule_id="DOC007",
                        severity=Severity.ERROR,
                        file=doc.rel_path,
                        line=1,
                        message=(
                            f"SKILL.md frontmatter is missing required field(s): "
                            f"{', '.join(missing)}."
                        ),
                        suggestion=(
                            "Define non-empty `name` and `description` keys in YAML frontmatter."
                        ),
                    )
                )

    if doc.token_count > config.max_file_tokens:
        diagnostics.append(
            Diagnostic(
                rule_id="DOC006",
                severity=Severity.WARNING,
                file=doc.rel_path,
                line=1,
                message=(
                    f"`{doc.rel_path}` consumes {doc.token_count:,} tokens "
                    f"(budget: {config.max_file_tokens:,})."
                ),
                suggestion=(
                    "Trim verbose prose and offload specialized workflows into on-demand "
                    "SKILL.md files."
                ),
            )
        )

    if doc.rel_path in ("AGENTS.md", "CLAUDE.md"):
        headings_lower = " ".join(h.lower() for _, h in doc.headings)
        has_verify = bool(doc.command_claims) or any(
            kw in headings_lower
            for kw in ("test", "build", "command", "verify", "verification", "dev")
        )
        has_layout = any(
            kw in headings_lower
            for kw in ("architecture", "layout", "structure", "overview", "map")
        )
        missing_sections: list[str] = []
        if not has_verify:
            missing_sections.append("build/test verification commands")
        if not has_layout:
            missing_sections.append("architecture/layout overview")
        if missing_sections:
            diagnostics.append(
                Diagnostic(
                    rule_id="DOC008",
                    severity=Severity.INFO,
                    file=doc.rel_path,
                    line=1,
                    message=(
                        f"Root charter `{doc.rel_path}` appears to omit: "
                        f"{', '.join(missing_sections)}."
                    ),
                    suggestion=(
                        "Add short sections listing exact build/test commands and key source "
                        "directories."
                    ),
                )
            )

    return diagnostics


def lint_repository(
    root: str | Path,
    config: LintConfig | None = None,
    *,
    counter: TokenCounter | None = None,
) -> LintReport:
    """Run all agentdoc-lint rules against a repository root and return a LintReport."""
    cfg = config or LintConfig()
    tok = counter or get_token_counter()

    facts = collect_repo_facts(root, extra_ignore_dirs=cfg.ignore_dirs)
    doc_paths = discover_instruction_files(facts.root, extra_ignore_dirs=cfg.ignore_dirs)
    documents = [parse_instruction_file(facts.root, p, counter=tok) for p in doc_paths]

    diagnostics: list[Diagnostic] = []
    for doc in documents:
        diagnostics.extend(_check_paths(doc, facts))
        diagnostics.extend(_check_commands(doc, facts))
        diagnostics.extend(_check_symbols(doc, facts))
        if cfg.check_env_vars:
            diagnostics.extend(_check_env_vars(doc, facts))
        diagnostics.extend(_check_frontmatter_and_structure(doc, cfg))

    diagnostics.extend(_check_toolchains(documents, facts))

    total_tokens = sum(d.token_count for d in documents)
    if documents and total_tokens > cfg.max_total_tokens:
        diagnostics.append(
            Diagnostic(
                rule_id="DOC006",
                severity=Severity.WARNING,
                file=documents[0].rel_path,
                line=1,
                message=(
                    f"Combined instruction footprint across {len(documents)} files is "
                    f"{total_tokens:,} tokens (budget: {cfg.max_total_tokens:,})."
                ),
                suggestion=(
                    "Consolidate duplicate instructions across AGENTS.md, CLAUDE.md, and "
                    "cursor rules."
                ),
            )
        )

    if cfg.ignore_rules:
        ignored = {r.upper() for r in cfg.ignore_rules}
        diagnostics = [d for d in diagnostics if d.rule_id.upper() not in ignored]

    diagnostics.sort(key=lambda d: (-d.severity.rank(), d.file, d.line, d.rule_id))

    penalty = sum(
        15 if d.severity == Severity.ERROR else (5 if d.severity == Severity.WARNING else 2)
        for d in diagnostics
    )
    health_score = max(0, 100 - penalty)

    return LintReport(
        root=str(facts.root),
        tokenizer_name=tok.name,
        documents=documents,
        diagnostics=diagnostics,
        total_tokens=total_tokens,
        health_score=health_score,
        skipped_files=list(facts.skipped_python_files),
    )
