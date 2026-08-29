"""Hub-verify must not point OpenShell sandboxes at the live hub database."""

from __future__ import annotations

from mac.openshell_runtime import SANDBOX_BASE_PATH
from mac.services import (
    hub_verify_sandbox_env_pairs,
    hub_verify_sandbox_pg_url,
    parse_start_test_postgres_export,
)


def test_parse_start_test_postgres_export() -> None:
    stdout = "warning: skip me\nexport MAC_TEST_PG_URL=postgresql://me@127.0.0.1:5432/mac_test\n"
    assert parse_start_test_postgres_export(stdout) == "postgresql://me@127.0.0.1:5432/mac_test"


def test_hub_verify_rewrites_loopback_for_sandbox() -> None:
    got = hub_verify_sandbox_pg_url("postgresql://tester@127.0.0.1:5432/mac_test")
    assert got == "postgresql://tester@host.docker.internal:5432/mac_test"


def test_hub_verify_refuses_live_hub_database() -> None:
    live = "postgresql://mac@127.0.0.1:5432/mac"
    assert hub_verify_sandbox_pg_url(live, live_database_url=live) is None


def test_hub_verify_refuses_explicit_dsn_that_is_the_live_hub() -> None:
    live = "postgresql://mac@100.72.16.110:5432/mac"
    assert hub_verify_sandbox_pg_url(live, live_database_url=live) is None


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
        test_pg_url="postgresql://mac_test@host.docker.internal:5432/mac_test"
    )
    assert "MAC_TEST_PG_URL=postgresql://mac_test@host.docker.internal:5432/mac_test" in injected
