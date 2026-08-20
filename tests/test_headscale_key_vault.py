"""The headscale pre-auth key must reach the secrets vault, and come back out.

install-headscale.sh has generated a reusable pre-auth key for a while, but it
only ever wrote it to the hub's local env file. Workers got the value because
the deploy pipeline forwarded that env var over SSH during their own bootstrap;
nothing else could ask for it. A node the pipeline never touches -- an
operator's laptop, a provisioner host joining the mesh it is about to provision
-- had no route to the key at all, which is how the first demo fleet ended up
pasting a personal Tailscale auth key into ~/.mac/.env instead.

deploy/headscale-key-vault.sh is both ends of the fix. These tests drive it
against a FAKE `mac` CLI (a recording shim on PATH) rather than a live hub, so
they pin the contract that matters here: which CLI verbs run, in which order,
and -- the part a live-hub test would not show -- that the key value never
appears in argv or in anything the script prints.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "headscale-key-vault.sh"

KEY = "hskey-super-secret-value"


def _fake_mac(bin_dir: Path, *, listed: str = "[]", behavior: str = "ok") -> Path:
    """A stand-in `mac` that records its argv+stdin and answers `secret list`.

    ``behavior``: ``ok`` (all calls succeed), ``unreachable`` (every call
    fails, as when the hub is not up yet), or ``no-secret`` (list works, `get`
    fails, as when nothing was ever published).
    """
    calls = bin_dir / "calls.log"
    script = bin_dir / "mac"
    template = """#!/usr/bin/env bash
printf 'ARGV:%s\\n' "$*" >> @CALLS@
stdin_value=''
if [ ! -t 0 ]; then stdin_value="$(cat)"; fi
printf 'STDIN:%s\\n' "$stdin_value" >> @CALLS@
behavior='@BEHAVIOR@'
if [ "$behavior" = unreachable ]; then exit 1; fi
case "$*" in
  *'secret list'*) printf '%s\\n' '@LISTED@' ;;
  *'secret get'*)
    if [ "$behavior" = no-secret ]; then exit 1; fi
    printf '%s\\n' '@KEY@' ;;
  *) printf '{}\\n' ;;
