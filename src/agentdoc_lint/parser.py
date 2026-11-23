"""Discovery and static claim extraction for coding-agent instruction documents."""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

from agentdoc_lint.facts import (
    DEFAULT_IGNORE_DIRS,
    is_link_or_junction,
    is_safe_repo_file,
    is_within_root,
    validate_root_dir,
)
from agentdoc_lint.models import (
    CommandClaim,
    EnvClaim,
    InstructionDocument,
    PathClaim,
    SymbolClaim,
    ToolchainClaim,
)
from agentdoc_lint.tokenizer import TokenCounter, get_token_counter

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*([A-Za-z0-9_-]*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

_KNOWN_EXTENSIONS: frozenset[str] = frozenset(
    {
        "py",
        "pyi",
        "ts",
        "tsx",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "json",
        "toml",
        "yaml",
        "yml",
        "md",
        "mdc",
        "txt",
        "sh",
        "bash",
        "ps1",
        "sql",
        "rs",
        "go",
        "java",
        "kt",
        "rb",
        "php",
        "c",
        "h",
        "cpp",
        "hpp",
        "css",
        "scss",
        "html",
        "xml",
        "cfg",
        "ini",
        "lock",
        "env",
    }
)

_ROOT_SPECIAL_FILES: frozenset[str] = frozenset(
    {
        "Makefile",
        "GNUmakefile",
        "Dockerfile",
        "pyproject.toml",
        "package.json",
        "tsconfig.json",
        "Cargo.toml",
        "go.mod",
        "AGENTS.md",
        "CLAUDE.md",
        "SKILL.md",
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    }
)

_KNOWN_DIR_PREFIXES: frozenset[str] = frozenset(
    {
        "src",
        "tests",
        "test",
        "app",
        "apps",
        "lib",
        "libs",
        "pkg",
        "pkgs",
        "packages",
        "cmd",
        "internal",
        "scripts",
        "docs",
        "examples",
        "config",
        "configs",
        "skills",
        ".github",
        ".cursor",
        ".agents",
        ".claude",
    }
)

_MIME_PREFIXES: frozenset[str] = frozenset(
    {"application", "text", "image", "audio", "video", "multipart", "font"}
)

_NON_PATH_PAIRS: frozenset[str] = frozenset(
    {
        "sync/async",
        "read/write",
        "input/output",
        "true/false",
        "yes/no",
        "and/or",
        "pass/fail",
        "get/post",
        "http/https",
        "stdio/http",
        "error/warning",
        "warning/error",
        "json/yaml",
        "json/sarif",
        "ci/cd",
        "pr/issue",
        "file/dir",
        "files/dirs",
        "dev/prod",
        "build/test",
        "lint/test",
    }
)

_BUILTIN_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TERM",
        "PWD",
        "OLDPWD",
        "LANG",
        "LC_ALL",
        "TZ",
        "TMP",
        "TEMP",
        "TMPDIR",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "VIRTUAL_ENV",
        "NODE_ENV",
        "NO_COLOR",
        "FORCE_COLOR",
        "CI",
        "DEBUG",
        "VERBOSE",
        "EDITOR",
        "VISUAL",
        "GITHUB_ACTIONS",
        "GITHUB_TOKEN",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_OUTPUT",
        "GITHUB_ENV",
        "GITHUB_WORKSPACE",
        "GITHUB_REPOSITORY",
        "GITHUB_SHA",
        "GITHUB_REF",
        "RUNNER_OS",
        "RUNNER_TEMP",
    }
)

# Command extraction regexes
_MAKE_CMD_RE = re.compile(r"(?:^|&&|\|\||;)\s*make(?:\s+-[A-Za-z]+)*\s+([A-Za-z0-9_.-]+)")
_JS_RUN_RE = re.compile(
    r"(?:^|&&|\|\||;)\s*(npm|pnpm|yarn|bun)\s+run\s+([A-Za-z0-9_.:-]+)"
)
_PY_MOD_RE = re.compile(
    r"(?:^|&&|\|\||;)\s*(?:[A-Z0-9_]+=[^\s]+\s+)*(?:python3?|py)\s+-m\s+([A-Za-z_][A-Za-z0-9_.]*)"
)
_PY_RUNNER_RE = re.compile(
    r"(?:^|&&|\|\||;)\s*(uv|poetry)\s+run\s+([A-Za-z0-9_./-]+)"
)
_PYTEST_MARKER_RE = re.compile(
    r"\bpytest\b[^\n]*?\s-m\s+['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?"
)
_INLINE_CMD_PREFIX_RE = re.compile(
    r"^(?:make\s+|(?:npm|pnpm|yarn|bun)\s+run\s+|(?:uv|poetry)\s+run\s+|"
    r"(?:python3?|py)\s+-m\s+|pytest\s+.*-m\s+)"
)
_INLINE_NEGATED_PREFIX_RE = re.compile(
    r"(?:do\s+not|don't|never|avoid|instead\s+of|rather\s+than|not|replaced|deprecated|"
    r"removed|old|legacy)\s+(?:run|running|use|using|call|calling)?\s*$",
    re.I,
)

