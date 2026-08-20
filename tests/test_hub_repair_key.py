"""The hub's own SSH key: restricted authorization and a closed repair grammar.

The feature exists so a wedged worker is reachable when the operator who
provisioned it is not.  That only holds if three things are true, and each
section below pins one of them: the hub's authorization is a forced command
rather than a shell, rotating the hub key retires the old one instead of
stacking beside it, and the on-node shim enforces the same grammar this module
validates -- so the tests run the generated shell, not a Python imitation of it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.hub_repair_key import (  # noqa: E402
    EXIT_DENIED,
    KEY_COMMENT,
    MAX_TAIL_LINES,
    SCHEMA,
    VERBS,
    HubRepairKeyError,
    authorized_keys_line,
    authorized_keys_options,
    hub_repair_key_path,
    install_authorized_key,
    main,
    merge_authorized_keys,
    parse_public_key,
    parse_repair_request,
    parse_service_map,
    repair_shim_script,
)

HUB_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHubRepairKeyBodyAAAAAAAAAAAAAAAAAAAAAAAA"
ROTATED_HUB_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRotatedHubKeyBodyAAAAAAAAAAAAAAAAAAAAA"
OPERATOR_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOperatorProvisionerKeyAAAAAAAAAAAAAAA"
    " jkh@laptop"
)
SHIM = "/home/mac/.mac/bin/mac-hub-repair"
SERVICES = {"mac": "mac", "agent": "mac-agent", "hermes": "hermes"}


# ---------------------------------------------------------------------------
# Authorization shape
# ---------------------------------------------------------------------------

def test_hub_entry_is_restricted_and_forced_to_the_shim():
    line = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    options = line.split(" ", 1)[0]
    assert options.startswith("restrict,")
    assert 'command="%s"' % SHIM in options
    assert line.endswith(" " + KEY_COMMENT)


def test_an_unrestricted_hub_entry_is_not_installable():
    # The whole security argument is the forced command; refusing to emit an
    # entry without one keeps "temporarily give the hub a shell" from being a
    # one-flag operation.
    with pytest.raises(HubRepairKeyError):
        authorized_keys_options("")


def test_from_patterns_narrow_the_entry_further():
    options = authorized_keys_options(SHIM, from_patterns=["100.64.0.0/10", "10.1.2.3"])
    assert 'from="100.64.0.0/10,10.1.2.3"' in options


@pytest.mark.parametrize(
    "command",
    ['/bin/sh -c "id"', "/bin/sh\nrm -rf /", "/bin/sh\\x"],
)
def test_forced_command_rejects_quote_and_newline_smuggling(command):
    with pytest.raises(HubRepairKeyError):
        authorized_keys_options(command)


def test_public_key_comment_is_normalized_to_the_marker():
    parsed = parse_public_key("%s mac-hub-tunnel@somewhere" % HUB_KEY)
    assert parsed.comment == KEY_COMMENT


@pytest.mark.parametrize(
    "value",
    ["", "ssh-ed25519", "not-a-key AAAA", "ssh-ed25519 not+base64!", "ssh-ed25519 AAAA\nssh-ed25519 AAAA"],
)
def test_unusable_public_keys_are_rejected(value):
    with pytest.raises(HubRepairKeyError):
        parse_public_key(value)


# ---------------------------------------------------------------------------
# Rotation and coexistence with the provisioner's key
# ---------------------------------------------------------------------------

def test_merge_preserves_the_provisioner_key_verbatim():
    existing = "# operator keys\n%s\n" % OPERATOR_KEY
    line = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    merged, changed = merge_authorized_keys(existing, line)
    assert changed is True
    assert merged.splitlines()[:2] == ["# operator keys", OPERATOR_KEY]
    assert merged.splitlines()[-1] == line


def test_merge_is_idempotent():
    line = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    once, _ = merge_authorized_keys(OPERATOR_KEY + "\n", line)
    twice, changed = merge_authorized_keys(once, line)
    assert twice == once
    assert changed is False


def test_rotating_the_hub_key_retires_the_previous_entry():
    # Appending without pruning is the failure this replaces: a rotated hub key
    # would leave its predecessor authorized on every worker forever.
    old = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    new = authorized_keys_line(ROTATED_HUB_KEY, forced_command=SHIM)
    first, _ = merge_authorized_keys(OPERATOR_KEY + "\n", old)
    second, changed = merge_authorized_keys(first, new)
    assert changed is True
    assert HUB_KEY.split()[1] not in second
    assert ROTATED_HUB_KEY.split()[1] in second
    assert OPERATOR_KEY in second


def test_merge_replaces_the_same_key_installed_unrestricted():
    # An earlier install (or a hand edit) may have authorized the same key with
    # a full shell and a different comment. Re-installing must narrow it, not
    # leave the unrestricted line in place next to the restricted one.
    existing = "%s %s\n" % (HUB_KEY, "mac-hub-tunnel")
    line = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    merged, changed = merge_authorized_keys(existing, line)
    assert changed is True
    assert merged.strip().splitlines() == [line]


def test_merge_survives_quoted_commands_in_neighbouring_entries():
    # Splitting an authorized_keys line on whitespace mangles exactly the kind
    # of entry this module writes; the parser has to honour the quoting.
    neighbour = 'command="/usr/bin/backup --to /srv, now",no-pty %s borg' % ROTATED_HUB_KEY
    line = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    merged, _ = merge_authorized_keys(neighbour + "\n", line)
    assert neighbour in merged
    assert merged.strip().endswith(line)


def test_install_authorized_key_is_owner_only_and_reports_change(tmp_path):
    path = tmp_path / ".ssh" / "authorized_keys"
    line = authorized_keys_line(HUB_KEY, forced_command=SHIM)
    assert install_authorized_key(path, line) is True
    assert install_authorized_key(path, line) is False
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text(encoding="utf-8").strip() == line


def test_install_refuses_a_line_that_is_not_ours(tmp_path):
    with pytest.raises(HubRepairKeyError):
        install_authorized_key(tmp_path / "authorized_keys", OPERATOR_KEY)


# ---------------------------------------------------------------------------
# Request grammar (Python side)
# ---------------------------------------------------------------------------

def test_every_verb_round_trips_through_the_parser():
    samples = {
        "status": "status",
        "services": "services",
        "restart": "restart mac",
        "logs": "logs",
        "tail": "tail deploy.log 50",
        "deploy-info": "deploy-info",
    }
    assert set(samples) == set(VERBS)
    for verb, request in samples.items():
        assert parse_repair_request(request).verb == verb


@pytest.mark.parametrize(
    "request_text",
    [
        "",
        "   ",
        "bash",
        "status; rm -rf /",
        "status && id",
        "restart mac extra",
        "tail ../../etc/passwd",
        "tail deploy.log 0",
        "tail deploy.log %d" % (MAX_TAIL_LINES + 1),
        "tail deploy.log notanumber",
        "restart $(id)",
        "tail /etc/passwd",
    ],
)
def test_denied_requests(request_text):
    with pytest.raises(HubRepairKeyError):
        parse_repair_request(request_text)


def test_service_allowlist_is_enforced_when_supplied():
    assert parse_repair_request("restart mac", services=SERVICES).args == ("mac",)
    with pytest.raises(HubRepairKeyError):
        parse_repair_request("restart sshd", services=SERVICES)


def test_parse_service_map_rejects_unusable_pairs():
    assert parse_service_map(["mac=mac", "agent=mac-agent"]) == {
        "mac": "mac",
        "agent": "mac-agent",
    }
    for bad in (["mac"], ["=mac"], ["mac="], ["MAC=mac"], ["mac=a b"]):
        with pytest.raises(HubRepairKeyError):
            parse_service_map(bad)


# ---------------------------------------------------------------------------
# The generated shim, executed
# ---------------------------------------------------------------------------

@pytest.fixture()
def shim(tmp_path):
    """Render the shim, make it executable, and give it a node-like MAC_HOME."""

    script = repair_shim_script(supervisor="supervisord", services=SERVICES, agent="worker-1")
    path = tmp_path / "mac-hub-repair"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    home = tmp_path / "machome"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "deploy.log").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (home / "deployed-source-revision").write_text("abc123\n", encoding="utf-8")
    return path, home


def _run_shim(shim_path: Path, home: Path, request: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, MAC_HOME=str(home), SSH_ORIGINAL_COMMAND=request)
    # /bin/sh, not bash: the shim runs on nodes whose login shell is whatever
    # the image ships, so POSIX sh is the contract.
    return subprocess.run(
        ["/bin/sh", str(shim_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_shim_is_posix_sh_clean(shim):
    shim_path, _ = shim
    assert subprocess.run(["/bin/sh", "-n", str(shim_path)]).returncode == 0


def test_shim_denies_an_interactive_shell(shim):
    shim_path, home = shim
    result = _run_shim(shim_path, home, "")
    assert result.returncode == EXIT_DENIED
    assert "interactive shells are not permitted" in result.stderr


@pytest.mark.parametrize(
    "request_text",
    [
        "bash",
        "status; id",
        "status && id",
        "restart mac; id",
        "tail ../../etc/passwd",
        "tail deploy.log %d" % (MAX_TAIL_LINES + 1),
        "tail deploy.log 0",
        "restart sshd",
        "restart",
        "status extra",
        "`id`",
        "$(id)",
    ],
)
def test_shim_denies_what_the_parser_denies(shim, request_text):
    shim_path, home = shim
    result = _run_shim(shim_path, home, request_text)
    assert result.returncode == EXIT_DENIED, result.stdout
    assert "denied" in result.stderr


def test_shim_serves_deploy_info_and_tail(shim):
    shim_path, home = shim
    info = _run_shim(shim_path, home, "deploy-info")
    assert info.returncode == 0
    assert "deployed_source_revision=abc123" in info.stdout

    tail = _run_shim(shim_path, home, "tail deploy.log 2")
    assert tail.returncode == 0
    assert tail.stdout.splitlines() == ["two", "three"]

    logs = _run_shim(shim_path, home, "logs")
    assert "deploy.log" in logs.stdout


def test_shim_audits_allowed_and_denied_requests(shim):
    shim_path, home = shim
    _run_shim(shim_path, home, "deploy-info")
    _run_shim(shim_path, home, "restart sshd")
    audit = (home / "logs" / "hub-repair.log").read_text(encoding="utf-8")
    assert "allowed" in audit and "request=deploy-info" in audit
    # A request that fails the allowlist must not be recorded as allowed --
    # otherwise the audit trail says the hub restarted something it never
    # touched.
    denied = [line for line in audit.splitlines() if "restart sshd" in line]
    assert denied and all(" denied " in line for line in denied)


def test_shim_refuses_an_unsupported_supervisor():
    with pytest.raises(HubRepairKeyError):
        repair_shim_script(supervisor="runit", services=SERVICES)


def test_shim_requires_at_least_one_service():
    with pytest.raises(HubRepairKeyError):
        repair_shim_script(supervisor="systemd", services={})


# ---------------------------------------------------------------------------
# CLI, as the deploy calls it
# ---------------------------------------------------------------------------

def test_install_subcommand_writes_shim_and_authorizes_key(tmp_path, capsys):
    authorized = tmp_path / ".ssh" / "authorized_keys"
    authorized.parent.mkdir(parents=True)
    authorized.write_text(OPERATOR_KEY + "\n", encoding="utf-8")
    shim_path = tmp_path / ".mac" / "bin" / "mac-hub-repair"

    rc = main(
        [
            "install",
            "--supervisor", "systemd",
            "--service", "mac=mac",
            "--service", "agent=mac-agent",
            "--agent", "worker-1",
            "--public-key", HUB_KEY,
            "--shim", str(shim_path),
            "--authorized-keys", str(authorized),
        ]
    )
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == SCHEMA
    assert report["shim_changed"] is True and report["authorized_keys_changed"] is True
    assert oct(shim_path.stat().st_mode & 0o777) == "0o700"

    body = authorized.read_text(encoding="utf-8")
    assert OPERATOR_KEY in body
    assert 'command="%s"' % shim_path in body


def test_check_command_exits_denied_for_a_bad_request(capsys):
    assert main(["check-command", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["verb"] == "status"
    assert main(["check-command", "bash"]) == EXIT_DENIED


def test_repair_ssh_argv_inherits_the_fleet_bastion(tmp_path, monkeypatch):
    # The hub needs its own key, not its own transport: an in-cluster worker is
    # only reachable through the fleet's declared ProxyJump, and the hub reads
    # the same registry the deploy installs on every node.
    from mac.hub_repair_key import repair_ssh_argv

    key = tmp_path / "mac-hub-repair-id"
    key.write_text("private", encoding="utf-8")
    config = {
        "fleets": {
            "gke": {
                "fleet_name": "gke",
                "hub_agent": "gke-hub",
                "defaults": {
                    "ssh_jump": "ops@bastion.example:2222",
                    "ssh_host_key_policy": "accept-new",
                },
                "agents": {
                    "gke-worker-1": {"target": "horde@gke-worker-1", "os": "linux"},
                },
            }
        }
    }
    argv = repair_ssh_argv(
        config,
        "gke-worker-1",
        parse_repair_request("restart mac"),
        identity_file=str(key),
    )
    assert "ProxyJump=ops@bastion.example:2222" in argv
    assert argv[argv.index("-i") + 1] == str(key)
    assert argv[-2:] == ["horde@gke-worker-1", "restart mac"]


def test_repair_ssh_argv_reports_an_absent_hub_key(tmp_path):
    from mac.hub_repair_key import repair_ssh_argv

    config = {
        "fleets": {
            "f": {"hub_agent": "h", "agents": {"h": {"target": "u@h"}}},
        }
    }
    with pytest.raises(HubRepairKeyError, match="is absent"):
        repair_ssh_argv(
            config, "h", parse_repair_request("status"),
            identity_file=str(tmp_path / "missing"),
        )


# ---------------------------------------------------------------------------
# Deploy wiring
# ---------------------------------------------------------------------------

NODE_INSTALL = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
FLEET_DEPLOY = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")


def test_hub_generates_the_repair_key_and_workers_authorize_it():
    assert "ensure_hub_repair_key" in NODE_INSTALL
    assert "install_hub_repair_access" in NODE_INSTALL
    # The node delegates both artifacts to this module rather than hand-rolling
    # the authorized_keys grammar in shell, so the entry a worker ends up with
    # and the entry this module's parser recognizes cannot drift apart.
    block = NODE_INSTALL.split("install_hub_repair_access() {", 1)[1].split("\n}", 1)[0]
    assert "-m mac.hub_repair_key install" in block
    assert "--shim " in block and "--authorized-keys " in block
    assert "--public-key " in block


def test_the_node_can_actually_import_the_module_it_invokes():
    # $PY is the node's system interpreter, not $MAC_HOME/venv, so `-m
    # mac.hub_repair_key` resolves only when PYTHONPATH points at the freshly
    # unpacked source tree. Omitting it does not degrade -- the worker dies on
    # ModuleNotFoundError the moment the hub has a repair key to install, which
    # is every worker in the cohort right after the hub is upgraded.
    block = NODE_INSTALL.split("install_hub_repair_access() {", 1)[1].split("\n}", 1)[0]
    invocation = block.split("-m mac.hub_repair_key install", 1)[0].rsplit("\n", 1)[-1]
    assert 'PYTHONPATH="$SRC_DIR/src:${PYTHONPATH:-}"' in invocation


def test_repair_key_is_created_if_absent_not_rotated_per_deploy():
    block = NODE_INSTALL.split("ensure_hub_repair_key() {", 1)[1].split("\n}", 1)[0]
    assert 'if [ ! -f "$key_file" ]; then' in block
    assert "ssh-keygen" in block
    assert "rm -f" not in block


def test_deploy_reads_the_hub_repair_pubkey_and_ships_it_to_workers():
    assert "read_hub_repair_pubkey" in FLEET_DEPLOY
    assert "keys/mac-hub-repair-id.pub" in FLEET_DEPLOY
    assert 'add_remote_env MAC_DEPLOY_HUB_REPAIR_PUBKEY' in FLEET_DEPLOY
    assert 'HUB_REPAIR_PUBKEY="${MAC_DEPLOY_HUB_REPAIR_PUBKEY:-}"' in NODE_INSTALL


def test_authorizing_the_hub_key_is_skipped_when_the_hub_has_none():
    # A fleet whose hub predates this feature must keep deploying, not fail its
    # whole cohort on a key that does not exist yet.
    block = NODE_INSTALL.split("install_hub_repair_access() {", 1)[1].split("\n}", 1)[0]
    assert '[ -n "$HUB_REPAIR_PUBKEY" ] || return 0' in block


def test_default_key_path_lives_in_node_state_not_the_source_tree(monkeypatch, tmp_path):
    # ~/.mac survives a deploy (which replaces ~/.mac/src and ~/.mac/venv) and
    # is never packed into a release archive. A key under the source tree would
    # be destroyed by the next deploy and shipped by the one after it.
    monkeypatch.delenv("MAC_HUB_REPAIR_KEY", raising=False)
    monkeypatch.setenv("MAC_HOME", str(tmp_path / ".mac"))
    path = hub_repair_key_path()
    assert path == tmp_path / ".mac" / "keys" / "mac-hub-repair-id"
