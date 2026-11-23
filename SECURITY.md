# Security Policy

## Supported Versions

We actively release security updates for the latest minor version of `agentdoc-lint`.

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Threat Model & Privacy Boundaries

1. **100% Static & Offline by Default**: `agentdoc-lint check` and `agentdoc-lint facts` inspect repository files purely via static parsing (`ast`, `tomllib`, `json`, regex). It **never executes** untrusted repository scripts or shell commands found inside Markdown files.
2. **Opt-in AI Patch Suggestions**: Outbound network requests occur **only** when the user explicitly passes `--suggest-fix` with `OPENAI_API_KEY` set. Even then, only the failing diagnostics and concise excerpts of the affected instruction files are sent.
3. **No Secret Harvesting**: Environment variable checks (`DOC004`) inspect variable *names* in `.env.example`, workflows, and AST calls (`os.getenv(...)`); `agentdoc-lint` intentionally skips `.env` files containing real secrets.

## Reporting a Vulnerability

Please **do not** disclose security vulnerabilities in public issues.

Report vulnerabilities privately through GitHub's **Private Vulnerability Reporting** ("Security" tab -> "Report a vulnerability").

We target acknowledgement within **48 hours** and a remediation plan within **7 days**.