# Python symbol & import patterns (supporting both top-level and dotted modules)
_PY_IMPORT_FROM_RE = re.compile(
    r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+([A-Za-z_][A-Za-z0-9_,\s*]+)"
)
_PY_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)")
_INLINE_COLON_SYM_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):([A-Za-z_][A-Za-z0-9_.]*)$"
)
_INLINE_DOTTED_SYM_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.([A-Za-z_][A-Za-z0-9_]*)\(?\)?$"
)

# Env var extraction
_SHELL_ENV_RE = re.compile(r"(?:\$\{([A-Z][A-Z0-9_]{2,})\}|\$([A-Z][A-Z0-9_]{2,}))")
_ENV_ASSIGN_RE = re.compile(r"(?:^|\s)(?:export\s+)?([A-Z][A-Z0-9_]{2,})=")
_INLINE_ENV_RE = re.compile(r"^([A-Z][A-Z0-9]*_[A-Z0-9_]+)$")

# Toolchain signals (category, tool, pattern)
_TOOLCHAIN_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("python_pm", "uv", re.compile(r"\buv\s+(?:run|sync|pip|add|lock)\b|\buse\s+`?uv`?\b", re.I)),
    (
        "python_pm",
        "poetry",
        re.compile(r"\bpoetry\s+(?:run|install|add|lock)\b|\buse\s+`?poetry`?\b", re.I),
    ),
    ("python_pm", "pdm", re.compile(r"\bpdm\s+(?:run|install|add|lock)\b|\buse\s+`?pdm`?\b", re.I)),
    (
        "python_pm",
        "pipenv",
        re.compile(r"\bpipenv\s+(?:run|install)\b|\buse\s+`?pipenv`?\b", re.I),
    ),
    (
        "js_pm",
        "pnpm",
        re.compile(r"\bpnpm\s+(?:run|install|add|test|build|exec)\b|\buse\s+`?pnpm`?\b", re.I),
    ),
    (
        "js_pm",
        "yarn",
        re.compile(r"\byarn\s+(?:run|install|add|test|build)\b|\buse\s+`?yarn`?\b", re.I),
    ),
    ("js_pm", "npm", re.compile(r"\bnpm\s+(?:run|install|ci|test)\b|\buse\s+`?npm`?\b", re.I)),
    ("js_pm", "bun", re.compile(r"\bbun\s+(?:run|install|add|test)\b|\buse\s+`?bun`?\b", re.I)),
    (
        "python_linter",
        "ruff",
        re.compile(r"\bruff\s+(?:check|format)\b|\buse\s+`?ruff`?\b", re.I),
    ),
    (
        "python_linter",
        "black",
        re.compile(
            r"\bblack\s+(?:\.|src|tests|--check)\b|\bformat\s+with\s+`?black`?\b|"
            r"\buse\s+`?black`?\b",
            re.I,
        ),
    ),
    ("python_linter", "flake8", re.compile(r"\bflake8\b|\buse\s+`?flake8`?\b", re.I)),
    (
        "python_typechecker",
        "mypy",
        re.compile(r"\bmypy\s+(?:src|\.|tests|--strict)\b|\buse\s+`?mypy`?\b", re.I),
    ),
    ("python_typechecker", "pyright", re.compile(r"\bpyright\b|\buse\s+`?pyright`?\b", re.I)),
)

_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don't|never|avoid|instead\s+of|rather\s+than|not|"
    r"migrate\s+from|migrated\s+from|replaced)\s+(?:use\s+)?`?([a-z0-9_-]+)`?",
    re.I,
)


