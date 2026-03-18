from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import ssl
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

INDEX_ROUTES = [
    ("/healthz", "Health check"),
    ("/api/v1/programstatus", "Nagios program status"),
    ("/api/v1/hosts", "List all hosts"),
    ("/api/v1/hosts/{host_name}", "Fetch one host by host name"),
    ("/api/v1/services", "List all services or filter with ?host_name="),
    (
        "/api/v1/services/{host_name}/{service_description}",
        "Fetch one service by host name and service description",
    ),
]


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

    async def get_statusjson(self, params: dict[str, str]) -> Any:
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
                    return await resp.json(content_type=None)
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

    async def check_backend(self) -> None:
        await self.get_statusjson({"query": "programstatus"})


def format_startup_error(exc: Exception, nagios: NagiosClient) -> str:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
        message = exc.detail.get("message", "Nagios startup check failed")
        backend_url = exc.detail.get("backend_url", nagios.statusjson_url)
        error = exc.detail.get("error")
        if error:
            return f"{message}: {backend_url} ({error})"
        return f"{message}: {backend_url}"

    return f"Nagios startup check failed: {nagios.statusjson_url} ({exc})"


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
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
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
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    nagios = NagiosClient(settings)
    try:
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


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    rows = "".join(
        f'<tr><td><a href="{path}">{path}</a></td><td>{description}</td></tr>'
        for path, description in INDEX_ROUTES
    )
    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Nagios Status API</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; max-width: 960px; }}
    th, td {{ border: 1px solid #ccc; padding: 0.75rem; text-align: left; vertical-align: top; }}
    th {{ background: #f5f5f5; }}
    code {{ font-family: monospace; }}
  </style>
</head>
<body>
  <h1>Nagios Status API</h1>
  <p>Available endpoints:</p>
  <table>
    <thead>
      <tr><th>Path</th><th>Description</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""
    return HTMLResponse(content=content)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
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
