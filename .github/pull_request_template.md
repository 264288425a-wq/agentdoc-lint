## Summary

Describe the change and why it improves `agentdoc-lint`.

## Checklist

- [ ] Added or updated tests in `tests/` (including false-positive edge cases if touching parsers/rules)
- [ ] Ran `PYTHONPATH=src python -m unittest discover -s tests -v`
- [ ] Ran `PYTHONPATH=src python -m agentdoc_lint check . --ignore-dir examples --fail-on warning`
- [ ] Updated `AGENTS.md` / `README.md` / `CHANGELOG.md` if behavior changed
