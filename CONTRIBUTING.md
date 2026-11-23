# Contributing to agentdoc-lint

Thank you for helping keep coding-agent instructions trustworthy and drift-free!

## Core Engineering Principles

- **Zero required runtime dependencies**: Core fact extraction, linting, and reporting must run on the Python 3.10+ standard library.
- **Never execute untrusted repo code**: Verify commands and symbols statically via AST and project manifests (`pyproject.toml`, `Makefile`, `package.json`, workflows).
- **Low false positives**: When in doubt, prefer precision over noisy warnings. Every diagnostic must point to an exact file and line number and include a helpful suggestion.

## Local Development

```bash
git clone https://github.com/264288425a-wq/agentdoc-lint.git
cd agentdoc-lint
python -m venv .venv
pip install -e ".[dev]"
```

## Verification Commands

```bash
# Built-in unittest suite (works with zero dev dependencies)
PYTHONPATH=src python -m unittest discover -s tests -v

# Self-dogfood check on this repository
PYTHONPATH=src python -m agentdoc_lint check .

# Optional dev checks
pytest
ruff check src tests
mypy src
```

## Pull Request Process

1. Open an issue before adding a new lint rule (`DOC009+`) or breaking CLI change.
2. Add focused unit tests in `tests/` covering both positive matches and false-positive edge cases.
3. Ensure `agentdoc-lint check .` passes on this repository itself.
