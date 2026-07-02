"""Coding-CLI credential fabric: workstation -> fleet workers, on demand.

The fleet's agents are the user acting in parallel on more machines — so the
coding CLIs (claude, codex, cursor) on the workers should authenticate as the
user, from the credentials the user already created by logging in on their own
workstation. This module makes the operator's *current* workstation the
source of truth and syncs credentials to workers **over the same trusted SSH
routes the fleet is deployed through**, only when the worker actually needs
them (workers self-report per-CLI auth status in heartbeat resources;
``--needed`` targets exactly the agents whose status shows missing auth).

Where credentials actually live (verified, not assumed):

- **codex** — ``~/.codex/auth.json`` (+ ``config.toml``): plain files in
  ``$HOME`` on every platform. Synced as files.
- **claude** — ``ANTHROPIC_API_KEY`` env, or ``~/.claude/.credentials.json``
  (Linux), or the **macOS Keychain** (service ``"Claude Code"``, commonly a
  plain ``sk-ant-…`` key). Keychain values are exported via ``security`` and
  delivered to the worker as ``ANTHROPIC_API_KEY`` in ``~/.mac/mac.env`` —
  the same place the deploy already keeps that node's bearer secrets.
- **cursor** — ``CURSOR_API_KEY`` env or the macOS Keychain service
  ``"cursor-access-token"``. Delivered as ``CURSOR_API_KEY``.

Transport rules match ``mac.fleet_creds``: secret bytes travel over SSH
**stdin only** — never argv, never env, never stdout, and never through the
hub ledger. The remote side writes files 0600, merges env into ``mac.env``
(0600), then re-runs the coding-agent detector and prints the secret-free
status JSON so the caller gets a verified verdict, not a hope.
"""
from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from mac.fleet_ssh import load_fleet_config, resolve_fleet_ssh, ssh_argv

KNOWN_CLIS = ("claude", "codex", "cursor")

CLAUDE_KEYCHAIN_SERVICE = "Claude Code"
CURSOR_KEYCHAIN_SERVICE = "cursor-access-token"


class CliCredentialError(Exception):
    pass


@dataclass
class CredentialSource:
    """One CLI's portable credential material from this workstation.

    ``files`` maps $HOME-relative paths to raw bytes; ``env`` maps env-var
    names to secret values (merged into the worker's mac.env). ``origin`` is
    a secret-free human description for status output."""

    cli: str
    origin: str
    files: Dict[str, bytes] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)

    @property
    def present(self) -> bool:
        return bool(self.files or self.env)


def _keychain_password(service: str, runner: Optional[Callable] = None) -> str:
    """Export a generic password from the macOS Keychain ('' when absent)."""
    if sys.platform != "darwin" and runner is None:
        return ""
    run = runner or (
        lambda argv: subprocess.run(argv, capture_output=True, text=True)
    )
    try:
        proc = run(["security", "find-generic-password", "-s", service, "-w"])
    except OSError:
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return str(getattr(proc, "stdout", "") or "").strip()


