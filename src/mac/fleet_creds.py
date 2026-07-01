"""Client-side fleet credential sync + rotation (auth-token-sync-01).

The hub accepts only the bearer tokens loaded from *its own* ``~/.mac/mac.env``
at process startup (``MAC_API_TOKEN`` / ``MAC_API_TOKENS``; see
``mac.api._load_auth_tokens_from_env``). The client, meanwhile, sends
``MAC_API_TOKEN__<FLEET>`` (see ``mac.dispatch``). Nothing keeps those two
copies in sync, so they drift and the hub returns ``403 "unknown bearer
token"``. There is no in-band "forgot my token" flow — you cannot recover an
API credential through the API it gates. The only recovery channel is the
*out-of-band, higher-trust* one: host (SSH) access to the hub.

This module codifies that recovery path:

* :func:`sync_token` (``mac fleet sync-token``) SSHes to the fleet's hub, reads
  its current ``MAC_API_TOKEN``, and writes it into the local ``~/.mac/.env``
  as ``MAC_API_TOKEN__<FLEET>``.
* :func:`rotate_token` (``mac fleet rotate-token``) drives graceful rotation
  using the hub's existing multi-token ``MAC_API_TOKENS`` registry: add a new
  token alongside the old (overlap window), advertise it as the new primary so
  other clients can ``sync-token`` to it, then ``--prune`` to drop the old
  ones once everyone has rolled over.

Secrets only ever travel over SSH **stdin** — never argv/env (which leak into
remote process listings) and never stdout (callers see fingerprints, not the
token). The SSH runner is injectable so the logic is unit-testable without
shelling out.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from mac.fleet_env import scoped_var, set_env_key
from mac.fleet_ssh import (
    FleetSshError,
    FleetSshSpec,
    resolve_fleet_ssh,
    ssh_argv,
)


class FleetCredsError(Exception):
    """Raised for unrecoverable credential-sync/rotation problems."""


# Read the hub's resolved bearer config: source mac.env, then emit
# MAC_API_TOKEN and MAC_API_TOKENS separated by a unit-separator (0x1f) that
# never appears in url-safe tokens or JSON. The hub is single-fleet, so it uses
# the flat (unscoped) names (mac.api note at _load_auth_tokens_from_env).
_READ_HUB_AUTH_CMD = (
    'set -a; . "$HOME/.mac/mac.env" 2>/dev/null; set +a; '
    "printf '%s\\x1f%s' \"${MAC_API_TOKEN:-}\" \"${MAC_API_TOKENS:-}\""
)

# Idempotent multi-key writer run on the hub. Reads a JSON object {key: value}
# from stdin (so secret values never hit argv/env) and sets each key in
# ~/.mac/mac.env, shell-quoting values so they survive sourcing / systemd
# EnvironmentFile parsing. Stdlib only — no mac import needed on the hub.
_REMOTE_SET_ENV_SCRIPT = (
    "import sys, os, json, shlex, pathlib\n"
    "p = pathlib.Path(os.path.expanduser('~/.mac/mac.env'))\n"
    "updates = json.loads(sys.stdin.read())\n"
    "lines = p.read_text().splitlines() if p.exists() else []\n"
    "remaining = dict(updates)\n"
    "out = []\n"
    "for ln in lines:\n"
    "    s = ln[len('export '):] if ln.strip().startswith('export ') else ln\n"
    "    k = s.split('=', 1)[0].strip() if '=' in s else None\n"
    "    if k in remaining:\n"
    "        out.append('%s=%s' % (k, shlex.quote(str(remaining.pop(k)))))\n"
    "    else:\n"
    "        out.append(ln)\n"
    "for k, v in remaining.items():\n"
    "    out.append('%s=%s' % (k, shlex.quote(str(v))))\n"
    "p.write_text('\\n'.join(out) + '\\n')\n"
    "os.chmod(p, 0o600)\n"
    "print('ok')\n"
)


# --------------------------------------------------------------------------- #
# SSH runner abstraction (injectable for tests)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., RunResult]


def _subprocess_runner(argv: List[str], *, input: Optional[str] = None) -> RunResult:
    proc = subprocess.run(
        argv,
        input=input,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- #
# fleets.yaml resolution
# --------------------------------------------------------------------------- #
def _fleets_config_path(path: Optional[str] = None) -> Path:
    if path:
        return Path(path).expanduser()
    env = os.environ.get("MAC_FLEETS_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".mac" / "fleets.yaml"


def load_fleets_config(path: Optional[str] = None) -> dict:
    p = _fleets_config_path(path)
    if not p.is_file():
        raise FleetCredsError("fleets config not found: %s" % p)
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - yaml is a hard dep in practice
        raise FleetCredsError("PyYAML is required to read %s" % p) from exc
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise FleetCredsError("could not parse %s: %s" % (p, exc)) from exc
    if not isinstance(data, dict):
        raise FleetCredsError("unexpected fleets config shape in %s" % p)
    return data


HubSsh = FleetSshSpec


def hub_ssh(config: dict, fleet: str) -> HubSsh:
    """Resolve the SSH coordinates of ``fleet``'s hub from a fleets config."""
    try:
        return resolve_fleet_ssh(config, fleet)
    except FleetSshError as exc:
        raise FleetCredsError(str(exc)) from exc