def classify_instruction_file(rel_posix: str) -> str | None:
    """Return document kind if `rel_posix` is a known coding-agent instruction file."""
    lower = rel_posix.lower()
    name = Path(rel_posix).name.lower()

    if name == "agents.md":
        return "agents"
    if name == "claude.md":
        return "claude"
    if name == "skill.md":
        return "skill"
    if name == "gemini.md":
        return "gemini"
    if name == "codex.md":
        return "codex"
    if name == ".cursorrules":
        return "cursor"
    if lower == ".github/copilot-instructions.md":
        return "copilot"
    if lower.startswith(".cursor/rules/") and lower.endswith((".mdc", ".md")):
        return "cursor"
    return None


def discover_instruction_files(
    root: str | Path,
    *,
    extra_ignore_dirs: set[str] | None = None,
) -> list[Path]:
    """Recursively locate supported coding-agent instruction files safely inside `root`."""
    root_real = validate_root_dir(root)
    ignore_dirs = set(DEFAULT_IGNORE_DIRS)
    if extra_ignore_dirs:
        ignore_dirs.update(extra_ignore_dirs)

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root_real, followlinks=False):
        cur_dir = Path(dirpath)
        safe_subdirs: list[str] = []
        for dirname in sorted(dirnames):
            if dirname in ignore_dirs:
                continue
            cand = cur_dir / dirname
            if is_link_or_junction(cand) or not is_within_root(cand, root_real):
                continue
            safe_subdirs.append(dirname)
        dirnames[:] = safe_subdirs

        for fname in sorted(filenames):
            fpath = cur_dir / fname
            if not is_safe_repo_file(fpath, root_real):
                continue
            rel_posix = fpath.relative_to(root_real).as_posix()
            if classify_instruction_file(rel_posix) is not None:
                found.append(fpath)

    found.sort(key=lambda p: p.relative_to(root_real).as_posix())
    return found


def _extract_markdown_links(line: str) -> list[str]:
    """Single-pass linear-time O(n) Markdown inline link target extractor (ReDoS-safe)."""
    targets: list[str] = []
    n = len(line)
    i = 0

    while i < n:
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            # Skip inline code spans so `[a](b)` inside code is not parsed as a link
            end_tick = line.find("`", i + 1)
            if end_tick == -1:
                break
            i = end_tick + 1
            continue
        if ch != "[":
            i += 1
            continue

        # Scan for the closing `]` of this label without quadratic backtracking
        j = i + 1
        found_close = -1
        while j < n:
            cj = line[j]
            if cj == "\\":
                j += 2
                continue
            if cj == "[":
                # Restart label anchor at inner `[` while advancing strictly forward
                i = j
                j += 1
                continue
            if cj == "]":
                found_close = j
                break
            j += 1

        if found_close == -1:
            # No `]` exists anywhere in the remainder of the line -> done in O(n)
            break

        if found_close + 1 < n and line[found_close + 1] == "(":
            k = found_close + 2
            while k < n and line[k] in (" ", "\t"):
                k += 1
            if k < n and line[k] == "<":
                end_angle = line.find(">", k + 1)
                if end_angle != -1:
                    target = line[k + 1 : end_angle].strip()
                    close_paren = line.find(")", end_angle + 1)
                    if close_paren != -1:
                        if target:
                            targets.append(target)
                        i = close_paren + 1
                        continue
            start_url = k
            depth = 0
            end_url = -1
            end_paren = -1
            while k < n:
                ck = line[k]
                if ck == "\\":
                    k += 2
                    continue
                if ck == "(":
                    depth += 1
                elif ck == ")":
                    if depth == 0:
                        if end_url == -1:
                            end_url = k
                        end_paren = k
                        break
                    depth -= 1
                elif ck in (" ", "\t") and depth == 0 and end_url == -1:
                    end_url = k
                k += 1
            if end_paren != -1 and end_url > start_url:
                targets.append(line[start_url:end_url].strip())
                i = end_paren + 1
                continue

        i = found_close + 1

    return targets


