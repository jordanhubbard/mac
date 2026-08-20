"""The headscale pre-auth key reaches the secrets vault, and comes back out.

install-headscale.sh has always generated a reusable pre-auth key and written it
to the hub's own env file. Every other machine got it because the deploy
pipeline forwarded that env value over ssh during the node's bootstrap -- which
covers exactly the machines the pipeline ssh'es into, and nothing else. The key
that admits a machine to the fleet network was the one credential the fleet's
own vault never held, so an operator wanting to join a provisioner to the same
mesh had no supported way to obtain it.

These tests cover both halves of closing that:

  * PUBLICATION -- install-headscale.sh writes the generated key to the vault
    under a fleet-scoped name, without ever putting it in argv.
  * CONSUMPTION -- install-tailscale.sh falls back to the vault when no
    HEADSCALE_PREAUTHKEY was handed to it.

The bash is exercised for real against a stub `mac` CLI rather than asserted
against as text wherever behavior is what matters: rotate-then-create, the
HEADSCALE_VAULT_PUBLISH policy, and the argv-hygiene claim are all things a
grep would happily agree with while the script did something else.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Resolved once, absolutely: several tests below blank PATH to simulate a node
# with no mac CLI, and a relative "bash" would then fail to spawn at all.
BASH = shutil.which("bash") or "/bin/bash"

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / "deploy" / "headscale-key-vault.sh"
INSTALL_HEADSCALE = (ROOT / "deploy" / "install-headscale.sh").read_text(encoding="utf-8")
INSTALL_TAILSCALE = (ROOT / "deploy" / "install-tailscale.sh").read_text(encoding="utf-8")

# A stub `mac` that records exactly how it was called and simulates the one
# behavior the library depends on: `secret rotate` fails for a name that has
# never been set, and succeeds afterwards.
STUB_CLI = """#!/usr/bin/env bash
store="$STUB_STORE"
printf '%s\\n' "$*" >> "$store/argv.log"
# drop the leading `admin` so the cases below read like the CLI's own spelling
[ "$1" = admin ] && shift
case "$1 $2" in
  "secret rotate")
    name="$3"
    grep -qx "$name" "$store/names" 2>/dev/null || exit 1
    cat > "$store/$name.value"
    echo '{"rotated": true}'
    ;;
  "secret set")
    name="$3"
    cat > "$store/$name.value"
    printf '%s\\n' "$name" >> "$store/names"
    echo '{"created": true}'
    ;;
  "secret get")
    name="$3"
    [ -f "$store/$name.value" ] || { echo "secret not found" >&2; exit 1; }
    printf '%s' "$(cat "$store/$name.value")"
    ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def vault(tmp_path):
    """A stub-CLI sandbox plus a `run` helper that sources the library."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "mac"
    cli.write_text(STUB_CLI, encoding="utf-8")
    cli.chmod(0o755)
    store = tmp_path / "store"
    store.mkdir()

    def run(script: str, **env_overrides):
        env = dict(os.environ)
        env.update(
            {
                "PATH": "%s:%s" % (bin_dir, env.get("PATH", "")),
                "STUB_STORE": str(store),
                "MAC_DEPLOY_VAULT_CLI": str(cli),
                "FLEET_NAME": "demo",
                "HOME": str(tmp_path),
            }
        )
        env.update({key: str(value) for key, value in env_overrides.items()})
        return subprocess.run(
            [BASH, "-c", ". %s\n%s" % (LIBRARY, script)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(ROOT),
        )

    run.store = store  # type: ignore[attr-defined]
    return run


def _argv_log(vault) -> str:
    path = vault.store / "argv.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# Naming and scoping
# ---------------------------------------------------------------------------


def test_secret_name_is_fleet_scoped(vault):
    """Two fleets sharing a hub must not share a mesh key, so the fleet name is
    part of the secret name rather than a convention someone has to remember."""
    result = vault("headscale_vault_secret_name")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "headscale-preauthkey-demo"


def test_secret_name_is_overridable(vault):
    result = vault(
        "headscale_vault_secret_name", HEADSCALE_PREAUTHKEY_SECRET_NAME="mesh-key"
    )
    assert result.stdout.strip() == "mesh-key"


def test_scopes_are_valid_json_naming_the_mesh_capability(vault):
    result = vault("headscale_vault_scopes")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"capabilities": ["mesh"], "agents": []}


def test_scopes_accept_an_explicit_agent_list(vault):
    result = vault(
        "headscale_vault_scopes", HEADSCALE_VAULT_SCOPE_AGENTS="agent_a, agent_b"
    )
    assert json.loads(result.stdout) == {
        "capabilities": ["mesh"],
        "agents": ["agent_a", "agent_b"],
    }


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


def test_publish_creates_a_secret_that_does_not_exist_yet(vault):
    result = vault('printf %s "hskey-first" | headscale_vault_publish mesh-key')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "created"
    assert (vault.store / "mesh-key.value").read_text(encoding="utf-8") == "hskey-first"


def test_publish_rotates_an_existing_secret_rather_than_duplicating_it(vault):
    """Rotation preserves the secret's id and scopes and lands in the audit
    trail as a rotation. Creating a second secret for the same purpose would
    lose both, and a redeploy is the common case -- so rotate is tried first."""
    first = vault('printf %s "hskey-first" | headscale_vault_publish mesh-key')
    assert first.stdout.strip() == "created"

    second = vault(
        'printf %s "hskey-first" | headscale_vault_publish mesh-key >/dev/null\n'
        'printf %s "hskey-second" | headscale_vault_publish mesh-key'
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "rotated"
    assert (vault.store / "mesh-key.value").read_text(encoding="utf-8") == "hskey-second"


def test_publish_never_puts_the_key_in_argv(vault):
    """`ps` is world-readable on every platform the fleet deploys to. A key
    passed as a positional argument is visible to every local user for as long
    as the process lives, so the CLI is driven with --from-stdin."""
    result = vault('printf %s "hskey-supersecret" | headscale_vault_publish mesh-key')
    assert result.returncode == 0, result.stderr

    log = _argv_log(vault)
    assert log, "the stub CLI was never invoked"
    assert "hskey-supersecret" not in log
    assert "--from-stdin" in log


def test_publish_does_not_echo_the_key(vault):
    result = vault('printf %s "hskey-supersecret" | headscale_vault_publish mesh-key')
    assert "hskey-supersecret" not in result.stdout
    assert "hskey-supersecret" not in result.stderr


def test_publish_refuses_an_empty_key(vault):
    """An empty key would be stored, fetched, and then fail the mesh join with
    an opaque auth error far from the cause."""
    result = vault('printf "" | headscale_vault_publish mesh-key')
    assert result.returncode != 0
    assert "refusing to publish an empty key" in result.stderr
    assert not (vault.store / "mesh-key.value").exists()


def test_publish_fails_when_the_cli_is_missing(vault):
    result = vault(
        'printf %s "hskey" | headscale_vault_publish mesh-key',
        MAC_DEPLOY_VAULT_CLI="/nonexistent/mac",
        PATH="/nonexistent",
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# Publication policy
# ---------------------------------------------------------------------------


def test_guarded_publish_reports_the_disposition(vault):
    result = vault(
        'printf %s "hskey" | headscale_vault_publish_guarded headscale-preauthkey-demo'
    )
    assert result.returncode == 0, result.stderr
    assert "created secret headscale-preauthkey-demo" in result.stdout


def test_guarded_publish_off_skips_without_calling_the_cli(vault):
    result = vault(
        'printf %s "hskey" | headscale_vault_publish_guarded mesh-key',
        HEADSCALE_VAULT_PUBLISH="off",
    )
    assert result.returncode == 0, result.stderr
    assert "publication disabled" in result.stdout
    assert _argv_log(vault) == ""


def test_guarded_publish_auto_warns_but_succeeds_when_the_hub_is_absent(vault):
    """install-headscale.sh runs during hub bring-up, where the control plane it
    would publish to may not be listening yet. Failing the headscale install
    over that would make the vault copy a hard dependency of the thing that
    produces it."""
    result = vault(
        'printf %s "hskey" | headscale_vault_publish_guarded mesh-key',
        MAC_DEPLOY_VAULT_CLI="/nonexistent/mac",
        PATH="/nonexistent",
    )
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr


def test_guarded_publish_required_fails_when_the_hub_is_absent(vault):
    """A fleet whose workers source the key from the vault must not treat a
    silent no-op as a working install."""
    result = vault(
        'printf %s "hskey" | headscale_vault_publish_guarded mesh-key',
        HEADSCALE_VAULT_PUBLISH="required",
        MAC_DEPLOY_VAULT_CLI="/nonexistent/mac",
        PATH="/nonexistent",
    )
    assert result.returncode != 0
    assert "HEADSCALE_VAULT_PUBLISH=required" in result.stderr


def test_guarded_publish_rejects_an_unknown_policy(vault):
    result = vault(
        'printf %s "hskey" | headscale_vault_publish_guarded mesh-key',
        HEADSCALE_VAULT_PUBLISH="maybe",
    )
    assert result.returncode != 0
    assert "unsupported HEADSCALE_VAULT_PUBLISH" in result.stderr


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------


def test_fetch_returns_only_the_key(vault):
    """The caller does `KEY="$(headscale_vault_fetch "$name")"`, so anything
    else on stdout is baked into the credential."""
    result = vault(
        'printf %s "hskey-fetchme" | headscale_vault_publish mesh-key >/dev/null\n'
        "headscale_vault_fetch mesh-key"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "hskey-fetchme"


def test_fetch_asks_for_raw_output_with_an_audit_purpose(vault):
    vault('printf %s "hskey" | headscale_vault_publish mesh-key >/dev/null\nheadscale_vault_fetch mesh-key')
    log = _argv_log(vault)
    assert "admin secret get mesh-key --raw --purpose headscale-enrollment" in log


def test_fetch_is_empty_for_an_absent_secret(vault):
    result = vault("headscale_vault_fetch absent-key || true")
    assert result.stdout == ""


# ---------------------------------------------------------------------------
# Wiring into the deploy scripts
# ---------------------------------------------------------------------------


def test_install_headscale_publishes_before_writing_the_env_file():
    """Ordering matters for the `required` policy: the env write is what makes
    the install look successful, so a required publication that failed must
    abort before it."""
    publish = INSTALL_HEADSCALE.index("headscale_vault_publish_guarded")
    env_write = INSTALL_HEADSCALE.index("set_env_key \"$ENV_FILE\" HEADSCALE_PREAUTHKEY ")
    assert publish < env_write


def test_install_headscale_pipes_the_key_rather_than_passing_it(vault):
    call = re.search(
        r"printf '%s' \"\$preauthkey\"[^\n]*\n\s*\| headscale_vault_publish_guarded",
        INSTALL_HEADSCALE,
    )
    assert call, "the key must reach the publisher over stdin, not as an argument"


def test_install_headscale_records_the_vault_name_in_the_env_file():
    """The env file is how a later worker deploy learns which vault entry holds
    this fleet's key."""
    assert (
        'set_env_key "$ENV_FILE" HEADSCALE_PREAUTHKEY_SECRET_NAME "$preauthkey_secret_name"'
        in INSTALL_HEADSCALE
    )


