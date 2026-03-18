# AGENTS.md

This repository is a small Python 3.13 service that exposes a FastAPI wrapper around Nagios `statusjson.cgi` using `aiohttp` and `uvicorn`.

Use `uv` for dependency management and project commands. For local startup, prefer `mise run start`, which currently runs `uv run uvicorn nagios_status_api.__init__:app --host 0.0.0.0 --port 8000`.

The main application logic currently lives in `nagios_status_api/__init__.py`. Keep changes simple and direct. Prefer small refactors inside the existing module before splitting code into new files.

Runtime configuration is environment-driven:
- `NAGIOS_BASE_URL`
- `NAGIOS_CLIENT_CERT`
- `NAGIOS_CLIENT_KEY`
- `NAGIOS_CA_CERT` (optional)
- `NAGIOS_TIMEOUT`
- `NAGIOS_VERIFY_SSL`

Preserve the current mutual TLS behavior when changing backend access. Keep API additions aligned with the existing `/api/v1/...` route shape unless the task explicitly requires a redesign.

There is no real test suite yet. If you add behavior, add focused tests instead of relying only on manual verification.