def _parse_frontmatter(lines: list[str]) -> tuple[bool, dict[str, str], str | None, int]:
    """Parse YAML frontmatter block if file starts with `---`.

    Returns `(has_fm, data, error_msg, content_start_index)`.
    """
    if not lines or lines[0].strip() != "---":
        return False, {}, None, 0

    end_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return True, {}, "Unclosed YAML frontmatter block (missing closing `---`).", 1

    fm: dict[str, str] = {}
    current_key: str | None = None
    for idx in range(1, end_idx):
        raw = lines[idx]
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        if raw.startswith((" ", "\t")) and current_key is not None:
            fm[current_key] = (fm[current_key] + " " + raw.strip()).strip()
            continue
        if ":" not in raw:
            msg = f"Malformed frontmatter line {idx + 1}: expected `key: value`."
            return True, fm, msg, end_idx + 1
        key, val = raw.split(":", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            return True, fm, f"Empty frontmatter key on line {idx + 1}.", end_idx + 1
        fm[key] = val
        current_key = key

    return True, fm, None, end_idx + 1


def _normalize_candidate_path(
    raw: str,
    doc_rel_dir: str,
    *,
    is_markdown_link: bool,
) -> str | None:
    """Return a clean POSIX repo-relative path if `raw` looks like a concrete repo path."""
    cleaned = raw.strip()
    if not cleaned:
        return None
    cleaned = cleaned.split("#", 1)[0]
    if re.search(r":[0-9]+(?::[0-9]+)?$", cleaned):
        cleaned = re.sub(r":[0-9]+(?::[0-9]+)?$", "", cleaned)
    cleaned = cleaned.strip().strip("\"'")
    if not cleaned:
        return None

    # Skip URLs, anchors, template placeholders, globs, CLI flags, env vars
    lower_pre = cleaned.lower()
    if (
        "://" in cleaned
        or lower_pre.startswith(("mailto:", "tel:", "data:", "#"))
        or any(ch in cleaned for ch in ("<", ">", "{", "}", "*", "?", "$", "|", " ", "\t"))
        or cleaned.startswith(("-", "~/"))
        or "YOUR_" in cleaned
        or "..." in cleaned
        or "@" in cleaned
    ):
        return None

    # Normalize Windows separators to POSIX BEFORE structural path analysis
    cleaned = cleaned.replace("\\", "/")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:/", cleaned):
        return None

    lower = cleaned.lower()
    if lower in _NON_PATH_PAIRS:
        return None

    explicit_rel = cleaned.startswith(("./", "../"))
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]

    cleaned_no_slash = cleaned.rstrip("/")
    if not cleaned_no_slash or cleaned_no_slash in (".", ".."):
        return None

    if "/" in cleaned_no_slash:
        first_seg = cleaned_no_slash.split("/", 1)[0].lower()
        if first_seg in _MIME_PREFIXES:
            return None
        if not is_markdown_link:
            last_seg = cleaned_no_slash.rsplit("/", 1)[-1]
            has_known_ext = (
                "." in last_seg and last_seg.rsplit(".", 1)[-1].lower() in _KNOWN_EXTENSIONS
            )
            starts_known_dir = first_seg in _KNOWN_DIR_PREFIXES
            is_dir_notation = cleaned.endswith("/") and first_seg in _KNOWN_DIR_PREFIXES
            if not (explicit_rel or has_known_ext or starts_known_dir or is_dir_notation):
                return None
    else:
        # Single segment without slash: require explicit link or `./` to avoid prose noise
        if not (is_markdown_link or explicit_rel):
            return None
        if not is_markdown_link and "." in cleaned_no_slash:
            ext = cleaned_no_slash.rsplit(".", 1)[-1].lower()
            if ext not in _KNOWN_EXTENSIONS and cleaned_no_slash not in _ROOT_SPECIAL_FILES:
                return None
            if re.fullmatch(r"v?[0-9]+(?:\.[0-9]+)+", cleaned_no_slash):
                return None

    # Normalize strictly with POSIX semantics across all platforms
    if is_markdown_link and doc_rel_dir and doc_rel_dir != ".":
        combined = posixpath.join(doc_rel_dir, cleaned_no_slash)
        norm = posixpath.normpath(combined)
        if norm == ".." or norm.startswith("../"):
            return None
        return norm

    norm = posixpath.normpath(cleaned_no_slash)
    if norm in (".", "..") or norm.startswith("../"):
        return None
    return norm