esac
"""
    rendered = (
        template.replace("@CALLS@", str(calls))
        .replace("@BEHAVIOR@", behavior)
        .replace("@LISTED@", listed)
        .replace("@KEY@", KEY)
    )
    script.write_text(rendered, encoding="utf-8")
    script.chmod(0o755)
    return calls


def _run(tmp_path, argument, *, env_extra=None, listed="[]", behavior="ok"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = _fake_mac(bin_dir, listed=listed, behavior=behavior)
    env = dict(os.environ)
    env.update(
        {
            "PATH": "%s:%s" % (bin_dir, env.get("PATH", "")),
            "MAC_BIN": str(bin_dir / "mac"),
            "FLEET_NAME": "demo",
            "ENV_FILE": str(tmp_path / "mac.env"),
            "HOME": str(tmp_path),
        }
    )
    env.update(env_extra or {})
    completed = subprocess.run(
        ["bash", str(SCRIPT), argument],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    recorded = calls.read_text(encoding="utf-8") if calls.exists() else ""
    return completed, recorded


def _env_file(tmp_path) -> dict:
    path = tmp_path / "mac.env"
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_publish_creates_the_secret_when_the_vault_does_not_have_it(tmp_path):
    completed, recorded = _run(
        tmp_path, "publish", env_extra={"HEADSCALE_PREAUTHKEY": KEY}
    )
    assert completed.returncode == 0, completed.stderr

    assert "admin secret set headscale-preauthkey-demo --from-stdin" in recorded
    assert "--created-by install-headscale.sh" in recorded
    # The value is handed over on stdin, never as an argv word: argv is world
    # readable in /proc, so a key passed there leaks to every user on the host.
    assert "STDIN:%s" % KEY in recorded
    argv_lines = [l for l in recorded.splitlines() if l.startswith("ARGV:")]
    assert argv_lines and all(KEY not in line for line in argv_lines)
    assert KEY not in completed.stdout
    assert KEY not in completed.stderr

    env = _env_file(tmp_path)
    assert env["HEADSCALE_PREAUTHKEY_SECRET"] == "headscale-preauthkey-demo"
    assert env["HEADSCALE_PREAUTHKEY_VAULT"] == "published"


def test_publish_rotates_rather_than_duplicating_an_existing_secret(tmp_path):
    """Re-running install-headscale.sh generates a NEW key.

    `secrets.name` is UNIQUE, so a second create would simply fail; rotating
    keeps the record's id, scopes and audit trail while swapping the value,
    which is what a regenerated key actually is.
    """
    listed = '[{"name": "headscale-preauthkey-demo", "id": "secret_1"}]'
    completed, recorded = _run(
        tmp_path,
        "publish",
        env_extra={"HEADSCALE_PREAUTHKEY": KEY},
        listed=listed,
    )
    assert completed.returncode == 0, completed.stderr

    assert "admin secret rotate headscale-preauthkey-demo --from-stdin" in recorded
    assert "secret set" not in recorded
    assert _env_file(tmp_path)["HEADSCALE_PREAUTHKEY_VAULT"] == "published"


def test_publish_reads_the_key_from_the_env_file_when_not_in_the_environment(tmp_path):
    """`publish` is re-runnable by hand after the hub comes up.

    On a fresh fleet the network layer is installed BEFORE the control plane,
    so the first publish attempt is expected to be deferred. The re-run has no
    HEADSCALE_PREAUTHKEY in its environment -- only the env file the earlier
    install wrote.
    """
    (tmp_path / "mac.env").write_text(
        "HEADSCALE_URL=http://127.0.0.1:8080\nHEADSCALE_PREAUTHKEY=%s\n" % KEY,
        encoding="utf-8",
    )
    completed, recorded = _run(tmp_path, "publish")
    assert completed.returncode == 0, completed.stderr
    assert "STDIN:%s" % KEY in recorded
    assert _env_file(tmp_path)["HEADSCALE_PREAUTHKEY_VAULT"] == "published"


def test_publish_defers_instead_of_failing_the_deploy_when_the_hub_is_down(tmp_path):
    """A hub that is not up yet must not fail the network install.

    But it must not look like success either: the env file records `deferred`
    so a later phase -- or an operator reading it -- can tell "published" from
    "nobody ever managed to".
    """
    completed, _ = _run(
        tmp_path,
        "publish",
        env_extra={"HEADSCALE_PREAUTHKEY": KEY},
        behavior="unreachable",
    )
    assert completed.returncode == 0
    assert "did not answer" in completed.stderr
    assert _env_file(tmp_path)["HEADSCALE_PREAUTHKEY_VAULT"] == "deferred"


def test_publish_is_fatal_when_the_caller_demands_the_vault(tmp_path):
    completed, _ = _run(
        tmp_path,
        "publish",
        env_extra={"HEADSCALE_PREAUTHKEY": KEY, "HEADSCALE_VAULT_REQUIRED": "1"},
        behavior="unreachable",
    )
    assert completed.returncode == 1
    assert _env_file(tmp_path)["HEADSCALE_PREAUTHKEY_VAULT"] == "deferred"


def test_fetch_prints_the_key_and_nothing_else(tmp_path):
    """`fetch` is captured in a shell, so stdout carries the key alone.

    Anything else on stdout -- a progress line, a JSON envelope -- ends up
    inside the value install-tailscale.sh passes to `tailscale up`.
    """
    completed, recorded = _run(tmp_path, "fetch")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "%s\n" % KEY
    assert "admin secret get headscale-preauthkey-demo --raw --purpose mesh-join" in recorded


def test_fetch_fails_loudly_rather_than_returning_an_empty_key(tmp_path):
    completed, _ = _run(tmp_path, "fetch", behavior="no-secret")
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert "headscale-preauthkey-demo" in completed.stderr


def test_installers_are_wired_to_the_vault_helper():
    """The scripts that generate and consume the key must actually call it.

    A helper nothing invokes is the same gap with more files in it.
    """
    install_headscale = (ROOT / "deploy" / "install-headscale.sh").read_text(encoding="utf-8")
    assert "headscale-key-vault.sh" in install_headscale
    assert '"$key_vault_script" publish' in install_headscale

    install_tailscale = (ROOT / "deploy" / "install-tailscale.sh").read_text(encoding="utf-8")
    assert "headscale-key-vault.sh" in install_tailscale
    assert '"$key_vault_script" fetch' in install_tailscale


@pytest.mark.parametrize(
    "script_name", ("headscale-key-vault.sh", "install-headscale.sh", "install-tailscale.sh")
)
def test_scripts_are_syntactically_valid_and_executable(script_name):
    script = ROOT / "deploy" / script_name
    assert os.access(script, os.X_OK), "%s must be executable on a node" % script_name
    subprocess.run(["bash", "-n", str(script)], check=True, timeout=60)
