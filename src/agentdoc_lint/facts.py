"""Static codebase fact extraction (filesystem, Python AST, manifests, env vars, lockfiles)."""

from __future__ import annotations

import ast
import importlib
import json
import os
import re
import stat
import sys
import tokenize
from collections import defaultdict
from pathlib import Path
from typing import Any

from agentdoc_lint.models import RepoFacts, ScopeManifests

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        "dist",
        "build",
        ".eggs",
        "htmlcov",
        ".next",
        ".nuxt",
        "target",
    }
)

_MAKE_RULE_RE = re.compile(r"^([^:=#]+?)\s*:(?!=)")
_MAKE_TARGET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,})\s*=")
_WORKFLOW_ENV_RE = re.compile(r"\b(?:env|secrets)\.([A-Z][A-Z0-9_]{1,})\b")
_YAML_ENV_KEY_RE = re.compile(r"^\s+([A-Z][A-Z0-9_]{2,})\s*:")
_TOML_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_TOML_KEY_RE = re.compile(r'^\s*([A-Za-z0-9_.-]+|"[^"]+"|\'[^\']+\')\s*=(.*)$')

_SECRET_ENV_NAMES: frozenset[str] = frozenset({".env", ".env.local", ".env.production"})
_ALLOWED_ENV_TEMPLATES: frozenset[str] = frozenset(
    {".env.example", ".env.sample", ".env.template", "env.example"}
)


def validate_root_dir(root: str | Path) -> Path:
    """Validate that `root` exists, is a readable directory, and return its canonical path."""
    raw_path = Path(root)
    if not raw_path.exists():
        raise FileNotFoundError(f"Repository root does not exist: {root}")
    if not raw_path.is_dir():
        raise NotADirectoryError(f"Repository root is not a directory: {root}")
    resolved = Path(os.path.realpath(raw_path))
    # Verify directory can actually be listed
    os.listdir(resolved)
    return resolved