def _extract_shell_claims(
    line: str,
    rel_path: str,
    line_no: int,
    doc: InstructionDocument,
) -> None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return
    if stripped.startswith("$ "):
        stripped = stripped[2:].strip()

    for m in _MAKE_CMD_RE.finditer(stripped):
        target = m.group(1)
        if not target.startswith("-"):
            doc.command_claims.append(
                CommandClaim(
                    file=rel_path,
                    line=line_no,
                    raw_command=stripped,
                    kind="make",
                    runner="make",
                    target=target,
                )
            )

    for m in _JS_RUN_RE.finditer(stripped):
        runner, script = m.group(1), m.group(2)
        if not script.startswith("-"):
            doc.command_claims.append(
                CommandClaim(
                    file=rel_path,
                    line=line_no,
                    raw_command=stripped,
                    kind="js_script",
                    runner=runner,
                    target=script,
                )
            )

    for m in _PY_MOD_RE.finditer(stripped):
        mod = m.group(1)
        doc.command_claims.append(
            CommandClaim(
                file=rel_path,
                line=line_no,
                raw_command=stripped,
                kind="python_module",
                runner="python",
                target=mod,
            )
        )

    for m in _PY_RUNNER_RE.finditer(stripped):
        runner, target = m.group(1), m.group(2)
        if not target.startswith("-"):
            doc.command_claims.append(
                CommandClaim(
                    file=rel_path,
                    line=line_no,
                    raw_command=stripped,
                    kind="py_runner",
                    runner=runner,
                    target=target,
                )
            )

    for m in _PYTEST_MARKER_RE.finditer(stripped):
        marker = m.group(1)
        if marker not in ("not", "and", "or"):
            doc.command_claims.append(
                CommandClaim(
                    file=rel_path,
                    line=line_no,
                    raw_command=stripped,
                    kind="pytest_marker",
                    runner="pytest",
                    target=marker,
                )
            )

    seen_env: set[str] = set()
    for m in _SHELL_ENV_RE.finditer(stripped):
        var = m.group(1) or m.group(2)
        if var and var not in _BUILTIN_ENV_VARS and var not in seen_env:
            seen_env.add(var)
            doc.env_claims.append(
                EnvClaim(file=rel_path, line=line_no, name=var, raw_text=stripped)
            )
    for m in _ENV_ASSIGN_RE.finditer(stripped):
        var = m.group(1)
        if var and var not in _BUILTIN_ENV_VARS and var not in seen_env:
            seen_env.add(var)
            doc.env_claims.append(
                EnvClaim(file=rel_path, line=line_no, name=var, raw_text=stripped)
            )


def _extract_python_block_claims(
    line: str,
    rel_path: str,
    line_no: int,
    doc: InstructionDocument,
) -> None:
    m_from = _PY_IMPORT_FROM_RE.match(line)
    if m_from:
        mod = m_from.group(1)
        names_part = m_from.group(2).split("#", 1)[0]
        for raw_sym in names_part.split(","):
            sym = raw_sym.strip().split(" as ", 1)[0].strip()
            if sym and sym != "*" and sym.isidentifier():
                doc.symbol_claims.append(
                    SymbolClaim(
                        file=rel_path,
                        line=line_no,
                        raw_ref=f"from {mod} import {sym}",
                        module=mod,
                        symbol=sym,
                    )
                )
        return

    m_imp = _PY_IMPORT_RE.match(line)
    if m_imp:
        mod = m_imp.group(1)
        doc.symbol_claims.append(
            SymbolClaim(
                file=rel_path,
                line=line_no,
                raw_ref=f"import {mod}",
                module=mod,
                symbol=None,
            )
        )


def _extract_toolchain_claims(
    line: str,
    rel_path: str,
    line_no: int,
    doc: InstructionDocument,
) -> None:
    negated = {m.group(1).lower() for m in _NEGATION_RE.finditer(line)}
    for category, tool, pattern in _TOOLCHAIN_PATTERNS:
        if tool in negated:
            continue
        if pattern.search(line):
            doc.toolchain_claims.append(
                ToolchainClaim(
                    file=rel_path,
                    line=line_no,
                    category=category,
                    tool=tool,
                    snippet=line.strip()[:120],
                )
            )


