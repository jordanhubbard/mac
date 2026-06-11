"""Tests for scripts/purge-dead-tokenhub-keys.sh.

The purge must clean every env file it claims to (agent mac.env, the operator
deploy env, and the gateway ~/.hermes/.env), keep live vars, handle fleet-scoped
__SUFFIX variants, and survive a file that is ENTIRELY dead lines (the awk-vs-
grep -e exit-1 edge case) without aborting under set -e.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "purge-dead-tokenhub-keys.sh"


def _run(home: Path, extra_env=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True
    )


def test_purge_cleans_all_three_env_files(tmp_path):
    home = tmp_path
    (home / ".mac").mkdir(parents=True)
    (home / ".hermes").mkdir(parents=True)
    mac_env = home / ".mac" / "mac.env"
    deploy_env = home / ".mac" / ".env"
    hermes_env = home / ".hermes" / ".env"

    # agent env: dead vars (incl the root URL) mixed with live config
    mac_env.write_text(
        "MAC_API_TOKEN=keepme\n"
        "TOKENHUB_API_KEY=dead\n"
        "MAC_REQUIRE_TOKENHUB=1\n"
        "TOKENHUB_URL=http://hub:8090\n"
        "MAC_TOKENHUB_PORT=8090\n"
        "MAC_ROUTER_BACKEND=inproc\n"
    )
    # operator deploy env: bare + fleet-scoped dead variants + a live provider key
    deploy_env.write_text(
        "MAC_DEPLOY_HUB_AGENT=hosta\n"
        "MAC_DEPLOY_TOKENHUB_API_KEY=dead\n"
        "MAC_DEPLOY_TOKENHUB_API_KEY__HOSTA=dead\n"
        "MAC_DEPLOY_TOKENHUB_URL=http://hub:8090\n"
        "NVIDIA_API_KEY__HOSTA=keep\n"
    )
    # gateway env: ENTIRELY dead lines (would make `grep -v` exit 1 and, under
    # set -e, abort after the backup — awk must emit empty output and exit 0).
    hermes_env.write_text(
        "TOKENHUB_API_KEY=dead\n"
        "export TOKENHUB_ADMIN_TOKEN=dead\n"
        "MAC_TOKENHUB_PORT=8090\n"
    )

    result = _run(home)
    assert result.returncode == 0, result.stderr + result.stdout

    mac_txt = mac_env.read_text()
    assert "TOKENHUB" not in mac_txt
    assert "MAC_REQUIRE_TOKENHUB" not in mac_txt
    assert "MAC_API_TOKEN=keepme" in mac_txt
    assert "MAC_ROUTER_BACKEND=inproc" in mac_txt

    deploy_txt = deploy_env.read_text()
    assert "MAC_DEPLOY_TOKENHUB_API_KEY" not in deploy_txt  # bare + __HOSTA both gone
    assert "MAC_DEPLOY_TOKENHUB_URL" not in deploy_txt
    assert "NVIDIA_API_KEY__HOSTA=keep" in deploy_txt
    assert "MAC_DEPLOY_HUB_AGENT=hosta" in deploy_txt

    # all-dead file: emptied (no dead vars), NOT aborted, backup written
    hermes_txt = hermes_env.read_text()
    assert "TOKENHUB" not in hermes_txt
    assert hermes_txt.strip() == ""
    assert list((home / ".hermes").glob(".env.bak-tokenhub-*")), "backup must exist"
    assert list((home / ".mac").glob("mac.env.bak-tokenhub-*"))
    assert "PURGE_CHANGED=1" in result.stdout


def test_purge_is_idempotent_and_dry_run_makes_no_changes(tmp_path):
    home = tmp_path
    (home / ".mac").mkdir(parents=True)
    mac_env = home / ".mac" / "mac.env"
    mac_env.write_text("MAC_API_TOKEN=keepme\nTOKENHUB_API_KEY=dead\n")

    # dry-run: reports the hit but writes nothing
    dry = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        env={**os.environ, "HOME": str(home)},
        text=True,
        capture_output=True,
    )
    assert dry.returncode == 0
    assert "TOKENHUB_API_KEY" in mac_env.read_text()  # untouched
    assert not list((home / ".mac").glob("mac.env.bak-tokenhub-*"))

    # real run, then a second run is a clean no-op
    first = _run(home)
    assert first.returncode == 0
    assert "TOKENHUB_API_KEY" not in mac_env.read_text()
    second = _run(home)
    assert second.returncode == 0
    assert "PURGE_CHANGED=0" in second.stdout