def ssh_command(hub: HubSsh, remote_cmd: str) -> List[str]:
    """Build the ``ssh`` argv that runs ``remote_cmd`` on the hub host."""
    try:
        return ssh_argv(hub, remote_cmd)
    except FleetSshError as exc:
        raise FleetCredsError(str(exc)) from exc


def restart_command(hub: HubSsh) -> str:
    """Best-effort command to reload the hub so it re-reads its token set.

    The hub loads tokens once at startup, so any token change needs a restart.
    The service name is the fleet's ``fleet_name`` (e.g. ``mac`` for the rocky
    fleet) per the deploy script.
    """
    name = hub.fleet_name
    if hub.supervisor == "supervisord":
        return "supervisorctl restart %s-control-plane" % name
    if hub.os_kind == "darwin":
        return "launchctl kickstart -k gui/$(id -u)/com.%s.control-plane" % name
    return "sudo systemctl restart %s" % name


def _fingerprint(token: str) -> str:
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def read_hub_auth(hub: HubSsh, *, runner: Runner = _subprocess_runner) -> Tuple[str, str]:
    """Return ``(MAC_API_TOKEN, MAC_API_TOKENS)`` as seen by the hub process."""
    res = runner(ssh_command(hub, _READ_HUB_AUTH_CMD), input=None)
    if res.returncode != 0:
        raise FleetCredsError(
            "ssh to hub %s failed (rc=%d): %s"
            % (hub.target, res.returncode, (res.stderr or "").strip())
        )
    token, _sep, tokens = res.stdout.partition("\x1f")
    return token.strip(), tokens.strip()


# --------------------------------------------------------------------------- #
# #1 — sync a client's token to the hub's current value
# --------------------------------------------------------------------------- #
def sync_token(
    fleet: str,
    *,
    fleets_config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    runner: Runner = _subprocess_runner,
) -> dict:
    """Pull the hub's current ``MAC_API_TOKEN`` into the local scoped client var."""
    config = load_fleets_config(fleets_config_path)
    hub = hub_ssh(config, fleet)
    token, _tokens = read_hub_auth(hub, runner=runner)
    if not token:
        raise FleetCredsError(
            "hub %s has no MAC_API_TOKEN in ~/.mac/mac.env (auth may be open, or "
            "the hub uses only MAC_API_TOKENS — rotate-token --prune to reduce to one)"
            % hub.target
        )
    env_file = Path(env_path).expanduser() if env_path else (Path.home() / ".mac" / ".env")
    key = scoped_var("MAC_API_TOKEN", fleet)
    changed = set_env_key(env_file, key, token)
    return {
        "fleet": fleet,
        "hub": hub.target,
        "env_file": str(env_file),
        "key": key,
        "fingerprint": _fingerprint(token),
        "changed": changed,
    }


# --------------------------------------------------------------------------- #
# #3 — multi-token registry primitives + graceful rotation
# --------------------------------------------------------------------------- #
def _principal_dict(value) -> Dict:
    if isinstance(value, dict):
        out: Dict = {"scopes": [str(s) for s in value.get("scopes", [])]}
        if value.get("tenant_id"):
            out["tenant_id"] = value["tenant_id"]
        if value.get("agent_id"):
            out["agent_id"] = value["agent_id"]
        return out
    return {"scopes": [str(s) for s in value]}


def normalize_registry(single_token: str, tokens_json: str) -> Dict[str, Dict]:
    """Build ``{token: {"scopes": [...]}}`` from the hub's current env.

    Mirrors ``mac.api._load_auth_tokens_from_env``: a non-empty
    ``MAC_API_TOKENS`` JSON map wins (the single token is then ignored by the
    hub); otherwise the single ``MAC_API_TOKEN`` is an admin token.
    """
    registry: Dict[str, Dict] = {}
    if tokens_json:
        data = json.loads(tokens_json)
        if not isinstance(data, dict):
            raise FleetCredsError("MAC_API_TOKENS on hub is not a JSON object")
        for tok, val in data.items():
            registry[str(tok)] = _principal_dict(val)
    elif single_token:
        registry[single_token] = {"scopes": ["admin"]}
    return registry


def add_token(registry: Dict[str, Dict], token: str, scopes) -> Dict[str, Dict]:
    out = {t: dict(v) for t, v in registry.items()}
    out[token] = {"scopes": list(scopes)}
    return out