def detect_local_credentials(
    clis: Optional[List[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    keychain: Optional[Callable[[str], str]] = None,
) -> Dict[str, CredentialSource]:
    """Resolve this workstation's portable credentials per CLI.

    Priority per CLI: explicit env var -> portable file(s) -> macOS Keychain.
    Returns a source for every requested CLI; ``present`` is False when this
    workstation has nothing portable for it."""
    import os

    env = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    read_keychain = keychain or _keychain_password
    wanted = list(clis or KNOWN_CLIS)
    sources: Dict[str, CredentialSource] = {}

    def _file_bytes(path: Path) -> Optional[bytes]:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path.read_bytes()
        except OSError:
            pass
        return None

    for cli in wanted:
        if cli == "codex":
            source = CredentialSource(cli="codex", origin="")
            auth = _file_bytes(home / ".codex" / "auth.json")
            if auth is not None:
                source.files[".codex/auth.json"] = auth
                config = _file_bytes(home / ".codex" / "config.toml")
                if config is not None:
                    source.files[".codex/config.toml"] = config
                source.origin = "~/.codex/auth.json"
            sources[cli] = source
        elif cli == "claude":
            source = CredentialSource(cli="claude", origin="")
            key = str(env.get("ANTHROPIC_API_KEY") or "").strip()
            if key:
                source.env["ANTHROPIC_API_KEY"] = key
                source.origin = "ANTHROPIC_API_KEY (env)"
            else:
                credentials = _file_bytes(home / ".claude" / ".credentials.json")
                if credentials is not None:
                    source.files[".claude/.credentials.json"] = credentials
                    source.origin = "~/.claude/.credentials.json"
                else:
                    secret = read_keychain(CLAUDE_KEYCHAIN_SERVICE)
                    if secret.startswith("sk-ant-"):
                        source.env["ANTHROPIC_API_KEY"] = secret
                        source.origin = "macOS Keychain (%s)" % CLAUDE_KEYCHAIN_SERVICE
                    elif secret:
                        # OAuth-credential JSON exported from the Keychain:
                        # materialize it as the Linux-path credentials file.
                        source.files[".claude/.credentials.json"] = secret.encode("utf-8")
                        source.origin = "macOS Keychain (%s)" % CLAUDE_KEYCHAIN_SERVICE
            sources[cli] = source
        elif cli == "cursor":
            source = CredentialSource(cli="cursor", origin="")
            key = str(env.get("CURSOR_API_KEY") or "").strip()
            if not key:
                key = read_keychain(CURSOR_KEYCHAIN_SERVICE)
                if key:
                    source.origin = "macOS Keychain (%s)" % CURSOR_KEYCHAIN_SERVICE
            else:
                source.origin = "CURSOR_API_KEY (env)"
            if key:
                source.env["CURSOR_API_KEY"] = key
            sources[cli] = source
        else:
            raise CliCredentialError("unknown coding CLI: %r" % cli)
    return sources


def build_sync_manifest(sources: Mapping[str, CredentialSource]) -> Dict[str, object]:
    files: Dict[str, str] = {}
    env: Dict[str, str] = {}
    for source in sources.values():
        for rel, content in source.files.items():
            files[rel] = base64.b64encode(content).decode("ascii")
        env.update(source.env)
    return {"schema": "mac.cli_credentials_sync.v1", "files": files, "env": env}


# Runs on the worker under its mac venv. Reads the manifest from stdin (the
# only channel secrets travel), writes files 0600 under $HOME, merges env
# entries into ~/.mac/mac.env (0600), then prints the secret-free detection
# JSON so the caller verifies the sync actually satisfied the CLI gate.
_REMOTE_APPLY = r'''
import base64, json, os, sys
from pathlib import Path

manifest = json.load(sys.stdin)
if manifest.get("schema") != "mac.cli_credentials_sync.v1":
    raise SystemExit("unexpected sync manifest schema")
home = Path.home()
for rel, encoded in (manifest.get("files") or {}).items():
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise SystemExit("refusing manifest path: %r" % rel)
    target = home / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(encoded))
    target.chmod(0o600)
env_updates = manifest.get("env") or {}
if env_updates:
    env_file = home / ".mac" / "mac.env"
    lines = []
    if env_file.exists():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    for key, value in env_updates.items():
        if not key.replace("_", "").isalnum():
            raise SystemExit("refusing env key: %r" % key)
        lines = [l for l in lines if not l.startswith(key + "=")]
        lines.append("%s=%s" % (key, value))
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_file.chmod(0o600)
    os.environ.update(env_updates)
from mac.coding_agent import detect_all
print(json.dumps({"schema": "mac.cli_credentials_apply.v1", "clis": detect_all()}))
'''

_REMOTE_STATUS = r'''
import json
from mac.coding_agent import detect_all
print(json.dumps({"schema": "mac.cli_credentials_apply.v1", "clis": detect_all()}))
'''


def _remote_python_cmd(script: str) -> str:
    quoted = shlex.quote(script)
    return (
        'set -a; . "$HOME/.mac/mac.env" 2>/dev/null; set +a; '
        '"$HOME/.mac/venv/bin/python" -c %s' % quoted
    )


def sync_agent(
    fleet: str,
    agent: str,
    manifest: Mapping[str, object],
    *,
    fleets_config: Optional[str] = None,
    runner: Optional[Callable] = None,
) -> Dict[str, object]:
    """Push the manifest to one agent over its fleet SSH route and return the
    remote post-apply detection (secret-free)."""
    config = load_fleet_config(fleets_config)
    spec = resolve_fleet_ssh(config, fleet, agent)
    argv = ssh_argv(spec, _remote_python_cmd(_REMOTE_APPLY))
    run = runner or (
        lambda a, input: subprocess.run(a, input=input, capture_output=True, text=True)
    )
    proc = run(argv, input=json.dumps(manifest))
    if getattr(proc, "returncode", 1) != 0:
        raise CliCredentialError(
            "credential sync to %s failed: %s"
            % (agent, (getattr(proc, "stderr", "") or "").strip()[-500:])
        )
    return _parse_apply_output(agent, getattr(proc, "stdout", "") or "")


def probe_agent(
    fleet: str,
    agent: str,
    *,
    fleets_config: Optional[str] = None,
    runner: Optional[Callable] = None,
) -> Dict[str, object]:
    """Live remote detection over SSH (no hub dependency, no secrets moved)."""
    config = load_fleet_config(fleets_config)
    spec = resolve_fleet_ssh(config, fleet, agent)
    argv = ssh_argv(spec, _remote_python_cmd(_REMOTE_STATUS))
    run = runner or (
        lambda a, input: subprocess.run(a, input=input, capture_output=True, text=True)
    )
    proc = run(argv, input="")
    if getattr(proc, "returncode", 1) != 0:
        raise CliCredentialError(
            "credential probe of %s failed: %s"
            % (agent, (getattr(proc, "stderr", "") or "").strip()[-500:])
        )
    return _parse_apply_output(agent, getattr(proc, "stdout", "") or "")


def _parse_apply_output(agent: str, stdout: str) -> Dict[str, object]:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            loaded = json.loads(line)
        except ValueError:
            continue
        if isinstance(loaded, dict) and loaded.get("schema") == "mac.cli_credentials_apply.v1":
            return loaded.get("clis") or {}
    raise CliCredentialError(
        "credential apply on %s produced no detection report (stdout tail: %r)"
        % (agent, stdout.strip()[-300:])
    )


def agent_cli_status(agent_resources: Mapping[str, object]) -> Dict[str, object]:
    """Extract the heartbeat-reported CLI status block from agent resources."""
    block = agent_resources.get("coding_clis") if isinstance(agent_resources, dict) else None
    if not isinstance(block, dict):
        return {}
    clis = block.get("clis")
    return clis if isinstance(clis, dict) else {}


def agents_needing_sync(
    agents: List[Mapping[str, object]],
    clis: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """Map agent name -> CLIs that are on PATH but unauthenticated, per the
    agents' own heartbeat reports. An agent that never reported is skipped
    (unknown, not needy) — use ``probe`` for live truth."""
    wanted = list(clis or KNOWN_CLIS)
    out: Dict[str, List[str]] = {}
    for agent in agents:
        name = str(agent.get("name") or agent.get("id") or "").strip()
        status = agent_cli_status(agent.get("resources") or {})
        if not name or not status:
            continue
        missing = [
            cli
            for cli in wanted
            if isinstance(status.get(cli), dict)
            and status[cli].get("on_path")
            and not status[cli].get("available")
        ]
        if missing:
            out[name] = missing
    return out
