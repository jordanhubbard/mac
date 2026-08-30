"""Hub-verify must not point OpenShell sandboxes at the live hub database."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mac.openshell_runtime import SANDBOX_BASE_PATH
from mac.services import (
    hub_verify_sandbox_env_pairs,
    hub_verify_sandbox_pg_url,
    hub_verify_test_pg_url,
    parse_start_test_postgres_export,
)


def test_parse_start_test_postgres_export() -> None:
    stdout = "warning: skip me\nexport MAC_TEST_PG_URL=postgresql://me@127.0.0.1:5432/mac_test\n"
    assert parse_start_test_postgres_export(stdout) == "postgresql://me@127.0.0.1:5432/mac_test"


def test_hub_verify_rewrites_loopback_for_sandbox() -> None:
    got = hub_verify_sandbox_pg_url("postgresql://tester@127.0.0.1:5432/mac_test")
    assert got == "postgresql://tester@host.openshell.internal:5432/mac_test"


def test_hub_verify_refuses_live_hub_database() -> None:
    live = "postgresql://mac@127.0.0.1:5432/mac"
    assert hub_verify_sandbox_pg_url(live, live_database_url=live) is None


def test_hub_verify_refuses_explicit_dsn_that_is_the_live_hub() -> None:
    live = "postgresql://mac@100.72.16.110:5432/mac"
    assert hub_verify_sandbox_pg_url(live, live_database_url=live) is None


def test_hub_verify_refuses_same_server_different_database() -> None:
    live = "postgresql://mac@127.0.0.1:5432/mac"
    assert (
        hub_verify_sandbox_pg_url(
            "postgresql://tester@127.0.0.1:5432/mac_test",
            live_database_url=live,
        )
        is None
    )


def test_hub_verify_refuses_loopback_when_live_is_tailscale_same_port() -> None:
    live = "postgresql://mac@100.72.16.110:5432/mac"
    assert (
        hub_verify_sandbox_pg_url(
            "postgresql://jkh@127.0.0.1:5432/mac_hubverify",
            live_database_url=live,
        )
        is None
    )


def test_hub_verify_allows_dedicated_port_on_loopback() -> None:
    live = "postgresql://mac@127.0.0.1:5432/mac"
    got = hub_verify_sandbox_pg_url(
        "postgresql://tester@127.0.0.1:55432/mac_hubverify",
        live_database_url=live,
    )
    assert got == "postgresql://tester@host.openshell.internal:55432/mac_hubverify"


def test_hub_verify_host_override() -> None:
    got = hub_verify_sandbox_pg_url(
        "postgresql://tester@localhost:5432/mac_test",
        sandbox_host="hub.internal",
    )
    assert got == "postgresql://tester@hub.internal:5432/mac_test"


def test_hub_verify_env_pairs_include_path_and_optional_dsn() -> None:
    pairs = hub_verify_sandbox_env_pairs()
    assert "HOME=/tmp" in pairs
    assert "PATH=%s" % SANDBOX_BASE_PATH in pairs
    assert not any(item.startswith("MAC_TEST_PG_URL=") for item in pairs)
    injected = hub_verify_sandbox_env_pairs(
        test_pg_url="postgresql://mac_test@host.openshell.internal:55432/mac_hubverify"
    )
    assert (
        "MAC_TEST_PG_URL=postgresql://mac_test@host.openshell.internal:55432/mac_hubverify"
        in injected
    )


def test_hub_verify_test_pg_url_runs_helper_on_dedicated_port(tmp_path: Path, monkeypatch) -> None:
    """Helper exit 0 must inject a rewritten DSN on a non-live port.

    ``returncode or 1`` treats 0 as failure, which is how #682 shipped a helper
    that never injected MAC_TEST_PG_URL.
    """

    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "start-test-postgres.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="export MAC_TEST_PG_URL=postgresql://me@127.0.0.1:55432/mac_hubverify\n",
            stderr="",
        )

    monkeypatch.setattr("mac.services.subprocess.run", fake_run)
    monkeypatch.delenv("MAC_HUB_VERIFY_PG_URL", raising=False)
    monkeypatch.delenv("MAC_HUB_VERIFY_PG_PORT", raising=False)
    monkeypatch.delenv("MAC_HUB_VERIFY_PG_HOST", raising=False)
    monkeypatch.delenv("MAC_OPENSHELL_HOST_ALIAS", raising=False)
    monkeypatch.setenv("MAC_DATABASE_URL", "postgresql://mac@127.0.0.1:5432/mac")
    monkeypatch.setenv("MAC_TEST_PG_URL", "postgresql://mac@127.0.0.1:5432/mac")
    got = hub_verify_test_pg_url(repo)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["MAC_TEST_PG_PORT"] == "55432"
    assert env["MAC_TEST_PG_DB"] == "mac_hubverify"
    assert env.get("MAC_TEST_PG_URL") in (None, "")
    assert got == "postgresql://me@host.openshell.internal:55432/mac_hubverify"
