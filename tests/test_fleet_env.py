"""Tests for fleet-scoped env var resolution (mac-g55y)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import fleet_env


def test_scoped_var_normalizes_fleet_name():
    assert fleet_env.scoped_var("MAC_API_TOKEN", "rocky") == "MAC_API_TOKEN__ROCKY"
    assert fleet_env.scoped_var("MAC_API_TOKEN", "jordanh-hub") == "MAC_API_TOKEN__JORDANH_HUB"
    assert fleet_env.scoped_var("MAC_API_TOKEN", "fleet.alpha-1") == "MAC_API_TOKEN__FLEET_ALPHA_1"


def test_scoped_var_rejects_empty_fleet():
    with pytest.raises(ValueError):
        fleet_env.scoped_var("MAC_API_TOKEN", "")
    with pytest.raises(ValueError):
        fleet_env.scoped_var("MAC_API_TOKEN", "---")


def test_resolve_prefers_scoped_form():
    env = {
        "MAC_API_TOKEN": "legacy-flat",
        "MAC_API_TOKEN__ROCKY": "rocky-scoped",
        "MAC_API_TOKEN__JORDANH_HUB": "jh-scoped",
    }
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="rocky", env=env) == "rocky-scoped"
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="jordanh-hub", env=env) == "jh-scoped"


def test_resolve_falls_back_to_legacy_when_no_scoped():
    env = {"MAC_API_TOKEN": "legacy-only"}
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="rocky", env=env) == "legacy-only"


def test_resolve_uses_mac_fleet_env_when_no_explicit_arg():
    env = {
        "MAC_FLEET": "rocky",
        "MAC_API_TOKEN__ROCKY": "rocky-scoped",
        "MAC_API_TOKEN": "legacy",
    }
    assert fleet_env.resolve("MAC_API_TOKEN", env=env) == "rocky-scoped"


def test_resolve_returns_none_when_nothing_set():
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="rocky", env={}) is None


def test_resolve_first_walks_priority_chain():
    env = {
        "MAC_WORKER_TOKEN__ROCKY": "worker-rocky",
        "MAC_API_TOKEN__ROCKY": "api-rocky",
    }
    # MAC_WORKER_TOKEN wins because it's earlier in the list.
    got = fleet_env.resolve_first(["MAC_WORKER_TOKEN", "MAC_API_TOKEN"], fleet="rocky", env=env)
    assert got == "worker-rocky"
    got = fleet_env.resolve_first(["MAC_TOKEN", "MAC_API_TOKEN"], fleet="rocky", env=env)
    assert got == "api-rocky"


def test_parse_env_file_handles_quotes_and_exports(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# comment line\n"
        "\n"
        "MAC_API_TOKEN=plain-value\n"
        'export MAC_DEPLOY_HUB_TOKEN="quoted value with spaces"\n'
        "MAC_DEPLOY_ROUTER_DEFAULT_MODEL='single-quoted'\n"
    )
    parsed = fleet_env.parse_env_file(env_path)
    assert parsed["MAC_API_TOKEN"] == "plain-value"
    assert parsed["MAC_DEPLOY_HUB_TOKEN"] == "quoted value with spaces"
    assert parsed["MAC_DEPLOY_ROUTER_DEFAULT_MODEL"] == "single-quoted"


def test_migrate_env_file_adds_scoped_variants(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MAC_API_TOKEN=secret-token\n"
        "MAC_DEPLOY_HUB_TOKEN=hub-token\n"
        "MAC_DB=/tmp/mac.db\n"  # not in FLEET_SCOPED_VARS — left alone
    )
    added, kept = fleet_env.migrate_env_file(env_path, "rocky")
    assert "MAC_API_TOKEN__ROCKY" in added
    assert added["MAC_API_TOKEN__ROCKY"] == "secret-token"
    assert "MAC_DEPLOY_HUB_TOKEN__ROCKY" in added
    assert "MAC_DB__ROCKY" not in added  # shared vars not migrated
    # Legacy keys preserved by default.
    assert kept["MAC_API_TOKEN"] == "secret-token"

    # The rewritten file should have both forms.
    content = env_path.read_text()
    assert "MAC_API_TOKEN=secret-token" in content
    assert "MAC_API_TOKEN__ROCKY=secret-token" in content
    assert "MAC_DB=/tmp/mac.db" in content


def test_migrate_env_file_drops_legacy_when_requested(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_API_TOKEN=secret-token\n")
    added, kept = fleet_env.migrate_env_file(env_path, "rocky", keep_legacy=False)
    assert added == {"MAC_API_TOKEN__ROCKY": "secret-token"}
    assert kept == {}
    content = env_path.read_text()
    assert "MAC_API_TOKEN=secret-token" not in content
    assert "MAC_API_TOKEN__ROCKY=secret-token" in content


def test_migrate_env_file_is_idempotent(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_API_TOKEN=secret-token\n")
    fleet_env.migrate_env_file(env_path, "rocky")
    # Second migration for same fleet must not duplicate.
    added2, _ = fleet_env.migrate_env_file(env_path, "rocky")
    assert added2 == {}
    # But a new fleet adds a new scoped key.
    added3, _ = fleet_env.migrate_env_file(env_path, "jordanh-hub")
    assert "MAC_API_TOKEN__JORDANH_HUB" in added3


def test_two_fleets_in_one_env_file_do_not_collide(tmp_path: Path):
    """Acceptance scenario from mac-g55y: after migration, a workstation
    that participates in both rocky and jordanh-hub keeps both tokens
    distinct and addressable.
    """
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_API_TOKEN=rocky-token\n")
    fleet_env.migrate_env_file(env_path, "rocky", keep_legacy=False)
    # Now jordanh-hub's setup writes its token — into the SCOPED form,
    # not the legacy form. (This mirrors what the updated setup-fleet.py
    # would do.)
    contents = env_path.read_text() + "MAC_API_TOKEN__JORDANH_HUB=jh-token\n"
    env_path.write_text(contents)
    parsed = fleet_env.parse_env_file(env_path)
    assert parsed["MAC_API_TOKEN__ROCKY"] == "rocky-token"
    assert parsed["MAC_API_TOKEN__JORDANH_HUB"] == "jh-token"
    # And resolve picks the right one per fleet.
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="rocky", env=parsed) == "rocky-token"
    assert fleet_env.resolve("MAC_API_TOKEN", fleet="jordanh-hub", env=parsed) == "jh-token"


def test_mac_token_is_fleet_scoped():
    """mac-g55y audit: MAC_TOKEN sits in the worker credential chain, so it
    must be scoped/migrated/warned about like the other fleet tokens."""
    assert "MAC_TOKEN" in fleet_env.FLEET_SCOPED_VARS


def test_resolve_mac_token_prefers_scoped_form():
    env = {
        "MAC_TOKEN": "legacy-flat",
        "MAC_TOKEN__ROCKY": "rocky-scoped",
    }
    assert fleet_env.resolve("MAC_TOKEN", fleet="rocky", env=env) == "rocky-scoped"


def test_worker_chain_prefers_worker_token_then_mac_token():
    """The worker resolves ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"].
    Argument order — not the FLEET_SCOPED_VARS ordering — decides precedence."""
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"]
    env = {"MAC_WORKER_TOKEN__ROCKY": "worker", "MAC_TOKEN__ROCKY": "tok"}
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == "worker"
    env2 = {"MAC_TOKEN__ROCKY": "tok", "MAC_API_TOKEN__ROCKY": "api"}
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env2) == "tok"


def test_migrate_env_file_scopes_mac_token(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("MAC_TOKEN=worker-token\n")
    added, kept = fleet_env.migrate_env_file(env_path, "rocky")
    assert added["MAC_TOKEN__ROCKY"] == "worker-token"
    assert kept["MAC_TOKEN"] == "worker-token"


def test_legacy_mac_token_emits_deprecation_warning(caplog):
    fleet_env._DEPRECATION_SEEN.clear()
    env = {"MAC_TOKEN": "legacy-only"}
    with caplog.at_level("WARNING", logger="mac.fleet_env"):
        assert fleet_env.resolve("MAC_TOKEN", fleet="rocky", env=env) == "legacy-only"
    assert any("legacy flat env var MAC_TOKEN" in r.getMessage() for r in caplog.records)


def test_resolve_first_scoped_form_outranks_earlier_legacy_flat():
    """mac-g55y regression: a stale legacy flat token for an *earlier* name in
    the chain must not shadow a correct fleet-scoped value for a *later* name.

    This was the startup-heartbeat 403: MAC_WORKER_TOKEN (legacy flat, stale)
    beat MAC_TOKEN__<FLEET> (scoped, correct) because argument order was applied
    before the scoped-vs-legacy distinction.
    """
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"]
    env = {"MAC_WORKER_TOKEN": "stale-legacy", "MAC_TOKEN__ROCKY": "correct-scoped"}
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == "correct-scoped"


def test_resolve_first_prefers_any_scoped_over_any_legacy():
    """Every scoped value in the chain outranks every legacy flat value,
    regardless of position; within each pass argument order still decides."""
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"]
    env = {
        "MAC_WORKER_TOKEN": "legacy-worker",
        "MAC_TOKEN": "legacy-tok",
        "MAC_API_TOKEN__ROCKY": "scoped-api",
    }
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == "scoped-api"


def test_resolve_first_scoped_pass_respects_argument_order():
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"]
    env = {"MAC_TOKEN__ROCKY": "tok", "MAC_API_TOKEN__ROCKY": "api"}
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == "tok"


def test_resolve_first_falls_back_to_legacy_when_no_scoped():
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"]
    env = {"MAC_TOKEN": "legacy-tok", "MAC_API_TOKEN": "legacy-api"}
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == "legacy-tok"


def test_resolve_first_accepts_one_shot_iterable():
    """base_names may be a generator; the two-pass resolver must not exhaust it
    after the scoped pass."""
    env = {"MAC_API_TOKEN": "legacy-api"}
    names = (n for n in ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"])
    assert fleet_env.resolve_first(names, fleet="rocky", env=env) == "legacy-api"


def test_resolve_first_returns_none_when_nothing_set():
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"]
    assert fleet_env.resolve_first(chain, fleet="rocky", env={}) is None


def test_resolve_first_scoped_empty_string_is_treated_as_set():
    """An explicitly-empty fleet-scoped value counts as set, matching
    :func:`resolve`; it short-circuits and is not silently skipped in favor of
    a legacy flat value later in the chain."""
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN"]
    env = {"MAC_WORKER_TOKEN__ROCKY": "", "MAC_TOKEN": "legacy-tok"}
    assert fleet_env.resolve(chain[0], fleet="rocky", env=env) == ""
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == ""


def test_resolve_first_legacy_empty_string_is_treated_as_set():
    """An explicitly-empty legacy flat value is returned rather than skipped,
    consistent with :func:`resolve`."""
    chain = ["MAC_WORKER_TOKEN", "MAC_TOKEN"]
    env = {"MAC_WORKER_TOKEN": "", "MAC_TOKEN": "legacy-tok"}
    assert fleet_env.resolve(chain[0], fleet="rocky", env=env) == ""
    assert fleet_env.resolve_first(chain, fleet="rocky", env=env) == ""
