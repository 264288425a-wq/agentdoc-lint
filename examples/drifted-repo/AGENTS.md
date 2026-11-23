# AGENTS.md

## Architecture

- Core API server lives in `src/sample_service/app.py` via `sample_service.server.TokenValidator.verify_jwt()`.
- Configure `OLD_JWT_SECRET` before running local tests.

## Build & Test

```bash
uv run sample-api
make test-unit
```
