# nagios-status-api

Small FastAPI wrapper around Nagios `statusjson.cgi` with:

- JSON API endpoints under `/api/v1/...`
- simple browseable HTML pages for hosts and services
- a CLI that queries the same backend with the same config

## Requirements

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/)
- Nagios reachable over HTTPS with a client certificate/key

## Setup

Install dependencies:

```bash
uv sync
```

Set the required environment variables. `envrc.example` shows the expected shape:

```bash
export NAGIOS_BASE_URL="https://nagios.example.com/nagios/cgi-bin"
export NAGIOS_CLIENT_CERT="./client.crt"
export NAGIOS_CLIENT_KEY="./client.key"
export NAGIOS_VERIFY_SSL="true"
export NAGIOS_TIMEOUT="10"
```

Optional:

```bash
export NAGIOS_CA_CERT="./ca.crt"
```

## Running

Run the API server:

```bash
uv run nagios-status-api-server
```

Enable reload for local development:

```bash
uv run nagios-status-api-server --reload
```

If you use `mise`, the repo also includes:

```bash
mise run start
```

The server validates the Nagios connection during startup and exits early if the current config or TLS setup is broken.

## CLI

The CLI uses the same environment variables and backend logic as the API server.

Examples:

```bash
uv run nagios-status-api programstatus
uv run nagios-status-api hosts
uv run nagios-status-api host localhost
uv run nagios-status-api services --host-name localhost
uv run nagios-status-api service localhost PING
```

## Web UI and API

Useful pages:

- `/` index page
- `/browse/hosts` host browser
- `/browse/services` service browser

Useful JSON endpoints:

- `/api/v1/programstatus`
- `/api/v1/hosts`
- `/api/v1/hosts/{host_name}`
- `/api/v1/services`
- `/api/v1/services/{host_name}/{service_description}`

## Notes

- Host and service status values are converted to readable text in both the API/browser output and the CLI.
- Raw JSON sections in the HTML pages are collapsed by default.
- Static CSS and JavaScript are served from `/static` so the pages work with stricter CSP settings.
