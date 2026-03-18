from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nagios_status_api import (
    NagiosClient,
    Settings,
    app,
    browse_host,
    browse_hosts,
    browse_service,
    browse_services,
    humanize_status_fields,
    index,
    lifespan,
)


@pytest.mark.anyio
async def test_lifespan_checks_backend_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def fake_start(self) -> None:
        events.append("start")

    async def fake_check_backend(self) -> None:
        events.append("check")

    async def fake_close(self) -> None:
        events.append("close")

    monkeypatch.setattr("nagios_status_api.NagiosClient.start", fake_start)
    monkeypatch.setattr("nagios_status_api.NagiosClient.check_backend", fake_check_backend)
    monkeypatch.setattr("nagios_status_api.NagiosClient.close", fake_close)

    async with lifespan(app):
        assert events == ["start", "check"]

    assert events == ["start", "check", "close"]


@pytest.mark.anyio
async def test_lifespan_closes_client_when_startup_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def fake_start(self) -> None:
        events.append("start")

    async def fake_check_backend(self) -> None:
        events.append("check")
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Failed to connect to Nagios backend",
                "backend_url": "https://nagios.example.com/nagios/cgi-bin/statusjson.cgi",
                "error": "backend unavailable",
            },
        )

    async def fake_close(self) -> None:
        events.append("close")

    monkeypatch.setattr("nagios_status_api.NagiosClient.start", fake_start)
    monkeypatch.setattr("nagios_status_api.NagiosClient.check_backend", fake_check_backend)
    monkeypatch.setattr("nagios_status_api.NagiosClient.close", fake_close)

    with pytest.raises(
        RuntimeError,
        match="Failed to connect to Nagios backend: https://nagios.example.com/nagios/cgi-bin/statusjson.cgi \\(backend unavailable\\)",
    ):
        async with lifespan(app):
            pytest.fail("lifespan should not yield when backend check fails")

    assert events == ["start", "check", "close"]


@pytest.mark.anyio
async def test_get_statusjson_connection_error_includes_backend_url() -> None:
    cfg = Settings(
        nagios_base_url="https://nagios.example.com/nagios/cgi-bin",
        client_cert_path="./client.crt",
        client_key_path="./client.key",
        verify_ssl=False,
    )
    client = NagiosClient(cfg)

    class FailingSession:
        def get(self, url: str, params: dict[str, str]):
            raise aiohttp.ClientConnectionError("no route to host")

    client._session = FailingSession()  # type: ignore[assignment]

    with pytest.raises(HTTPException) as exc_info:
        await client.get_statusjson({"query": "programstatus"})

    assert exc_info.value.detail["backend_url"] == client.statusjson_url


@pytest.mark.anyio
async def test_index_lists_available_routes_as_html_table() -> None:
    response = await index()
    body = response.body.decode()

    assert response.media_type == "text/html"
    assert "<table>" in body
    assert '/static/styles.css' in body
    assert '/static/app.js' in body
    assert "/?sort=path&amp;dir=asc" in body or "/?sort=path&dir=asc" in body
    assert "/browse/hosts" in body
    assert "/browse/services" in body
    assert "/api/v1/programstatus" in body
    assert "Nagios program status" in body
    assert "/api/v1/services" in body