def test_install_headscale_fails_when_the_vault_library_is_missing():
    assert "headscale-key-vault.sh is missing beside this script" in INSTALL_HEADSCALE


def test_install_tailscale_prefers_an_explicit_key_over_the_vault():
    """A caller who passed HEADSCALE_PREAUTHKEY meant it; the vault is the
    fallback, not an override."""
    assert '[ -n "$HEADSCALE_URL" ] && [ -z "$HEADSCALE_PREAUTHKEY" ]' in INSTALL_TAILSCALE


def test_install_tailscale_tries_the_vault_before_validating_credentials():
    fetch = INSTALL_TAILSCALE.index("headscale_vault_fetch")
    validate = INSTALL_TAILSCALE.index("# -- Validate credentials --")
    assert fetch < validate


def test_install_tailscale_names_the_vault_in_its_failure_message():
    """The old message said only that HEADSCALE_PREAUTHKEY was empty, which
    sends the reader to the env file when the fix may be a token scope."""
    assert "the secrets vault has no" in INSTALL_TAILSCALE


def test_install_tailscale_tolerates_the_vault_library_being_absent():
    """deploy-mac-fleet.sh uploads install-tailscale.sh to a remote host BY
    ITSELF (`prepare_remote_tailscale_prerequisite`), so the library is not
    beside it on that path. That path is the tailscale-cloud repair route and
    never needed the vault -- but an unguarded `source` would abort it under
    `set -e` before it got to say so."""
    assert 'if [ -r "$(headscale_vault_library)" ]; then' in INSTALL_TAILSCALE
    # ...and the fetch is skipped rather than attempted when the name is unset.
    assert 'headscale_preauthkey_secret_name=""' in INSTALL_TAILSCALE
    assert '&& [ -n "$headscale_preauthkey_secret_name" ]' in INSTALL_TAILSCALE


def test_install_tailscale_does_not_abort_when_the_vault_lookup_fails(vault):
    """The script runs under `set -e`; an unguarded failing command substitution
    would kill it before the error message that explains what to do."""
    assert 'headscale_vault_fetch "$headscale_preauthkey_secret_name" || true' in (
        INSTALL_TAILSCALE
    )