def parse_instruction_file(
    root: str | Path,
    file_path: str | Path,
    *,
    counter: TokenCounter | None = None,
) -> InstructionDocument:
    """Parse one agent instruction file into structured claims with exact line numbers."""
    root_real = validate_root_dir(root)
    raw_target = Path(file_path)
    if not is_safe_repo_file(raw_target, root_real):
        raise ValueError(f"Refusing to read unsafe or out-of-root file: {file_path}")

    target_real = Path(os.path.realpath(raw_target))
    rel_posix = target_real.relative_to(root_real).as_posix()
    doc_rel_dir = Path(rel_posix).parent.as_posix()
    kind = classify_instruction_file(rel_posix) or "agents"

    raw_text = target_real.read_text(encoding="utf-8-sig", errors="replace")
    tok = counter or get_token_counter()
    lines = raw_text.splitlines()

    has_fm, fm_data, fm_err, start_idx = _parse_frontmatter(lines)

    doc = InstructionDocument(
        path=target_real,
        rel_path=rel_posix,
        kind=kind,
        raw_text=raw_text,
        token_count=tok.count(raw_text),
        frontmatter=fm_data,
        has_frontmatter=has_fm,
        frontmatter_error=fm_err,
    )

    in_fence = False
    fence_char = ""
    fence_lang = ""

    shell_langs = {"", "sh", "bash", "shell", "zsh", "console", "powershell", "pwsh", "cmd"}
    python_langs = {"py", "python", "python3"}

    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        line_no = idx + 1

        fence_m = _FENCE_RE.match(line.strip())
        if fence_m:
            marker, lang = fence_m.group(1), fence_m.group(2).lower()
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
                fence_lang = lang
            elif marker[0] == fence_char:
                in_fence = False
                fence_char = ""
                fence_lang = ""
            continue

        if in_fence:
            if fence_lang in shell_langs:
                _extract_shell_claims(line, rel_posix, line_no, doc)
                _extract_toolchain_claims(line, rel_posix, line_no, doc)
            elif fence_lang in python_langs:
                _extract_python_block_claims(line, rel_posix, line_no, doc)
            continue

        h_m = _HEADING_RE.match(line)
        if h_m:
            doc.headings.append((line_no, h_m.group(2).strip()))

        _extract_toolchain_claims(line, rel_posix, line_no, doc)

        # Single-pass O(n) Markdown link extraction
        for raw_target_link in _extract_markdown_links(line):
            norm = _normalize_candidate_path(
                raw_target_link,
                doc_rel_dir,
                is_markdown_link=True,
            )
            if norm:
                doc.path_claims.append(
                    PathClaim(
                        file=rel_posix,
                        line=line_no,
                        raw_path=raw_target_link,
                        normalized=norm,
                        is_markdown_link=True,
                    )
                )

        # Inline backtick spans
        for code_m in _INLINE_CODE_RE.finditer(line):
            span = code_m.group(1).strip()
            if not span:
                continue

            # Explicit inline executable commands (skip negated prose mentions)
            if _INLINE_CMD_PREFIX_RE.match(span):
                prefix_text = line[: code_m.start()]
                if not _INLINE_NEGATED_PREFIX_RE.search(prefix_text):
                    _extract_shell_claims(span, rel_posix, line_no, doc)
                continue

            # Explicit `mod:Symbol` or `pkg.mod:Symbol`
            colon_m = _INLINE_COLON_SYM_RE.match(span)
            if colon_m:
                doc.symbol_claims.append(
                    SymbolClaim(
                        file=rel_posix,
                        line=line_no,
                        raw_ref=span,
                        module=colon_m.group(1),
                        symbol=colon_m.group(2),
                    )
                )
                continue

            # Explicit `mod.Symbol` / `pkg.mod.Symbol` / `mod.func()`
            dotted_m = _INLINE_DOTTED_SYM_RE.match(span)
            if dotted_m:
                last_seg = dotted_m.group(2)
                if last_seg.lower() not in _KNOWN_EXTENSIONS:
                    doc.symbol_claims.append(
                        SymbolClaim(
                            file=rel_posix,
                            line=line_no,
                            raw_ref=span,
                            module=dotted_m.group(1),
                            symbol=last_seg,
                        )
                    )
                    continue

            # Explicit env var like `MY_SERVICE_URL`
            env_m = _INLINE_ENV_RE.match(span)
            if env_m:
                var = env_m.group(1)
                if var not in _BUILTIN_ENV_VARS and not var.startswith(("DOC0", "DIET0")):
                    doc.env_claims.append(
                        EnvClaim(file=rel_posix, line=line_no, name=var, raw_text=span)
                    )
                continue

            norm = _normalize_candidate_path(span, doc_rel_dir, is_markdown_link=False)
            if norm:
                doc.path_claims.append(
                    PathClaim(
                        file=rel_posix,
                        line=line_no,
                        raw_path=span,
                        normalized=norm,
                        is_markdown_link=False,
                    )
                )

    return doc
