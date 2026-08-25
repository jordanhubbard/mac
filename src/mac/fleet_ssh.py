"""Canonical fleet SSH route resolution and argv construction.

``~/.mac/fleets.yaml`` is the source of truth for operator-to-agent SSH
connectivity.  Historically each caller interpreted a different subset of the
registry and then allowed OpenSSH to fill the gaps from ambient
``~/.ssh/config``.  That made a fleet work on the original operator's machine
while an otherwise identical client could not reproduce the route.

This module defines the versioned, secret-free route contract used by fleet
credential recovery, deploy, soul snapshots, migration, the desktop bridge,
and the SSH-first login flow.  Private key *references* may appear in the
registry; private key bytes never do.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from mac import mac_paths
from typing import Any, Dict, Iterable, List, Mapping, Optional

from mac.fleet_deploy import parse_ssh_target


SCHEMA = "mac.fleet_ssh.v1"
HOST_KEY_POLICIES = frozenset({"strict", "accept-new", "insecure"})


class FleetSshError(ValueError):
    """Raised when a fleet route is absent, ambiguous, or unsafe."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_int(value: Any, *, field: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise FleetSshError("%s must be an integer" % field) from exc
    if parsed <= 0 or parsed > 65535:
        raise FleetSshError("%s must be between 1 and 65535" % field)
    return parsed


def _local_path(value: Any) -> Optional[str]:
    text = _text(value)
    return str(Path(text).expanduser()) if text else None


def _normalize_agents(raw: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(raw, Mapping):
        return {str(name): dict(value) for name, value in raw.items() if isinstance(value, Mapping)}
    result: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, list):
        for value in raw:
            if not isinstance(value, Mapping):
                continue
            name = _text(value.get("name"))
            if name:
                result[name] = dict(value)
    return result


