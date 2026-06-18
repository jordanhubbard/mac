"""SSH ProxyJump (bastion) support: the setup writes ssh_jump into fleets.yaml,
and the deploy injects -o ProxyJump=/StrictHostKeyChecking into every
operator->node ssh/scp — so a bastion-only fleet (e.g. GKE pods) deploys with no
~/.ssh/config edits, while the in-cluster hub->spoke tunnels stay jump-free."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SCRIPT = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")


def test_fleet_setup_persists_ssh_jump_into_defaults():
    from mac.fleet_setup import build_setup_plan

    spec = {
        "schema": "mac.fleet_setup.v1", "fleet_name": "jordanh-gke", "hub": "jordanh-hub",
        "hub_url": "http://jordanh-hub:8789", "supervisor": "supervisord",
        "ssh_jump": "horde@bastion.horde-gke.nvidia.com:2222",
        "ssh_strict_host_key_checking": False,
        "router": {"backend": "inproc", "providers": [{"id": "nvidia"}]},
        "agents": [{"name": "jordanh-hub", "target": "horde@jordanh-hub", "os": "linux", "supervisor": "supervisord"}],
        "deploy_agents": ["jordanh-hub"],
    }
    plan = build_setup_plan(spec, root=ROOT, fleets_config=Path("/tmp/_x.yaml"), env_file=Path("/tmp/_x.env"))
    d = plan["fleet_config"]["defaults"]
    assert plan["errors"] == []
    assert d["ssh_jump"] == "horde@bastion.horde-gke.nvidia.com:2222"
    assert d["ssh_strict_host_key_checking"] is False


def test_default_setup_keeps_strict_on_and_jump_empty():
    from mac.fleet_setup import build_setup_plan

    spec = {
        "schema": "mac.fleet_setup.v1", "fleet_name": "f", "hub": "h", "hub_url": "http://h:8789",
        "router": {"backend": "inproc", "providers": [{"id": "nvidia"}]},
        "agents": [{"name": "h", "target": "u@h", "os": "linux"}], "deploy_agents": ["h"],
    }
    d = build_setup_plan(spec, root=ROOT, fleets_config=Path("/tmp/_y.yaml"), env_file=Path("/tmp/_y.env"))["fleet_config"]["defaults"]
    assert d["ssh_jump"] == "" and d["ssh_strict_host_key_checking"] is True


def test_deploy_script_wires_proxyjump():
    assert "ssh_conn_opts" in SCRIPT and "ProxyJump=" in SCRIPT and "load_ssh_jump_config" in SCRIPT
    # both arg builders call the injector
    assert SCRIPT.count("\n  ssh_conn_opts\n") >= 2
    # the bare-target ssh calls carry the opts string
    assert "$SSH_CONN_OPTS" in SCRIPT


def _extract(func: str) -> str:
    m = re.search(r"^%s\(\) \{\n.*?^\}$" % re.escape(func), SCRIPT, re.S | re.M)
    assert m, "could not extract %s" % func
    return m.group(0)


def test_ssh_conn_opts_emits_proxyjump_dynamically():
    fn = _extract("ssh_conn_opts")
    with_jump = subprocess.run(
        ["bash", "-c", fn + '\nSSH_JUMP="horde@bastion:2222"; SSH_STRICT=0; ssh_conn_opts | tr "\\0" "\\n"'],
        capture_output=True, text=True,
    ).stdout
    assert "ProxyJump=horde@bastion:2222" in with_jump
    assert "StrictHostKeyChecking=no" in with_jump
    # no jump configured -> emits nothing (default non-bastion fleet)
    none = subprocess.run(
        ["bash", "-c", fn + '\nSSH_JUMP=""; SSH_STRICT=1; ssh_conn_opts | tr "\\0" "\\n"'],
        capture_output=True, text=True,
    ).stdout
    assert none.strip() == ""


def test_load_ssh_jump_config_is_operator_side_safe():
    """load_ssh_jump_config runs on the operator, where the script's log() is
    NOT defined (it lives only inside the remote <<'REMOTE' node payload, so on
    macOS `log` resolves to /usr/bin/log and aborts under set -e). It must use
    the operator-side echo "==>" convention instead."""
    fn = _extract("load_ssh_jump_config")
    assert 'log "' not in fn, "operator-side function must not call the remote-only log()"
    assert 'echo "==>' in fn
