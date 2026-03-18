from __future__ import annotations

import sys
from pathlib import Path

import aiohttp
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nagios_status_api import Settings, NagiosClient, app, index, lifespan


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
    assert "/api/v1/programstatus" in body
    assert "Nagios program status" in body
    assert "/api/v1/services" in body