def fleet_entries(config: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return normalized ``{fleet_key: fleet}`` entries.

    The supported registry format is a mapping.  A legacy list is accepted for
    one migration window and keyed by ``hub_agent``/``fleet_name``.
    """

    raw = (config or {}).get("fleets")
    if isinstance(raw, Mapping):
        return {str(name): dict(value) for name, value in raw.items() if isinstance(value, Mapping)}
    result: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, list):
        for value in raw:
            if not isinstance(value, Mapping):
                continue
            key = _text(value.get("hub_agent") or value.get("fleet_name"))
            if not key:
                raise FleetSshError("legacy fleet entries require hub_agent or fleet_name")
            if key in result:
                raise FleetSshError("duplicate fleet key %r" % key)
            result[key] = dict(value)
    return result


def resolve_fleet_key(config: Mapping[str, Any], requested: Optional[str]) -> str:
    """Resolve a requested fleet name or alias to its registry key."""
    fleets = fleet_entries(config)
    if not fleets:
        raise FleetSshError("fleet registry contains no fleets")
    if requested:
        if requested in fleets:
            return requested
        matches = [
            key
            for key, fleet in fleets.items()
            if requested in {_text(fleet.get("fleet_name")), _text(fleet.get("hub_agent"))}
        ]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(sorted(fleets))
        raise FleetSshError("fleet %r not found (known: %s)" % (requested, known))
    if len(fleets) == 1:
        return next(iter(fleets))
    defaults = [key for key, fleet in fleets.items() if fleet.get("default") is True]
    if len(defaults) == 1:
        return defaults[0]
    raise FleetSshError("multiple fleets are configured; select one explicitly")


@dataclass(frozen=True)
class FleetSshSpec:
    """Resolved operator-to-agent route with no secret key material."""

    fleet: str
    fleet_name: str
    agent: str
    target: str
    port: Optional[int]
    proxy_jump: Optional[str]
    identity_file: Optional[str]
    identity_ref: Optional[str]
    known_hosts_file: Optional[str]
    host_key_policy: str
    host_key_fingerprint: Optional[str]
    host_ca: Optional[str]
    supervisor: str
    os_kind: str
    control_port: int
    schema: str = SCHEMA

    @property
    def hub_agent(self) -> str:
        """Compatibility alias for callers resolving the hub route."""

        return self.agent

    @property
    def strict_host_key_checking(self) -> bool:
        """Compatibility view for the former boolean registry field."""

        return self.host_key_policy == "strict"

    def to_dict(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    def validate_portable(self) -> None:
        """Require every identity input needed on a clean HOME.

        Normal route resolution remains backward compatible with fleets that
        intentionally use an SSH agent or the default known_hosts file.  Login
        and exported client profiles call this stricter check.
        """

        if not (self.identity_file or self.identity_ref):
            raise FleetSshError(
                "fleet %r agent %r has no explicit identity_file/identity_ref"
                % (self.fleet, self.agent)
            )
        if self.host_key_policy == "strict" and not (
            self.known_hosts_file or self.host_key_fingerprint or self.host_ca
        ):
            raise FleetSshError(
                "fleet %r agent %r uses strict host-key checking without an "
                "explicit known_hosts file, fingerprint, or host CA" % (self.fleet, self.agent)
            )


def resolve_fleet_ssh(
    config: Mapping[str, Any],
    fleet: Optional[str],
    agent: Optional[str] = None,
    *,
    port_override: Optional[int] = None,
    portable: bool = False,
) -> FleetSshSpec:
    """Resolve one agent route, applying per-agent values before defaults."""

    fleet_key = resolve_fleet_key(config, fleet)
    fleet_cfg = fleet_entries(config)[fleet_key]
    defaults = fleet_cfg.get("defaults")
    defaults = dict(defaults) if isinstance(defaults, Mapping) else {}
    agents = _normalize_agents(fleet_cfg.get("agents"))
    agent_name = _text(agent or fleet_cfg.get("hub_agent"))
    if not agent_name:
        raise FleetSshError("fleet %r has no hub_agent; select --agent" % fleet_key)
    agent_cfg = agents.get(agent_name)
    if agent_cfg is None:
        known = ", ".join(sorted(agents)) or "(none)"
        raise FleetSshError(
            "agent %r is not in fleet %r (known: %s)" % (agent_name, fleet_key, known)
        )
    raw_target = _text(agent_cfg.get("target"))
    if not raw_target:
        raise FleetSshError("fleet %r agent %r has no target" % (fleet_key, agent_name))
    configured_port = (
        port_override
        if port_override is not None
        else _optional_int(agent_cfg.get("ssh_port"), field="ssh_port")
    )
    try:
        parsed_target = parse_ssh_target(raw_target, port=configured_port)
    except ValueError as exc:
        raise FleetSshError(str(exc)) from exc

    def inherited(*names: str) -> Any:
        for source in (agent_cfg, defaults):
            for name in names:
                value = source.get(name)
                if value not in (None, ""):
                    return value
        return None

    raw_policy = _text(inherited("ssh_host_key_policy", "host_key_policy")).lower()
    if not raw_policy:
        strict = inherited("ssh_strict_host_key_checking")
        # The old ``false`` value now means TOFU/accept-new.  Completely
        # disabling host verification requires the explicit ``insecure`` enum.
        raw_policy = "accept-new" if strict is False else "strict"
    if raw_policy not in HOST_KEY_POLICIES:
        raise FleetSshError(
            "ssh_host_key_policy must be one of: %s" % ", ".join(sorted(HOST_KEY_POLICIES))
        )

    identity_ref = _text(inherited("identity_ref")) or None
    identity_file = _local_path(inherited("identity_file", "ssh_key"))
    if identity_ref and identity_ref.startswith("file:") and not identity_file:
        identity_file = _local_path(identity_ref[len("file:") :])

    spec = FleetSshSpec(
        fleet=fleet_key,
        fleet_name=_text(fleet_cfg.get("fleet_name")) or fleet_key,
        agent=agent_name,
        target=parsed_target.user_host,
        port=parsed_target.port,
        proxy_jump=_text(inherited("ssh_jump", "proxy_jump")) or None,
        identity_file=identity_file,
        identity_ref=identity_ref,
        known_hosts_file=_local_path(inherited("ssh_known_hosts_file", "known_hosts_file")),
        host_key_policy=raw_policy,
        host_key_fingerprint=_text(inherited("ssh_host_key_fingerprint", "host_key_fingerprint"))
        or None,
        # ``host_ca`` is a client-local known_hosts-format file containing one
        # or more ``@cert-authority`` entries, not CA key material.
        host_ca=_local_path(inherited("ssh_host_ca", "host_ca")),
        supervisor=_text(inherited("supervisor")) or "auto",
        os_kind=_text(agent_cfg.get("os")) or "linux",
        control_port=_optional_int(fleet_cfg.get("control_port") or 8789, field="control_port")
        or 8789,
    )
    if portable:
        spec.validate_portable()
    return spec


def load_fleet_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and parse the fleets config mapping from disk."""
    target = Path(
        path or os.environ.get("MAC_FLEETS_CONFIG") or mac_paths.fleets_config()
    ).expanduser()
    if not target.is_file():
        raise FleetSshError("fleets config not found: %s" % target)
    try:
        import yaml  # type: ignore

        value = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise FleetSshError("could not parse %s: %s" % (target, exc)) from exc
    if not isinstance(value, dict):
        raise FleetSshError("unexpected fleets config shape in %s" % target)
    return value


def _route_options(
    spec: FleetSshSpec,
    *,
    kind: str,
    batch_mode: bool = True,
    connect_timeout: int = 10,
) -> List[str]:
    if kind not in {"ssh", "scp"}:
        raise FleetSshError("kind must be ssh or scp")
    argv = ["-F", os.devnull]
    if batch_mode:
        argv += ["-o", "BatchMode=yes"]
    argv += ["-o", "ConnectTimeout=%d" % max(1, int(connect_timeout))]
    policy_value = {
        "strict": "yes",
        "accept-new": "accept-new",
        "insecure": "no",
    }[spec.host_key_policy]
    argv += ["-o", "StrictHostKeyChecking=%s" % policy_value]
    trusted_hosts_file = spec.known_hosts_file or spec.host_ca
    if trusted_hosts_file:
        argv += ["-o", "UserKnownHostsFile=%s" % trusted_hosts_file]
    elif spec.host_key_policy == "insecure":
        argv += ["-o", "UserKnownHostsFile=%s" % os.devnull]
    if spec.identity_file:
        argv += ["-o", "IdentitiesOnly=yes", "-i", spec.identity_file]
    elif spec.identity_ref and not spec.identity_ref.startswith("file:"):
        raise FleetSshError(
            "identity_ref %r cannot be converted to OpenSSH argv on this client" % spec.identity_ref
        )
    if spec.proxy_jump:
        argv += ["-o", "ProxyJump=%s" % spec.proxy_jump]
    if spec.port is not None:
        argv += ["-P" if kind == "scp" else "-p", str(spec.port)]
    return argv


def route_argv(
    spec: FleetSshSpec,
    *,
    kind: str = "ssh",
    batch_mode: bool = True,
    connect_timeout: int = 10,
) -> List[str]:
    """Return route options followed by the target (no executable)."""

    return _route_options(
        spec,
        kind=kind,
        batch_mode=batch_mode,
        connect_timeout=connect_timeout,
    ) + [spec.target]


def ssh_argv(
    spec: FleetSshSpec,
    remote_command: Optional[str] = None,
    *,
    extra: Iterable[str] = (),
    batch_mode: bool = True,
    connect_timeout: int = 10,
) -> List[str]:
    """Build the ``ssh`` argv for the given fleet SSH spec."""
    argv = ["ssh"] + _route_options(
        spec,
        kind="ssh",
        batch_mode=batch_mode,
        connect_timeout=connect_timeout,
    )
    argv.extend(str(item) for item in extra)
    argv.append(spec.target)
    if remote_command is not None:
        argv.append(remote_command)
    return argv


def scp_argv(
    spec: FleetSshSpec,
    sources: Iterable[str],
    destination: str,
    *,
    extra: Iterable[str] = (),
    batch_mode: bool = True,
    connect_timeout: int = 10,
) -> List[str]:
    """Build the ``scp`` argv for the given fleet SSH spec."""
    argv = ["scp"] + _route_options(
        spec,
        kind="scp",
        batch_mode=batch_mode,
        connect_timeout=connect_timeout,
    )
    argv.extend(str(item) for item in extra)
    argv.extend(str(item) for item in sources)
    argv.append(destination)
    return argv


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="resolve a fleet SSH route")
    parser.add_argument("--config")
    parser.add_argument("--fleet")
    parser.add_argument("--agent")
    parser.add_argument("--kind", choices=("spec", "ssh", "scp"), default="spec")
    parser.add_argument("--port-override", type=int)
    parser.add_argument("--portable", action="store_true")
    parser.add_argument("--nul", action="store_true")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run the fleet-ssh command-line interface."""
    args = _build_parser().parse_args(argv)
    try:
        spec = resolve_fleet_ssh(
            load_fleet_config(args.config),
            args.fleet,
            args.agent,
            port_override=args.port_override,
            portable=args.portable,
        )
        if args.kind == "spec":
            sys.stdout.write(json.dumps(spec.to_dict(), sort_keys=True) + "\n")
            return 0
        values = route_argv(spec, kind=args.kind)
        if args.nul:
            sys.stdout.buffer.write(b"\0".join(v.encode("utf-8") for v in values) + b"\0")
        else:
            sys.stdout.write(json.dumps(values) + "\n")
        return 0
    except FleetSshError as exc:
        print("mac admin fleet ssh route: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
