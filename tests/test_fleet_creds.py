"""Tests for client-side fleet credential sync + rotation (auth-token-sync-01)."""

from __future__ import annotations

import json

import pytest

from mac import fleet_creds as fc
from mac.fleet_env import scoped_var, set_env_key


# --------------------------------------------------------------------------- #
# set_env_key (idempotent single-key env writer)
# --------------------------------------------------------------------------- #
def test_set_env_key_creates_file(tmp_path):
    env = tmp_path / ".env"
    changed = set_env_key(env, "MAC_API_TOKEN__ROCKY", "abc123", backup=False)
    assert changed is True
    assert env.read_text() == "MAC_API_TOKEN__ROCKY=abc123\n"


def test_set_env_key_replaces_in_place_and_preserves_rest(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# header\nFOO=1\nMAC_API_TOKEN__ROCKY=old\nBAR=2\n")
    changed = set_env_key(env, "MAC_API_TOKEN__ROCKY", "new", backup=False)
    assert changed is True
    assert env.read_text() == "# header\nFOO=1\nMAC_API_TOKEN__ROCKY=new\nBAR=2\n"


def test_set_env_key_is_idempotent(tmp_path):
    env = tmp_path / ".env"
    set_env_key(env, "K", "v", backup=False)
    assert set_env_key(env, "K", "v", backup=False) is False


def test_set_env_key_preserves_export_prefix(tmp_path):
    env = tmp_path / ".env"
    env.write_text("export K=old\n")
    set_env_key(env, "K", "new", backup=False)
    assert env.read_text() == "export K=new\n"


def test_set_env_key_quotes_json_values(tmp_path):
    env = tmp_path / ".env"
    set_env_key(env, "MAC_API_TOKENS", '{"t":["admin"]}', backup=False)
    # JSON has quotes/braces -> must be double-quoted to survive sourcing.
    assert env.read_text() == 'MAC_API_TOKENS="{\\"t\\":[\\"admin\\"]}"\n'


def test_set_env_key_writes_backup_on_change(tmp_path):
    env = tmp_path / ".env"
    env.write_text("K=old\n")
    set_env_key(env, "K", "new", backup=True)
    backups = list(tmp_path.glob(".env.bak-setkey-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "K=old\n"


# --------------------------------------------------------------------------- #
# fleets.yaml -> hub ssh resolution
# --------------------------------------------------------------------------- #
SAMPLE_CONFIG = {
    "fleets": {
        "rocky": {
            "fleet_name": "mac",
            "hub_agent": "rocky",
            "agents": [
                {"name": "rocky", "target": "jkh@do-host1", "os": "linux"},
                {"name": "natasha", "target": "jkh@10.0.0.2"},
            ],
        },
        "gke": {
            "fleet_name": "gke",
            "hub_agent": "hub",
            "defaults": {
                "ssh_jump": "bastion@jump:2222",
                "ssh_strict_host_key_checking": False,
                "supervisor": "supervisord",
            },
            "agents": [{"name": "hub", "target": "horde@hub.internal", "os": "linux"}],
        },
    }
}


def test_hub_ssh_resolves_hub_agent_target():
    hub = fc.hub_ssh(SAMPLE_CONFIG, "rocky")
    assert hub.target == "jkh@do-host1"
    assert hub.fleet_name == "mac"
    assert hub.port is None
    assert hub.proxy_jump is None


def test_hub_ssh_inherits_defaults_proxy_and_supervisor():
    hub = fc.hub_ssh(SAMPLE_CONFIG, "gke")
    assert hub.target == "horde@hub.internal"
    assert hub.proxy_jump == "bastion@jump:2222"
    assert hub.strict_host_key_checking is False
    assert hub.supervisor == "supervisord"


def test_hub_ssh_unknown_fleet_raises():
    with pytest.raises(fc.FleetCredsError):
        fc.hub_ssh(SAMPLE_CONFIG, "nope")


def test_ssh_command_includes_batchmode_and_proxyjump():
    hub = fc.hub_ssh(SAMPLE_CONFIG, "gke")
    argv = fc.ssh_command(hub, "echo hi")
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "ProxyJump=bastion@jump:2222" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[-2:] == ["horde@hub.internal", "echo hi"]


def test_restart_command_per_supervisor():
    assert fc.restart_command(fc.hub_ssh(SAMPLE_CONFIG, "rocky")) == "sudo systemctl restart mac"
    assert "supervisorctl" in fc.restart_command(fc.hub_ssh(SAMPLE_CONFIG, "gke"))


# --------------------------------------------------------------------------- #
# registry primitives
# --------------------------------------------------------------------------- #
def test_normalize_registry_single_token():
    reg = fc.normalize_registry("T0", "")
    assert reg == {"T0": {"scopes": ["admin"]}}


def test_normalize_registry_prefers_tokens_map():
    reg = fc.normalize_registry("ignored", '{"a":["read"],"b":{"scopes":["write"],"agent_id":"x"}}')
    assert reg["a"] == {"scopes": ["read"]}
    assert reg["b"] == {"scopes": ["write"], "agent_id": "x"}
    assert "ignored" not in reg


def test_add_and_prune_registry():
    reg = fc.normalize_registry("T0", "")
    reg2 = fc.add_token(reg, "T1", ["admin"])
    assert set(reg2) == {"T0", "T1"}
    assert fc.prune_registry(reg2, {"T1"}) == {"T1": {"scopes": ["admin"]}}


def test_render_registry_json_is_deterministic():
    reg = {"b": {"scopes": ["x"]}, "a": {"scopes": ["y"]}}
    assert fc.render_registry_json(reg) == '{"a":{"scopes":["y"]},"b":{"scopes":["x"]}}'


# --------------------------------------------------------------------------- #
# fake SSH runner
# --------------------------------------------------------------------------- #
class FakeRunner:
    def __init__(self, hub_token="", hub_tokens=""):
        self.hub_token = hub_token
        self.hub_tokens = hub_tokens
        self.calls = []
        self.env_writes = []
        self.restarts = 0

    def __call__(self, argv, *, input=None):
        self.calls.append((argv, input))
        remote = argv[-1]
        if remote == fc._READ_HUB_AUTH_CMD:
            return fc.RunResult(0, "%s\x1f%s" % (self.hub_token, self.hub_tokens), "")
        if remote.startswith("python3 -c"):
            payload = json.loads(input)
            self.env_writes.append(payload)
            if "MAC_API_TOKENS" in payload:
                self.hub_tokens = payload["MAC_API_TOKENS"]
            if "MAC_API_TOKEN" in payload:
                self.hub_token = payload["MAC_API_TOKEN"]
            return fc.RunResult(0, "ok\n", "")
        self.restarts += 1
        return fc.RunResult(0, "", "")


@pytest.fixture
def fleets_file(tmp_path):
    import yaml

    path = tmp_path / "fleets.yaml"
    path.write_text(yaml.safe_dump(SAMPLE_CONFIG))
    return str(path)


# --------------------------------------------------------------------------- #
# sync_token
# --------------------------------------------------------------------------- #
def test_sync_token_writes_scoped_client_var(tmp_path, fleets_file):
    env = tmp_path / ".env"
    runner = FakeRunner(hub_token="HUBTOKEN")
    result = fc.sync_token(
        "rocky", fleets_config_path=fleets_file, env_path=str(env), runner=runner
    )
    assert result["key"] == scoped_var("MAC_API_TOKEN", "rocky")
    assert result["changed"] is True
    from mac.fleet_env import parse_env_file

    assert parse_env_file(env)[result["key"]] == "HUBTOKEN"
    # secret never echoed in the result payload
    assert "HUBTOKEN" not in json.dumps(result)


def test_sync_token_idempotent_second_run(tmp_path, fleets_file):
    env = tmp_path / ".env"
    runner = FakeRunner(hub_token="HUBTOKEN")
    fc.sync_token("rocky", fleets_config_path=fleets_file, env_path=str(env), runner=runner)
    second = fc.sync_token(
        "rocky", fleets_config_path=fleets_file, env_path=str(env), runner=runner
    )
    assert second["changed"] is False


def test_sync_token_errors_when_hub_has_no_token(tmp_path, fleets_file):
    env = tmp_path / ".env"
    with pytest.raises(fc.FleetCredsError):
        fc.sync_token(
            "rocky",
            fleets_config_path=fleets_file,
            env_path=str(env),
            runner=FakeRunner(hub_token=""),
        )


def test_sync_token_host_key_failure_is_closed_and_actionable(tmp_path, fleets_file):
    def failed_runner(argv, *, input=None):
        return fc.RunResult(
            255,
            "",
            "REMOTE HOST IDENTIFICATION HAS CHANGED! Host key verification failed.",
        )

    with pytest.raises(fc.FleetCredsError, match="Host key verification failed"):
        fc.sync_token(
            "rocky",
            fleets_config_path=fleets_file,
            env_path=str(tmp_path / ".env"),
            runner=failed_runner,
        )


# --------------------------------------------------------------------------- #
# rotate_token
# --------------------------------------------------------------------------- #
def test_rotate_dry_run_mutates_nothing(tmp_path, fleets_file):
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    runner = FakeRunner(hub_token="T0")
    plan = fc.rotate_token(
        "rocky",
        fleets_config_path=fleets_file,
        env_path=str(env),
        runner=runner,
        token_factory=lambda: "NEWTOKEN",
    )
    assert plan["applied"] is False
    assert plan["new_token_fingerprint"] == fc._fingerprint("NEWTOKEN")
    assert runner.env_writes == []  # no hub mutation
    assert env.read_text() == "X=1\n"  # no local mutation
    assert "NEWTOKEN" not in json.dumps(plan)  # only fingerprints, never the secret


def test_rotate_apply_overlaps_old_and_new_and_advertises_primary(tmp_path, fleets_file):
    env = tmp_path / ".env"
    runner = FakeRunner(hub_token="T0")
    plan = fc.rotate_token(
        "rocky",
        fleets_config_path=fleets_file,
        env_path=str(env),
        do_apply=True,
        runner=runner,
        token_factory=lambda: "T1",
    )
    assert plan["applied"] is True
    assert len(runner.env_writes) == 1
    write = runner.env_writes[0]
    # primary advertised as the new token so other clients can sync to it
    assert write["MAC_API_TOKEN"] == "T1"
    # overlap window: hub still accepts the old token too
    registry = json.loads(write["MAC_API_TOKENS"])
    assert set(registry) == {"T0", "T1"}
    # operator's own client is moved to the new token
    from mac.fleet_env import parse_env_file

    assert parse_env_file(env)[scoped_var("MAC_API_TOKEN", "rocky")] == "T1"


def test_rotate_prune_clears_overlap_keeping_current(tmp_path, fleets_file):
    env = tmp_path / ".env"
    # hub currently has an overlap map + a single current primary T1
    runner = FakeRunner(
        hub_token="T1", hub_tokens='{"T0":{"scopes":["admin"]},"T1":{"scopes":["admin"]}}'
    )
    plan = fc.rotate_token(
        "rocky",
        fleets_config_path=fleets_file,
        env_path=str(env),
        prune=True,
        do_apply=True,
        runner=runner,
    )
    assert plan["applied"] is True
    assert plan["kept_token_fingerprint"] == fc._fingerprint("T1")
    # clearing MAC_API_TOKENS makes the hub fall back to the single MAC_API_TOKEN
    assert runner.env_writes[-1] == {"MAC_API_TOKENS": ""}


def test_rotate_apply_restart_runs_restart_command(tmp_path, fleets_file):
    env = tmp_path / ".env"
    runner = FakeRunner(hub_token="T0")
    fc.rotate_token(
        "rocky",
        fleets_config_path=fleets_file,
        env_path=str(env),
        do_apply=True,
        restart=True,
        runner=runner,
        token_factory=lambda: "T1",
    )
    assert runner.restarts == 1