@pytest.mark.anyio
async def test_browse_hosts_lists_links(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            assert params == {"query": "hostlist"}
            return {
                "data": {
                    "hostlist": {
                        "db01": "up",
                        "db 02": "down",
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_hosts()
    body = response.body.decode()

    assert "/browse/hosts/db01" in body
    assert "/browse/hosts/db%2002" in body
    assert "/browse/hosts?sort=host&amp;dir=asc" in body or "/browse/hosts?sort=host&dir=asc" in body
    assert "/browse/hosts?sort=status&amp;dir=desc" in body or "/browse/hosts?sort=status&dir=desc" in body
    assert ">up<" in body
    assert ">down<" in body


@pytest.mark.anyio
async def test_browse_hosts_sorts_status_by_raw_value() -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            return {
                "data": {
                    "hostlist": {
                        "pending-host": "pending",
                        "up-host": "up",
                        "down-host": "down",
                        "unreachable-host": "unreachable",
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_hosts(sort="status", dir="asc")
    body = response.body.decode()

    assert body.index("pending-host") < body.index("up-host")
    assert body.index("up-host") < body.index("down-host")
    assert body.index("down-host") < body.index("unreachable-host")


@pytest.mark.anyio
async def test_browse_services_lists_links() -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            assert params == {"query": "servicelist"}
            return {
                "data": {
                    "servicelist": {
                        "db01": {
                            "Disk Space": "warning",
                            "PING": "ok",
                        }
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_services()
    body = response.body.decode()

    assert "/browse/services/db01/Disk%20Space" in body
    assert "/browse/services/db01/PING" in body
    assert "/browse/services?sort=status&amp;dir=desc" in body or "/browse/services?sort=status&dir=desc" in body
    assert ">warning<" in body
    assert ">ok<" in body


@pytest.mark.anyio
async def test_browse_services_sorts_status_by_raw_value() -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            return {
                "data": {
                    "servicelist": {
                        "db01": {
                            "svc-ok": "ok",
                            "svc-warning": "warning",
                            "svc-critical": "critical",
                            "svc-unknown": "unknown",
                        }
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_services(sort="status", dir="asc")
    body = response.body.decode()

    assert body.index("svc-ok") < body.index("svc-warning")
    assert body.index("svc-warning") < body.index("svc-critical")
    assert body.index("svc-warning") < body.index("svc-unknown")
    assert body.index("svc-unknown") < body.index("svc-critical")


@pytest.mark.anyio
async def test_browse_host_renders_status_page(monkeypatch: pytest.MonkeyPatch) -> None:
    seen_params: list[dict[str, str]] = []

    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            seen_params.append(params)
            if params == {"query": "host", "hostname": "db01"}:
                return {
                    "data": {
                        "host": {
                            "host_name": "db01",
                            "current_state": 0,
                            "current_state_text": "up",
                            "plugin_output": "PING OK",
                        }
                    }
                }

            assert params == {"query": "servicelist", "hostname": "db01"}
            return {
                "data": {
                    "servicelist": {
                        "Disk Space": "warning",
                        "PING": "ok",
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_host("db01")
    body = response.body.decode()

    assert "db01" in body
    assert "Status:" in body
    assert "/browse/hosts/db01?sort=field&amp;dir=asc" in body or "/browse/hosts/db01?sort=field&dir=asc" in body
    assert "/browse/services/db01/Disk%20Space" in body
    assert ">warning<" in body
    assert ">ok<" in body
    assert ">up<" in body
    assert "PING OK" in body
    assert "<details><summary>Raw JSON</summary>" in body
    assert '/static/styles.css' in body
    assert '/static/app.js' in body
    assert seen_params == [
        {"query": "host", "hostname": "db01"},
        {"query": "servicelist", "hostname": "db01"},
    ]


@pytest.mark.anyio
async def test_browse_host_falls_back_to_raw_status_value() -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            if params == {"query": "host", "hostname": "apache.housenet.yaleman.org"}:
                return {
                    "data": {
                        "host": {
                            "host_name": "apache.housenet.yaleman.org",
                            "current_state": 2,
                        }
                    }
                }

            assert params == {
                "query": "servicelist",
                "hostname": "apache.housenet.yaleman.org",
            }
            return {"data": {"servicelist": {}}}

    app.state.nagios = FakeNagios()

    response = await browse_host("apache.housenet.yaleman.org")
    body = response.body.decode()

    assert "Status:" in body
    assert ">up<" in body


@pytest.mark.anyio
async def test_browse_host_renders_service_rows_with_text_status() -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            if params == {"query": "host", "hostname": "db01"}:
                return {
                    "data": {
                        "host": {
                            "host_name": "db01",
                            "current_state": 2,
                        }
                    }
                }

            assert params == {"query": "servicelist", "hostname": "db01"}
            return {
                "data": {
                    "servicelist": {
                        "db01": {
                            "Disk Space": 4,
                            "PING": 2,
                        }
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_host("db01")
    body = response.body.decode()

    assert "/browse/services/db01/Disk%20Space" in body
    assert "/browse/services/db01/PING" in body
    assert ">warning<" in body
    assert ">ok<" in body


def test_humanize_status_fields_for_nested_service_list_payload() -> None:
    payload = {
        "data": {
            "servicelist": {
                "db01": {
                    "Disk Space": 4,
                    "PING": 2,
                }
            }
        }
    }

    result = humanize_status_fields(payload, "servicelist")

    assert result["data"]["servicelist"]["db01"]["Disk Space"] == "warning"
    assert result["data"]["servicelist"]["db01"]["PING"] == "ok"


@pytest.mark.anyio
async def test_browse_service_renders_status_page() -> None:
    class FakeNagios:
        async def get_statusjson(self, params: dict[str, str]) -> dict[str, Any]:
            assert params == {
                "query": "service",
                "hostname": "db01",
                "servicedescription": "Disk Space",
            }
            return {
                "data": {
                    "service": {
                        "host_name": "db01",
                        "service_description": "Disk Space",
                        "current_state": 1,
                        "current_state_text": "warning",
                        "plugin_output": "DISK WARNING",
                    }
                }
            }

    app.state.nagios = FakeNagios()

    response = await browse_service("db01", "Disk Space")
    body = response.body.decode()

    assert "Disk Space" in body
    assert '/browse/hosts/db01' in body
    assert "/browse/services/db01/Disk%20Space?sort=field&amp;dir=asc" in body or "/browse/services/db01/Disk%20Space?sort=field&dir=asc" in body
    assert ">warning<" in body
    assert "DISK WARNING" in body
    assert "<details><summary>Raw JSON</summary>" in body


def test_humanize_status_fields_for_service_payload() -> None:
    payload = {
        "data": {
            "service": {
                "current_state": 2,
                "last_hard_state": 1,
                "state_type": 1,
            }
        }
    }

    result = humanize_status_fields(payload, "service")

    assert result["data"]["service"]["current_state"] == 2
    assert result["data"]["service"]["current_state_text"] == "critical"
    assert result["data"]["service"]["last_hard_state_text"] == "warning"
    assert result["data"]["service"]["state_type_text"] == "hard"


def test_humanize_status_fields_for_host_detail_payload() -> None:
    payload = {
        "data": {
            "host": {
                "current_state": 2,
                "last_hard_state": 2,
            }
        }
    }

    result = humanize_status_fields(payload, "host")

    assert result["data"]["host"]["current_state_text"] == "up"
    assert result["data"]["host"]["last_hard_state_text"] == "up"


def test_humanize_status_fields_for_host_payload() -> None:
    payload = {
        "data": {
            "hostlist": {
                "db01": 2,
                "db02": 8,
                "db03": 1,
                "db04": 4,
            }
        }
    }

    result = humanize_status_fields(payload, "hostlist")

    assert result["data"]["hostlist"]["db01"] == "up"
    assert result["data"]["hostlist"]["db02"] == "unreachable"
    assert result["data"]["hostlist"]["db03"] == "pending"
    assert result["data"]["hostlist"]["db04"] == "down"