def is_link_or_junction(path: Path) -> bool:
    """Return True if `path` is a symlink or Windows directory junction / reparse point."""
    try:
        if path.is_symlink():
            return True
        isjunction_fn = getattr(os.path, "isjunction", None)
        if callable(isjunction_fn) and bool(isjunction_fn(path)):
            return True
        st = os.lstat(path)
        file_attrs = getattr(st, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_flag and (file_attrs & reparse_flag):
            return True
    except OSError:
        return True
    return False


def is_within_root(path: Path, root_real: Path) -> bool:
    """Return True if `path`'s canonical realpath resides within `root_real`."""
    try:
        real = Path(os.path.realpath(path))
        return real == root_real or real.is_relative_to(root_real)
    except OSError:
        return False


def is_safe_repo_file(path: Path, root_real: Path) -> bool:
    """Ensure `path` is a regular, non-link file inside `root_real` and not a secret `.env`."""
    name = path.name.lower()
    if (name in _SECRET_ENV_NAMES or name.startswith(".env.")) and (
        name not in _ALLOWED_ENV_TEMPLATES
    ):
        return False
    if is_link_or_junction(path):
        return False
    if not is_within_root(path, root_real):
        return False
    try:
        st = os.lstat(path)
        return stat.S_ISREG(st.st_mode)
    except OSError:
        return False


def _strip_toml_comment(line: str) -> str:
    """Remove trailing TOML `#` comment outside single or double quotes."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_single and not in_double:
            return line[:idx]
    return line


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split a TOML array/inline-table body by top-level commas respecting quotes and nesting."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    escaped = False

    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\" and in_double:
            buf.append(ch)
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            buf.append(ch)
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            buf.append(ch)
            continue
        if not in_single and not in_double:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth = max(0, depth - 1)
            elif ch == sep and depth == 0:
                parts.append("".join(buf).strip())
                buf = []
                continue
        buf.append(ch)

    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _unquote_toml_str(raw: str) -> str:
    val = raw.strip()
    if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        try:
            parsed = json.loads(val)
            return str(parsed)
        except json.JSONDecodeError:
            return val[1:-1]
    if len(val) >= 2 and val[0] == "'" and val[-1] == "'":
        return val[1:-1]
    return val


def _parse_toml_value(raw: str) -> Any:
    """Parse TOML scalar, string array `[...]`, or inline table `{...}` for fallback mode."""
    val = raw.strip()
    if not val:
        return ""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [_parse_toml_value(item) for item in _split_top_level(inner) if item]
    if val.startswith("{") and val.endswith("}"):
        inner = val[1:-1].strip()
        table: dict[str, Any] = {}
        if not inner:
            return table
        for entry in _split_top_level(inner):
            if "=" not in entry:
                continue
            k_raw, v_raw = entry.split("=", 1)
            key = _unquote_toml_str(k_raw)
            if key:
                table[key] = _parse_toml_value(v_raw)
        return table
    return _unquote_toml_str(val)


def _bracket_balance(text: str) -> int:
    """Return net open bracket/brace depth outside quotes."""
    balance = 0
    in_single = False
    in_double = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif not in_single and not in_double:
            if ch in "[{":
                balance += 1
            elif ch in "]}":
                balance -= 1
    return balance


def _minimal_toml_fallback(text: str) -> dict[str, Any]:
    """Zero-dependency fallback TOML parser for Python 3.10 when `tomli` is unavailable.

    Supports section tables, dotted keys, string arrays (single & multi-line), and inline tables.
    """
    result: dict[str, Any] = {}
    current_path: list[str] = []
    lines = text.splitlines()
    idx = 0

    while idx < len(lines):
        cleaned = _strip_toml_comment(lines[idx]).strip()
        idx += 1
        if not cleaned:
            continue

        sec_m = _TOML_SECTION_RE.match(cleaned)
        if sec_m:
            raw_sec = sec_m.group(1).strip()
            if raw_sec.startswith("["):
                continue
            current_path = [_unquote_toml_str(part) for part in raw_sec.split(".") if part.strip()]
            node: dict[str, Any] = result
            for part in current_path:
                existing = node.get(part)
                if not isinstance(existing, dict):
                    existing = {}
                    node[part] = existing
                node = existing
            continue

        key_m = _TOML_KEY_RE.match(cleaned)
        if not key_m:
            continue

        raw_key = key_m.group(1).strip()
        raw_val = key_m.group(2).strip()

        # Accumulate multi-line arrays / inline tables
        while _bracket_balance(raw_val) > 0 and idx < len(lines):
            next_clean = _strip_toml_comment(lines[idx]).strip()
            idx += 1
            if next_clean:
                raw_val = f"{raw_val} {next_clean}"

        node = result
        for part in current_path:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing

        key_parts = [_unquote_toml_str(p) for p in raw_key.split(".") if p.strip()]
        for sub in key_parts[:-1]:
            existing = node.get(sub)
            if not isinstance(existing, dict):
                existing = {}
                node[sub] = existing
            node = existing
        if key_parts:
            node[key_parts[-1]] = _parse_toml_value(raw_val)

    return result


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse TOML via stdlib `tomllib` (3.11+), optional `tomli`, or fallback parser."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if sys.version_info >= (3, 11):
        import tomllib

        return dict(tomllib.loads(text))
    try:
        tomli_mod: Any = importlib.import_module("tomli")
        loaded = tomli_mod.loads(text)
        return dict(loaded) if isinstance(loaded, dict) else {}
    except ImportError:
        return _minimal_toml_fallback(text)


def _module_name_from_rel_path(rel_posix: str) -> str | None:
    """Derive a dotted Python module name from a repo-relative `.py` path."""
    if not rel_posix.endswith(".py"):
        return None
    parts = list(Path(rel_posix).parts)
    if not parts:
        return None
    if parts[0] in ("src", "lib") and len(parts) > 1:
        parts = parts[1:]
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    if not parts:
        return None
    if not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


def _extract_python_ast_facts(
    py_file: Path,
    rel_posix: str,
    facts: RepoFacts,
    symbol_index: dict[str, set[str]],
) -> None:
    """Extract Python module names, symbols, and env var reads via `tokenize.open` + AST."""
    try:
        with tokenize.open(py_file) as handle:
            source = handle.read()
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError):
        facts.skipped_python_files.append(f"{rel_posix} (EncodingError)")
        return

    try:
        tree = ast.parse(source, filename=rel_posix)
    except (SyntaxError, RecursionError, ValueError) as exc:
        facts.skipped_python_files.append(f"{rel_posix} ({type(exc).__name__})")
        return

    mod_name = _module_name_from_rel_path(rel_posix)
    symbols: set[str] = set()

    if mod_name:
        facts.python_modules.add(mod_name)
        root_pkg = mod_name.split(".", 1)[0]
        if root_pkg != "tests":
            facts.python_root_packages.add(root_pkg)
        segments = mod_name.split(".")
        for i in range(1, len(segments)):
            facts.python_modules.add(".".join(segments[:i]))

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(stmt.name)
            symbol_index[stmt.name].add(rel_posix)
        elif isinstance(stmt, ast.ClassDef):
            symbols.add(stmt.name)
            symbol_index[stmt.name].add(rel_posix)
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = f"{stmt.name}.{item.name}"
                    symbols.add(qualified)
                    symbol_index[qualified].add(rel_posix)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            symbols.add(stmt.target.id)
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                exported = alias.asname or alias.name
                if exported != "*":
                    symbols.add(exported)

    if mod_name:
        existing = facts.module_symbols.setdefault(mod_name, set())
        existing.update(symbols)

    for expr in ast.walk(tree):
        if isinstance(expr, ast.Call):
            func = expr.func
            is_env_call = isinstance(func, ast.Attribute) and (
                (
                    func.attr == "getenv"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                or (
                    func.attr == "get"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
            )
            if is_env_call and expr.args:
                arg0 = expr.args[0]
                if (
                    isinstance(arg0, ast.Constant)
                    and isinstance(arg0.value, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{1,}", arg0.value)
                ):
                    facts.env_vars.add(arg0.value)
        elif (
            isinstance(expr, ast.Subscript)
            and isinstance(expr.value, ast.Attribute)
            and expr.value.attr == "environ"
            and isinstance(expr.slice, ast.Constant)
            and isinstance(expr.slice.value, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{1,}", expr.slice.value)
        ):
            facts.env_vars.add(expr.slice.value)


def _extract_pyproject_facts(pyproject_path: Path, scope: ScopeManifests) -> None:
    scope.has_pyproject = True
    try:
        data = _load_toml(pyproject_path)
    except Exception:
        return

    project = data.get("project")
    if isinstance(project, dict):
        scripts = project.get("scripts")
        if isinstance(scripts, dict):
            scope.pyproject_scripts.update(str(k) for k in scripts)

    tool = data.get("tool")
    if isinstance(tool, dict):
        if "uv" in tool:
            scope.detected_toolchains.setdefault("python_pm", set()).add("uv")
        if "poetry" in tool:
            scope.detected_toolchains.setdefault("python_pm", set()).add("poetry")
            poetry_tbl = tool["poetry"]
            if isinstance(poetry_tbl, dict) and isinstance(poetry_tbl.get("scripts"), dict):
                scope.pyproject_scripts.update(str(k) for k in poetry_tbl["scripts"])
        if "pdm" in tool:
            scope.detected_toolchains.setdefault("python_pm", set()).add("pdm")
        if "ruff" in tool:
            scope.detected_toolchains.setdefault("python_linter", set()).add("ruff")
        if "black" in tool:
            scope.detected_toolchains.setdefault("python_linter", set()).add("black")
        if "flake8" in tool:
            scope.detected_toolchains.setdefault("python_linter", set()).add("flake8")
        if "mypy" in tool:
            scope.detected_toolchains.setdefault("python_typechecker", set()).add("mypy")
        if "pyright" in tool:
            scope.detected_toolchains.setdefault("python_typechecker", set()).add("pyright")

        pytest_cfg = tool.get("pytest")
        if isinstance(pytest_cfg, dict):
            ini = pytest_cfg.get("ini_options")
            if isinstance(ini, dict) and isinstance(ini.get("markers"), list):
                for entry in ini["markers"]:
                    if isinstance(entry, str) and entry.strip():
                        marker_name = entry.split(":", 1)[0].split("(", 1)[0].strip()
                        if marker_name:
                            scope.pytest_markers.add(marker_name)


def _extract_makefile_facts(makefile_path: Path, scope: ScopeManifests) -> None:
    """Extract all literal targets from explicit GNU Make rules (including multi-target rules)."""
    scope.has_makefile = True
    try:
        text = makefile_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        if not line or line.startswith(("\t", "#", " ")):
            continue
        m = _MAKE_RULE_RE.match(line)
        if not m:
            continue
        lhs = m.group(1).strip()
        for token in lhs.split():
            if token.startswith(".") or any(ch in token for ch in ("%", "$", "*", "?")):
                continue
            if _MAKE_TARGET_TOKEN_RE.match(token):
                scope.makefile_targets.add(token)


def _extract_package_json_facts(pkg_path: Path, scope: ScopeManifests) -> None:
    scope.has_package_json = True
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        scope.package_scripts.update(str(k) for k in scripts)
    pm = data.get("packageManager")
    if isinstance(pm, str):
        prefix = pm.split("@", 1)[0].strip().lower()
        if prefix in ("npm", "pnpm", "yarn", "bun"):
            scope.detected_toolchains.setdefault("js_pm", set()).add(prefix)


def _extract_env_template_facts(env_path: Path, facts: RepoFacts) -> None:
    try:
        text = env_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        m = _ENV_LINE_RE.match(line)
        if m:
            facts.env_vars.add(m.group(1))


def _extract_workflow_env_facts(wf_path: Path, facts: RepoFacts) -> None:
    try:
        text = wf_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return
    for m in _WORKFLOW_ENV_RE.finditer(text):
        facts.env_vars.add(m.group(1))
    in_env_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "env:":
            in_env_block = True
            continue
        if in_env_block:
            if not line.startswith(" ") and stripped:
                in_env_block = False
                continue
            m_key = _YAML_ENV_KEY_RE.match(line)
            if m_key:
                facts.env_vars.add(m_key.group(1))


def _apply_scope_manifest_file(fname: str, fpath: Path, scope: ScopeManifests) -> bool:
    """Parse a manifest/lockfile into `scope`. Returns True if file was a known manifest."""
    if fname == "pyproject.toml":
        _extract_pyproject_facts(fpath, scope)
    elif fname in ("Makefile", "GNUmakefile"):
        _extract_makefile_facts(fpath, scope)
    elif fname == "package.json":
        _extract_package_json_facts(fpath, scope)
    elif fname == "uv.lock":
        scope.detected_toolchains.setdefault("python_pm", set()).add("uv")
    elif fname == "poetry.lock":
        scope.detected_toolchains.setdefault("python_pm", set()).add("poetry")
    elif fname == "pdm.lock":
        scope.detected_toolchains.setdefault("python_pm", set()).add("pdm")
    elif fname == "Pipfile.lock":
        scope.detected_toolchains.setdefault("python_pm", set()).add("pipenv")
    elif fname == "pnpm-lock.yaml":
        scope.detected_toolchains.setdefault("js_pm", set()).add("pnpm")
    elif fname == "yarn.lock":
        scope.detected_toolchains.setdefault("js_pm", set()).add("yarn")
    elif fname == "package-lock.json":
        scope.detected_toolchains.setdefault("js_pm", set()).add("npm")
    elif fname in ("bun.lock", "bun.lockb"):
        scope.detected_toolchains.setdefault("js_pm", set()).add("bun")
    elif fname in ("ruff.toml", ".ruff.toml"):
        scope.detected_toolchains.setdefault("python_linter", set()).add("ruff")
    elif fname == "pyrightconfig.json":
        scope.detected_toolchains.setdefault("python_typechecker", set()).add("pyright")
    elif fname in ("mypy.ini", ".mypy.ini"):
        scope.detected_toolchains.setdefault("python_typechecker", set()).add("mypy")
    else:
        return False
    return True


def collect_repo_facts(
    root: str | Path,
    *,
    extra_ignore_dirs: set[str] | None = None,
) -> RepoFacts:
    """Statically walk `root` and extract ground-truth repository and subproject facts."""
    root_real = validate_root_dir(root)
    ignore_dirs = set(DEFAULT_IGNORE_DIRS)
    if extra_ignore_dirs:
        ignore_dirs.update(extra_ignore_dirs)

    facts = RepoFacts(root=root_real)
    symbol_index: dict[str, set[str]] = defaultdict(set)

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

        rel_dir = cur_dir.relative_to(root_real).as_posix()
        if rel_dir != ".":
            facts.all_dirs.add(rel_dir)

        scope = ScopeManifests(dir_path=rel_dir)
        scope_touched = False

        for fname in sorted(filenames):
            fpath = cur_dir / fname
            if not is_safe_repo_file(fpath, root_real):
                continue

            rel_posix = fpath.relative_to(root_real).as_posix()
            facts.all_files.add(rel_posix)

            if fname.endswith(".py"):
                _extract_python_ast_facts(fpath, rel_posix, facts, symbol_index)

            if _apply_scope_manifest_file(fname, fpath, scope):
                scope_touched = True
            elif fname.lower() in _ALLOWED_ENV_TEMPLATES:
                _extract_env_template_facts(fpath, facts)
            elif rel_dir == ".github/workflows" and fname.endswith((".yml", ".yaml")):
                _extract_workflow_env_facts(fpath, facts)

        if scope_touched or rel_dir == ".":
            facts.scoped_manifests[rel_dir] = scope

    # Mirror root scope onto top-level RepoFacts fields for backward compatibility
    root_scope = facts.scoped_manifests.get(".")
    if root_scope is not None:
        facts.has_pyproject = root_scope.has_pyproject
        facts.has_makefile = root_scope.has_makefile
        facts.has_package_json = root_scope.has_package_json
        facts.pyproject_scripts = set(root_scope.pyproject_scripts)
        facts.pytest_markers = set(root_scope.pytest_markers)
        facts.makefile_targets = set(root_scope.makefile_targets)
        facts.package_scripts = set(root_scope.package_scripts)
        facts.detected_toolchains = {
            cat: set(tools) for cat, tools in root_scope.detected_toolchains.items()
        }

    facts.all_python_symbols = {k: sorted(v) for k, v in symbol_index.items()}
    return facts
