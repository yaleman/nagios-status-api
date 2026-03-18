from __future__ import annotations

import argparse
import asyncio
from enum import IntEnum
import html
import json
import logging
import os
import ssl
import sys
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).with_name("static")
APP_TITLE = "Nagios Status API"

INDEX_ROUTES = [
    ("/healthz", "Health check"),
    ("/docs", "FastAPI OpenAPI docs"),
    ("/browse/hosts", "Browse hosts as an HTML table"),
    ("/browse/hosts/{host_name}", "Browse one host as an HTML status page"),
    ("/browse/services", "Browse services as an HTML table"),
    (
        "/browse/services/{host_name}/{service_description}",
        "Browse one service as an HTML status page",
    ),
]


class HostState(IntEnum):
    UP = 0
    DOWN = 1
    UNREACHABLE = 2


class HostListState(IntEnum):
    PENDING = 1
    UP = 2
    DOWN = 4
    UNREACHABLE = 8


class ServiceState(IntEnum):
    OK = 0
    WARNING = 1
    CRITICAL = 2
    UNKNOWN = 3


class ServiceListState(IntEnum):
    PENDING = 1
    OK = 2
    WARNING = 4
    UNKNOWN = 8
    CRITICAL = 16


class StateType(IntEnum):
    SOFT = 0
    HARD = 1


HOST_STATUS_NAMES = {
    0: "up",
    1: "pending",
    2: "up",
    4: "down",
    8: "unreachable",
}

HOSTLIST_STATUS_NAMES = {
    1: "pending",
    2: "up",
    4: "down",
    8: "unreachable",
}

SERVICE_STATUS_NAMES = {
    0: "ok",
    1: "warning",
    2: "critical",
    3: "unknown",
}

SERVICE_LIST_STATUS_NAMES = {
    1: "pending",
    2: "ok",
    4: "warning",
    8: "unknown",
    16: "critical",
}


class Settings(BaseModel):
    nagios_base_url: str = Field(
        default=os.getenv(
            "NAGIOS_BASE_URL", "https://nagios.example.com/nagios/cgi-bin"
        )
    )
    client_cert_path: str = Field(
        default=os.getenv("NAGIOS_CLIENT_CERT", "./client.crt")
    )
    client_key_path: str = Field(default=os.getenv("NAGIOS_CLIENT_KEY", "./client.key"))
    ca_cert_path: Optional[str] = Field(default=os.getenv("NAGIOS_CA_CERT"))
    request_timeout_seconds: float = Field(
        default=float(os.getenv("NAGIOS_TIMEOUT", "10"))
    )
    verify_ssl: bool = Field(
        default=os.getenv("NAGIOS_VERIFY_SSL", "true").lower()
        in {"1", "true", "yes", "on"}
    )


settings = Settings()


class StartupConfigurationError(RuntimeError):
    pass


def validate_backend_settings(cfg: Settings) -> None:
    parsed = urlparse(cfg.nagios_base_url)

    if not parsed.hostname:
        raise StartupConfigurationError(
            "Startup failed: NAGIOS_BASE_URL must include a real backend hostname."
        )

    if parsed.hostname == "nagios.example.com":
        raise StartupConfigurationError(
            "Startup failed: NAGIOS_BASE_URL is still set to the example hostname nagios.example.com."
        )


def build_ssl_context(cfg: Settings) -> ssl.SSLContext:
    if cfg.verify_ssl:
        ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if cfg.ca_cert_path:
            ssl_ctx.load_verify_locations(cafile=cfg.ca_cert_path)
    else:
        ssl_ctx = ssl._create_unverified_context()

    ssl_ctx.load_cert_chain(
        certfile=cfg.client_cert_path,
        keyfile=cfg.client_key_path,
    )
    return ssl_ctx


def get_state_enum(query: Optional[str]):
    if query == "host":
        return HostState
    if query == "hostlist":
        return HostListState
    if query == "service":
        return ServiceState
    if query == "servicelist":
        return ServiceListState
    return None