def prune_registry(registry: Dict[str, Dict], keep) -> Dict[str, Dict]:
    keep = set(keep)
    return {t: dict(v) for t, v in registry.items() if t in keep}


def render_registry_json(registry: Dict[str, Dict]) -> str:
    return json.dumps(registry, separators=(",", ":"), sort_keys=True)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _write_hub_env(hub: HubSsh, updates: Dict[str, str], *, runner: Runner) -> None:
    payload = json.dumps(updates)
    res = runner(
        ssh_command(hub, "python3 -c " + shlex.quote(_REMOTE_SET_ENV_SCRIPT)),
        input=payload,
    )
    if res.returncode != 0 or "ok" not in (res.stdout or ""):
        raise FleetCredsError(
            "failed to update hub env on %s: %s"
            % (hub.target, (res.stderr or res.stdout or "").strip())
        )


def rotate_token(
    fleet: str,
    *,
    scopes=("admin",),
    prune: bool = False,
    do_apply: bool = False,
    restart: bool = False,
    fleets_config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    runner: Runner = _subprocess_runner,
    token_factory: Callable[[], str] = new_token,
) -> dict:
    """Rotate the hub's bearer token via the overlapping ``MAC_API_TOKENS`` map.

    Default is a dry-run **plan** (no mutation, fingerprints only). With
    ``do_apply`` the hub env + the local client var are updated. ``prune``
    (apply only) collapses the registry back to the single current token,
    ending the overlap window — run it only after every client has rolled to
    the new token via :func:`sync_token`.
    """
    config = load_fleets_config(fleets_config_path)
    hub = hub_ssh(config, fleet)
    cur_single, cur_tokens = read_hub_auth(hub, runner=runner)
    registry = normalize_registry(cur_single, cur_tokens)
    env_file = Path(env_path).expanduser() if env_path else (Path.home() / ".mac" / ".env")
    key = scoped_var("MAC_API_TOKEN", fleet)
    restart_cmd = restart_command(hub)

    plan: dict = {
        "fleet": fleet,
        "hub": hub.target,
        "key": key,
        "env_file": str(env_file),
        "prune": prune,
        "existing_token_fingerprints": sorted(_fingerprint(t) for t in registry),
        "restart_command": restart_cmd,
        "applied": False,
    }

    if prune:
        # Collapse to the single current primary; clear the overlap map so the
        # hub falls back to the lone MAC_API_TOKEN (mac.api loader: empty
        # MAC_API_TOKENS -> use MAC_API_TOKEN).
        primary = cur_single
        if not primary:
            raise FleetCredsError(
                "cannot --prune: hub %s has no single MAC_API_TOKEN to keep "
                "(advertise one with a non-prune rotate first)" % hub.target
            )
        plan["mode"] = "prune"
        plan["kept_token_fingerprint"] = _fingerprint(primary)
        if not do_apply:
            plan["note"] = (
                "dry-run: would clear MAC_API_TOKENS on the hub (keeping only the "
                "single current token), then restart. Nothing changed."
            )
            return plan
        _write_hub_env(hub, {"MAC_API_TOKENS": ""}, runner=runner)
        plan["applied"] = True
        plan["restart_note"] = (
            "run restart_command on the hub to drop the old tokens"
            if not restart
            else "restarted"
        )
        if restart:
            rres = runner(ssh_command(hub, restart_cmd), input=None)
            plan["restart_rc"] = rres.returncode
        return plan

    # Non-prune: mint a new token, add it alongside the existing ones (overlap),
    # and advertise it as the new primary so other clients can sync to it.
    new = token_factory()
    desired = add_token(registry, new, scopes)
    plan["mode"] = "add"
    plan["new_token_fingerprint"] = _fingerprint(new)
    plan["desired_token_fingerprints"] = sorted(_fingerprint(t) for t in desired)
    if not do_apply:
        plan["note"] = (
            "dry-run: would set hub MAC_API_TOKENS={old...,new} (overlap) and "
            "MAC_API_TOKEN=new, then sync this client. Re-run with --apply. After "
            "restarting the hub and rolling other clients (mac fleet sync-token), "
            "run rotate-token --prune --apply to drop the old tokens."
        )
        return plan

    _write_hub_env(
        hub,
        {"MAC_API_TOKENS": render_registry_json(desired), "MAC_API_TOKEN": new},
        runner=runner,
    )
    local_changed = set_env_key(env_file, key, new)
    plan["applied"] = True
    plan["local_changed"] = local_changed
    if restart:
        rres = runner(ssh_command(hub, restart_cmd), input=None)
        plan["restart_rc"] = rres.returncode
        plan["restart_note"] = "restarted"
    else:
        plan["restart_note"] = "run restart_command on the hub to load the new token set"
    return plan
