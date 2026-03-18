from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nagios_status_api import (
    StartupConfigurationError,
    app,
    build_parser,
    command_to_params,
    main,
    run_cli,
    serve,
)


def test_command_to_params_maps_commands() -> None:
    parser = build_parser()

    assert command_to_params(parser.parse_args(["programstatus"])) == {
        "query": "programstatus"
    }
    assert command_to_params(parser.parse_args(["hosts"])) == {"query": "hostlist"}
    assert command_to_params(parser.parse_args(["host", "db01"])) == {
        "query": "host",
        "hostname": "db01",
    }
    assert command_to_params(parser.parse_args(["services"])) == {
        "query": "servicelist"
    }
    assert command_to_params(
        parser.parse_args(["services", "--host-name", "db01"])
    ) == {"query": "servicelist", "hostname": "db01"}
    assert command_to_params(parser.parse_args(["service", "db01", "Disk Space"])) == {
        "query": "service",
        "hostname": "db01",
        "servicedescription": "Disk Space",
    }


def test_main_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_run_cli(args) -> int:
        assert args.command == "hosts"
        print('{\n  "ok": true\n}')
        return 0

    monkeypatch.setattr("nagios_status_api.run_cli", fake_run_cli)

    exit_code = main(["hosts"])

    assert exit_code == 0
    assert '"ok": true' in capsys.readouterr().out


@pytest.mark.anyio
async def test_run_cli_prints_concise_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["programstatus"])

    async def fake_start(self) -> None:
        return None

    async def fake_close(self) -> None:
        return None

    async def fake_get_statusjson(self, params: dict[str, str]):
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Failed to connect to Nagios backend",
                "backend_url": "https://nagios.example.com/nagios/cgi-bin/statusjson.cgi",
                "error": "no route to host",
            },
        )

    monkeypatch.setattr("nagios_status_api.NagiosClient.start", fake_start)
    monkeypatch.setattr("nagios_status_api.NagiosClient.close", fake_close)
    monkeypatch.setattr(
        "nagios_status_api.NagiosClient.get_statusjson", fake_get_statusjson
    )

    exit_code = await run_cli(args)

    assert exit_code == 1
    assert (
        capsys.readouterr().err.strip()
        == "Failed to connect to Nagios backend: https://nagios.example.com/nagios/cgi-bin/statusjson.cgi (no route to host)"
    )


def test_serve_runs_uvicorn_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_startup_checks(cfg) -> None:
        return None

    def fake_run(target_app, host: str, port: int, reload: bool) -> None:
        calls.append(
            {
                "app": target_app,
                "host": host,
                "port": port,
                "reload": reload,
            }
        )

    monkeypatch.setattr("nagios_status_api.run_startup_checks", fake_startup_checks)
    monkeypatch.setattr("nagios_status_api.uvicorn.run", fake_run)

    exit_code = serve([])
    reload_exit_code = serve(["--reload"])

    assert exit_code == 0
    assert reload_exit_code == 0
    assert calls == [
        {"app": app, "host": "0.0.0.0", "port": 8000, "reload": False},
        {
            "app": "nagios_status_api:app",
            "host": "0.0.0.0",
            "port": 8000,
            "reload": True,
        },
    ]


def test_serve_prints_clean_startup_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_startup_checks(cfg) -> None:
        raise StartupConfigurationError(
            "Startup failed: NAGIOS_BASE_URL is still set to the example hostname nagios.example.com."
        )

    monkeypatch.setattr("nagios_status_api.run_startup_checks", fake_startup_checks)

    exit_code = serve([])

    assert exit_code == 1
    assert (
        capsys.readouterr().err.strip()
        == "Startup failed: NAGIOS_BASE_URL is still set to the example hostname nagios.example.com."
    )