def enum_name(enum_cls: type[IntEnum], value: Any) -> Optional[str]:
    if not isinstance(value, int):
        return None

    try:
        return enum_cls(value).name.lower()
    except ValueError:
        return None


def enum_value_by_name(enum_cls: type[IntEnum], value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None

    try:
        return int(enum_cls[value.upper()])
    except KeyError:
        return None


def state_name_for_query(query: Optional[str], value: Any) -> Optional[str]:
    if not isinstance(value, int):
        return None

    if query == "host":
        return HOST_STATUS_NAMES.get(value)
    if query == "hostlist":
        return HOSTLIST_STATUS_NAMES.get(value)
    if query == "service":
        return SERVICE_STATUS_NAMES.get(value)
    if query == "servicelist":
        return SERVICE_LIST_STATUS_NAMES.get(value)

    return None


def status_text_from_record(query: Optional[str], data: dict[str, Any]) -> str:
    for key in ("current_state_text", "last_hard_state_text", "state_text"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value

    for key in ("current_state", "last_hard_state", "state"):
        value = data.get(key)
        name = state_name_for_query(query, value)
        if name is not None:
            return name

    return "unknown"


def humanize_status_fields(
    payload: dict[str, Any], query: Optional[str]
) -> dict[str, Any]:
    state_enum = get_state_enum(query)

    def transform(value: Any, key_name: Optional[str] = None) -> Any:
        if isinstance(value, list):
            return [transform(item, key_name) for item in value]

        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if (
                    key in {"hostlist", "servicelist"}
                    and isinstance(item, dict)
                    and state_enum
                ):
                    result[key] = {
                        item_key: transform(item_value, key)
                        for item_key, item_value in item.items()
                    }
                    continue

                if key_name == "servicelist" and isinstance(item, dict) and state_enum:
                    result[key] = {
                        item_key: transform(item_value, "servicelist")
                        for item_key, item_value in item.items()
                    }
                    continue

                if key_name == "servicelist" and state_enum:
                    result[key] = transform(item, "servicelist")
                    continue

                result[key] = transform(item, key)

                if key in {"current_state", "last_hard_state", "state"} and state_enum:
                    name = state_name_for_query(query, item)
                    if name is not None:
                        result[f"{key}_text"] = name

                if key == "state_type":
                    name = enum_name(StateType, item)
                    if name is not None:
                        result["state_type_text"] = name

            return result

        if key_name in {"hostlist", "servicelist"} and state_enum:
            name = state_name_for_query(query, value)
            if name is not None:
                return name

        return value

    return transform(payload)


def html_page(title: str, body: str) -> HTMLResponse:
    safe_title = html.escape(title)
    nav = (
        '<nav class="topnav">'
        '<a href="/">Home</a>'
        '<a href="/browse/hosts">Hosts</a>'
        '<a href="/browse/services">Services</a>'
        '<a href="/docs">Docs</a>'
        '<a href="/healthz">Healthz</a>'
        "</nav>"
    )
    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_title}</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <header class="topbar">
    <h1><a class="brand-link" href="/">{APP_TITLE}</a></h1>
    {nav}
  </header>
  {body}
  <script src="/static/app.js"></script>
</body>
</html>"""
    return HTMLResponse(content=content)


def build_sort_links(
    base_path: str, sort_key: str, current_sort: str, current_dir: str
) -> str:
    up_query = urlencode({"sort": sort_key, "dir": "asc"})
    down_query = urlencode({"sort": sort_key, "dir": "desc"})
    up = f'<a href="{base_path}?{up_query}">⬆️</a>'
    down = f'<a href="{base_path}?{down_query}">⬇️</a>'
    indicator = ""
    if current_sort == sort_key:
        indicator = " ↑" if current_dir == "asc" else " ↓"
    return f"{up} {down}{indicator}"


def render_host_status_table(
    hosts: dict[str, Any],
    sort_by: str,
    sort_dir: str,
) -> str:
    reverse = sort_dir == "desc"
    items = list(hosts.items())

    if sort_by == "status":
        items.sort(
            key=lambda item: (
                enum_value_by_name(HostListState, item[1])
                if item[1] is not None
                else -1,
                item[0].lower(),
            ),
            reverse=reverse,
        )
    else:
        items.sort(key=lambda item: item[0].lower(), reverse=reverse)

    rows = "".join(
        "<tr>"
        f'<td><a href="/browse/hosts/{quote(host_name, safe="")}">{html.escape(host_name)}</a></td>'
        f'<td class="status {html.escape(str(status))}">{html.escape(str(status))}</td>'
        "</tr>"
        for host_name, status in items
    )
    return (
        "<h1>Hosts</h1>"
        '<p><a href="/">Back to index</a></p>'
        "<table>"
        "<thead><tr>"
        f"<th>Host {build_sort_links('/browse/hosts', 'host', sort_by, sort_dir)}</th>"
        f"<th>Status {build_sort_links('/browse/hosts', 'status', sort_by, sort_dir)}</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def flatten_services(services: dict[str, Any]) -> list[tuple[str, str, Any]]:
    rows: list[tuple[str, str, Any]] = []
    for outer_key, outer_value in services.items():
        if isinstance(outer_value, dict):
            for service_name, status in outer_value.items():
                rows.append((outer_key, service_name, status))
            continue

        if ";" in outer_key:
            host_name, service_name = outer_key.split(";", 1)
            rows.append((host_name, service_name, outer_value))
            continue

        rows.append(("", outer_key, outer_value))

    return rows


def render_service_status_table(
    services: dict[str, Any],
    sort_by: str,
    sort_dir: str,
) -> str:
    reverse = sort_dir == "desc"
    items = flatten_services(services)

    if sort_by == "status":
        items.sort(
            key=lambda item: (
                enum_value_by_name(ServiceListState, item[2])
                if item[2] is not None
                else -1,
                item[0].lower(),
                item[1].lower(),
            ),
            reverse=reverse,
        )
    elif sort_by == "service":
        items.sort(key=lambda item: (item[1].lower(), item[0].lower()), reverse=reverse)
    else:
        items.sort(key=lambda item: (item[0].lower(), item[1].lower()), reverse=reverse)

    rows = "".join(
        "<tr>"
        f"<td>{html.escape(host_name)}</td>"
        f'<td><a href="/browse/services/{quote(host_name, safe="")}/{quote(service_name, safe="")}">{html.escape(service_name)}</a></td>'
        f'<td class="status {html.escape(str(status))}">{html.escape(str(status))}</td>'
        "</tr>"
        for host_name, service_name, status in items
    )
    return (
        "<h1>Services</h1>"
        '<p><a href="/">Back to index</a></p>'
        "<table>"
        "<thead><tr>"
        f"<th>Host {build_sort_links('/browse/services', 'host', sort_by, sort_dir)}</th>"
        f"<th>Service {build_sort_links('/browse/services', 'service', sort_by, sort_dir)}</th>"
        f"<th>Status {build_sort_links('/browse/services', 'status', sort_by, sort_dir)}</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def render_host_services_table(host_name: str, services: dict[str, Any]) -> str:
    if host_name in services and isinstance(services[host_name], dict):
        services = services[host_name]

    def display_status(value: Any) -> str:
        if isinstance(value, dict):
            return status_text_from_record("service", value)
        if isinstance(value, int):
            return state_name_for_query("servicelist", value) or str(value)
        return str(value)

    rows = "".join(
        "<tr>"
        f'<td><a href="/browse/services/{quote(host_name, safe="")}/{quote(service_name, safe="")}">{html.escape(service_name)}</a></td>'
        f'<td class="status {html.escape(display_status(status))}">{html.escape(display_status(status))}</td>'
        "</tr>"
        for service_name, status in sorted(
            services.items(), key=lambda item: item[0].lower()
        )
    )
    return (
        "<h2>Services</h2>"
        "<table>"
        "<thead><tr><th>Service</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def render_dashboard_host_issues(hosts: dict[str, Any]) -> str:
    issues = [
        (host_name, status)
        for host_name, status in sorted(hosts.items(), key=lambda item: item[0].lower())
        if str(status).lower() != "up"
    ]

    if not issues:
        return ""

    rows = "".join(
        "<tr>"
        f'<td><a href="/browse/hosts/{quote(host_name, safe="")}">{html.escape(host_name)}</a></td>'
        f'<td class="status {html.escape(str(status).lower())}">{html.escape(str(status))}</td>'
        "</tr>"
        for host_name, status in issues
    )
    return (
        '<section class="dashboard-card">'
        "<table>"
        "<thead><tr><th>Host</th><th>Status</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</section>"
    )


def render_dashboard_service_issues(services: dict[str, Any]) -> str:
    grouped_rows: list[tuple[str, str, str, str]] = []
    for host_name, service_name, status in flatten_services(services):
        if isinstance(status, dict):
            status_text = status_text_from_record("service", status)
            status_info = ""
            for key in ("plugin_output", "status_information", "output", "long_plugin_output"):
                value = status.get(key)
                if isinstance(value, str) and value:
                    status_info = value
                    break
        else:
            status_text = str(status)
            status_info = ""

        if status_text.lower() == "ok":
            continue
        grouped_rows.append((host_name or "Unassigned", service_name, status_text, status_info))

    if not grouped_rows:
        return ""

    grouped_rows.sort(key=lambda item: (item[0].lower(), item[1].lower()))
    counts: dict[str, int] = {}
    for host_name, _, _, _ in grouped_rows:
        counts[host_name] = counts.get(host_name, 0) + 1

    seen_hosts: set[str] = set()
    rows = []
    for host_name, service_name, status_text, status_info in grouped_rows:
        cells = ["<tr>"]
        if host_name not in seen_hosts:
            host_link = (
                html.escape(host_name)
                if host_name == "Unassigned"
                else f'<a href="/browse/hosts/{quote(host_name, safe="")}">{html.escape(host_name)}</a>'
            )
            cells.append(f'<td rowspan="{counts[host_name]}" class="group-host">{host_link}</td>')
            seen_hosts.add(host_name)
        cells.append(
            f'<td><a href="/browse/services/{quote(host_name, safe="")}/{quote(service_name, safe="")}">{html.escape(service_name)}</a></td>'
        )
        cells.append(f'<td class="status {html.escape(status_text.lower())}">{html.escape(status_text)}</td>')
        cells.append(f"<td>{html.escape(status_info)}</td>")
        cells.append("</tr>")
        rows.append("".join(cells))

    return (
        '<section class="dashboard-card dashboard-service-card">'
        "<table>"
        "<thead><tr><th>Host</th><th>Service</th><th>Status</th><th>Status Information</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</section>"
    )


def render_key_value_rows(
    data: Any, sort_by: str, sort_dir: str, base_path: str
) -> str:
    if not isinstance(data, dict):
        return ""

    reverse = sort_dir == "desc"
    items = list(data.items())
    if sort_by == "value":
        items.sort(
            key=lambda item: json.dumps(item[1], sort_keys=True), reverse=reverse
        )
    else:
        items.sort(key=lambda item: str(item[0]).lower(), reverse=reverse)

    rows = "".join(
        "<tr>"
        f"<th>{html.escape(str(key))}</th>"
        f"<td>{html.escape(json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value))}</td>"
        "</tr>"
        for key, value in items
    )
    return (
        "<table>"
        "<thead><tr>"
        f"<th>Field {build_sort_links(base_path, 'field', sort_by, sort_dir)}</th>"
        f"<th>Value {build_sort_links(base_path, 'value', sort_by, sort_dir)}</th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


class ProgramStatusResult(BaseModel):
    query_time: int
    cgi: str
    user: str
    query: str
    query_status: str
    program_start: int
    last_data_update: int
    type_code: int
    type_text: str
    message: str


class ProgramStatusData(BaseModel):
    programstatus: ProgramStatusInner


class ProgramStatusInner(BaseModel):
    version: str
    nagios_pid: int
    daemon_mode: bool
    program_start: int
    last_log_rotation: int
    enable_notifications: bool
    execute_service_checks: bool
    accept_passive_service_checks: bool
    execute_host_checks: bool
    accept_passive_host_checks: bool
    enable_event_handlers: bool
    obsess_over_services: bool
    obsess_over_hosts: bool
    check_service_freshness: bool
    check_host_freshness: bool
    enable_flap_detection: bool
    process_performance_data: bool


class StatusJson(BaseModel):
    format_version: int
    result: Optional[ProgramStatusResult]
    data: ProgramStatusData | Any


class NagiosClient:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        ssl_ctx = build_ssl_context(self.cfg)
        timeout = aiohttp.ClientTimeout(total=self.cfg.request_timeout_seconds)
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            raise_for_status=False,
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("NagiosClient session not initialized")
        return self._session

    @property
    def statusjson_url(self) -> str:
        return f"{self.cfg.nagios_base_url.rstrip('/')}/statusjson.cgi"

    async def get_statusjson(self, params: dict[str, str]) -> StatusJson:
        """fetch data from Nagios statusjson.cgi and handle errors with concise messages"""
        url = self.statusjson_url

        try:
            async with self.session.get(url, params=params) as resp:
                text = await resp.text()

                if resp.status >= 400:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "Nagios backend returned an error",
                            "backend_url": url,
                            "backend_status": resp.status,
                            "backend_body": text[:2000],
                        },
                    )

                try:
                    payload = await resp.json(content_type=None)
                    # try and parse it
                    StatusJson.model_validate(payload)
                    return StatusJson.model_validate(
                        humanize_status_fields(payload, params.get("query"))
                    )
                except Exception as exc:
                    raise HTTPException(
                        status_code=502,
                        detail={
                            "message": "Nagios backend did not return valid JSON",
                            "backend_url": url,
                            "backend_body": text[:2000],
                            "error": str(exc),
                        },
                    ) from exc

        except aiohttp.ClientError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "Failed to connect to Nagios backend",
                    "backend_url": url,
                    "error": str(exc),
                },
            ) from exc

    async def check_backend(self) -> StatusJson:
        return await self.get_statusjson({"query": "programstatus"})


def format_startup_error(exc: Exception, nagios: NagiosClient) -> str:
    if isinstance(exc, StartupConfigurationError):
        return str(exc)
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        message = exc.detail.get("message", "Nagios startup check failed")
        backend_url = exc.detail.get("backend_url", nagios.statusjson_url)
        error = exc.detail.get("error")
        if error:
            return f"Startup failed: {message}: {backend_url} ({error})"
        return f"Startup failed: {message}: {backend_url}"

    return f"Startup failed: {nagios.statusjson_url} ({exc})"


async def run_startup_checks(cfg: Settings) -> None:
    validate_backend_settings(cfg)
    nagios = NagiosClient(cfg)
    try:
        await nagios.start()
        await nagios.check_backend()
    finally:
        await nagios.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nagios-status-api",
        description="Query Nagios statusjson.cgi using the current environment config.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("programstatus", help="Fetch Nagios program status")
    subparsers.add_parser("hosts", help="List all hosts")

    host_parser = subparsers.add_parser("host", help="Fetch one host")
    host_parser.add_argument("host_name")

    services_parser = subparsers.add_parser("services", help="List services")
    services_parser.add_argument("--host-name")

    service_parser = subparsers.add_parser("service", help="Fetch one service")
    service_parser.add_argument("host_name")
    service_parser.add_argument("service_description")

    return parser


def command_to_params(args: argparse.Namespace) -> dict[str, str]:
    if args.command == "programstatus":
        return {"query": "programstatus"}
    if args.command == "hosts":
        return {"query": "hostlist"}
    if args.command == "host":
        return {"query": "host", "hostname": args.host_name}
    if args.command == "services":
        params = {"query": "servicelist"}
        if args.host_name:
            params["hostname"] = args.host_name
        return params
    if args.command == "service":
        return {
            "query": "service",
            "hostname": args.host_name,
            "servicedescription": args.service_description,
        }
    raise ValueError(f"Unsupported command: {args.command}")


async def run_cli(args: argparse.Namespace) -> int:
    nagios = NagiosClient(settings)
    try:
        await nagios.start()
        result = await nagios.get_statusjson(command_to_params(args))
        print(result.model_dump_json(indent=2))
        return 0
    except HTTPException as exc:
        detail = (
            exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        )
        message = detail.get("message", "Nagios query failed")
        backend_url = detail.get("backend_url", nagios.statusjson_url)
        error = detail.get("error")
        if error:
            print(f"{message}: {backend_url} ({error})", file=sys.stderr)
        else:
            print(f"{message}: {backend_url}", file=sys.stderr)
        return 1
    finally:
        await nagios.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_cli(args))


def serve(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nagios-status-api-server",
        description="Run the Nagios Status API server.",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run_startup_checks(settings))
    except Exception as exc:
        print(format_startup_error(exc, NagiosClient(settings)), file=sys.stderr)
        return 1
    target = "nagios_status_api:app" if args.reload else app
    uvicorn.run(target, host=args.host, port=args.port, reload=args.reload)
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    nagios = NagiosClient(settings)
    try:
        validate_backend_settings(settings)
        await nagios.start()
        await nagios.check_backend()
        app.state.nagios = nagios
        yield
    except Exception as exc:
        message = format_startup_error(exc, nagios)
        logger.error(message)
        raise RuntimeError(message) from None
    finally:
        await nagios.close()


app = FastAPI(
    title="Nagios Core REST Wrapper",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    nagios: NagiosClient = app.state.nagios
    hosts_payload = await nagios.get_statusjson({"query": "hostlist"})
    services_payload = await nagios.get_statusjson({"query": "servicelist"})
    hostlist = hosts_payload.data.get("hostlist", {})
    servicelist = services_payload.data.get("servicelist", {})
    host_issues = render_dashboard_host_issues(hostlist)
    service_issues = render_dashboard_service_issues(servicelist)
    body = (
        f"{host_issues}"
        f"{service_issues}"
        if host_issues or service_issues
        else '<section class="dashboard-card"><p>All hosts and services are healthy.</p></section>'
    )
    return html_page("Nagios Status API", body)


@app.get("/browse/hosts", response_class=HTMLResponse)
async def browse_hosts(
    sort: str = Query(default="host"),
    dir: str = Query(default="asc"),
) -> HTMLResponse:
    nagios: NagiosClient = app.state.nagios
    payload = await nagios.get_statusjson({"query": "hostlist"})
    hostlist = payload.data.get("hostlist", {})
    return html_page("Hosts", render_host_status_table(hostlist, sort, dir))


@app.get("/browse/hosts/{host_name}", response_class=HTMLResponse)
async def browse_host(
    host_name: str,
    sort: str = Query(default="field"),
    dir: str = Query(default="asc"),
) -> HTMLResponse:
    nagios: NagiosClient = app.state.nagios
    payload = await nagios.get_statusjson({"query": "host", "hostname": host_name})
    services_payload = await nagios.get_statusjson(
        {"query": "servicelist", "hostname": host_name}
    )
    host_data = payload.data.get("host", {})
    host_services = services_payload.data.get("servicelist", {})
    status_text = status_text_from_record("host", host_data)
    host_path = f"/browse/hosts/{quote(host_name, safe='')}"
    body = (
        f"<h1>{html.escape(host_name)}</h1>"
        f'<p>Status: <span class="status {html.escape(str(status_text))}">{html.escape(str(status_text))}</span></p>'
        f"{render_host_services_table(host_name, host_services)}"
        f"{render_key_value_rows(host_data, sort, dir, host_path)}"
        f"<details><summary>Raw JSON</summary><pre>{html.escape(payload.model_dump_json(indent=2))}</pre></details>"
    )
    return html_page(f"Host {host_name}", body)


@app.get("/browse/services", response_class=HTMLResponse)
async def browse_services(
    sort: str = Query(default="host"),
    dir: str = Query(default="asc"),
) -> HTMLResponse:
    nagios: NagiosClient = app.state.nagios
    payload = await nagios.get_statusjson({"query": "servicelist"})
    servicelist = payload.data.get("servicelist", {})
    return html_page("Services", render_service_status_table(servicelist, sort, dir))


@app.get(
    "/browse/services/{host_name}/{service_description}",
    response_class=HTMLResponse,
)
async def browse_service(
    host_name: str,
    service_description: str,
    sort: str = Query(default="field"),
    dir: str = Query(default="asc"),
) -> HTMLResponse:
    nagios: NagiosClient = app.state.nagios
    payload: StatusJson = await nagios.get_statusjson(
        {
            "query": "service",
            "hostname": host_name,
            "servicedescription": service_description,
        }
    )
    service_data = payload.data.get("service", {})
    status_text = status_text_from_record("service", service_data)
    service_path = f"/browse/services/{quote(host_name, safe='')}/{quote(service_description, safe='')}"
    body = (
        f"<h1>{html.escape(service_description)}</h1>"
        f'<p>Host: <a href="/browse/hosts/{quote(host_name, safe="")}">{html.escape(host_name)}</a></p>'
        f'<p>Status: <span class="status {html.escape(str(status_text))}">{html.escape(str(status_text))}</span></p>'
        f"{render_key_value_rows(service_data, sort, dir, service_path)}"
        f"<details><summary>Raw JSON</summary><pre>{html.escape(payload.model_dump_json(indent=2))}</pre></details>"
    )
    return html_page(f"Service {service_description}", body)


@app.get("/healthz")
async def healthz() -> dict[str, str]:

    nagios: NagiosClient = app.state.nagios
    try:
        await nagios.check_backend()
    except Exception:
        return {"status": "error"}
    return {"status": "ok"}


@app.get("/api/v1/programstatus")
async def program_status() -> Any:
    nagios: NagiosClient = app.state.nagios
    return await nagios.get_statusjson({"query": "programstatus"})


@app.get("/api/v1/hosts")
async def list_hosts() -> Any:
    nagios: NagiosClient = app.state.nagios
    return await nagios.get_statusjson({"query": "hostlist"})


@app.get("/api/v1/hosts/{host_name}")
async def get_host(host_name: str) -> Any:
    nagios: NagiosClient = app.state.nagios
    return await nagios.get_statusjson(
        {
            "query": "host",
            "hostname": host_name,
        }
    )


@app.get("/api/v1/services")
async def list_services(
    host_name: Optional[str] = Query(default=None),
) -> Any:
    nagios: NagiosClient = app.state.nagios

    params: dict[str, str]
    if host_name:
        params = {
            "query": "servicelist",
            "hostname": host_name,
        }
    else:
        params = {"query": "servicelist"}

    return await nagios.get_statusjson(params)


@app.get("/api/v1/services/{host_name}/{service_description}")
async def get_service(host_name: str, service_description: str) -> Any:
    nagios: NagiosClient = app.state.nagios
    return await nagios.get_statusjson(
        {
            "query": "service",
            "hostname": host_name,
            "servicedescription": service_description,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
