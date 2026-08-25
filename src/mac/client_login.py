"""SSH-first client login and reconnect-on-use tunnel sessions.

The bootstrap bearer exists only in memory until an authenticated API request
has succeeded through the verified SSH tunnel.  Session state never contains a
token or private-key material; it records only the managed SSH PID and route
metadata needed to stop or recover the tunnel safely.
"""

from __future__ import annotations

import contextlib
import getpass
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Tuple

from mac.client_profiles import (
    ClientProfileError,
    _name,
    active_profile_name,
    install_enrollment_manifest,
    list_profiles,
    load_profile,
    remove_profile,
    validate_enrollment_manifest,
)
from mac.client_principals import mac_home
from mac.fleet_deploy import parse_ssh_target
from mac.fleet_ssh import (
    FleetSshError,
    FleetSshSpec,
    load_fleet_config,
    resolve_fleet_ssh,
    ssh_argv,
)


SESSION_SCHEMA = "mac.login_session.v1"
DEFAULT_SCOPES = ("read", "write", "dispatch")
_FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/=]+")


class ClientLoginError(RuntimeError):
    """A secret-safe client login or session lifecycle failure."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_client_id() -> str:
    """Derive a default client id from the current user and hostname."""
    raw = "%s-%s" % (getpass.getuser(), socket.gethostname().split(".", 1)[0])
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-_").lower()
    return (value or "mac-client")[:64]


def sessions_root() -> Path:
    """Return the directory that stores per-profile session state."""
    return mac_home() / "sessions"


def _state_path(profile: str) -> Path:
    return sessions_root() / ("%s.json" % _name(profile))


def managed_known_hosts_path(profile: str) -> Path:
    """Return the managed known_hosts path for the given profile."""
    return mac_home() / "ssh" / ("%s.known_hosts" % _name(profile))


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _atomic_text(path: Path, value: str) -> None:
    import tempfile

    _ensure_private_dir(path.parent)
    descriptor, raw = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _write_state(profile: str, state: Mapping[str, Any]) -> Dict[str, Any]:
    value = {"schema": SESSION_SCHEMA, "profile": _name(profile), **dict(state)}
    value["updated_at"] = _timestamp()
    _atomic_text(_state_path(profile), json.dumps(value, indent=2, sort_keys=True) + "\n")
    return value


def _read_state(profile: str) -> Dict[str, Any]:
    path = _state_path(profile)
    if not path.is_file():
        return {}
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ClientLoginError("login session state has unsafe permissions: %s" % path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientLoginError("could not read login session state: %s" % exc) from exc
    if not isinstance(value, dict) or value.get("schema") != SESSION_SCHEMA:
        raise ClientLoginError("login session state has an unsupported schema")
    return value


def _remove_state(profile: str) -> None:
    _state_path(profile).unlink(missing_ok=True)


@contextlib.contextmanager
def _session_lock(profile: str) -> Iterator[None]:
    _ensure_private_dir(sessions_root())
    path = sessions_root() / ("%s.lock" % _name(profile))
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - supported hosts are POSIX
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover
            pass
        os.close(descriptor)


def _private_identity(path: Optional[str]) -> str:
    if not path:
        raise ClientLoginError("login requires an explicit SSH identity file")
    identity = Path(path).expanduser().resolve()
    if not identity.is_file():
        raise ClientLoginError("SSH identity file does not exist: %s" % identity)
    if os.name != "nt" and identity.stat().st_mode & 0o077:
        raise ClientLoginError(
            "SSH identity file permissions are too broad; run chmod 600 %s" % identity
        )
    return str(identity)


def _existing_fingerprints(path: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["ssh-keygen", "-lf", str(path), "-E", "sha256"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ClientLoginError("ssh-keygen is required to verify the hub host key") from exc
    if result.returncode != 0:
        raise ClientLoginError("could not inspect SSH host-key file: %s" % path)
    return set(_FINGERPRINT.findall(result.stdout))


def _target_host(target: str) -> str:
    host = target.rsplit("@", 1)[-1]
    return host[1:-1] if host.startswith("[") and host.endswith("]") else host


def _pin_scanned_fingerprint(
    spec: FleetSshSpec, profile: str, fingerprint: str, *, timeout: int
) -> str:
    if spec.proxy_jump:
        raise ClientLoginError(
            "fingerprint discovery cannot traverse ProxyJump; supply a verified "
            "--known-hosts-file containing hub and jump-host keys"
        )
    try:
        scan = subprocess.run(
            [
                "ssh-keyscan",
                "-T",
                str(max(1, timeout)),
                "-p",
                str(spec.port or 22),
                _target_host(spec.target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ClientLoginError("ssh-keyscan is required to pin the hub host key") from exc
    selected = []
    for line in scan.stdout.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            checked = subprocess.run(
                ["ssh-keygen", "-lf", "-", "-E", "sha256"],
                input=line + "\n",
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise ClientLoginError("ssh-keygen is required to pin the hub host key") from exc
        if checked.returncode == 0 and fingerprint in _FINGERPRINT.findall(checked.stdout):
            selected.append(line)
    if not selected:
        raise ClientLoginError("the hub SSH host key does not match the pinned fingerprint")
    path = managed_known_hosts_path(profile)
    _atomic_text(path, "\n".join(selected) + "\n")
    return str(path)


def prepare_login_spec(
    spec: FleetSshSpec, profile: str, *, connect_timeout: int = 10
) -> Tuple[FleetSshSpec, Optional[Path]]:
    """Validate any explicit identity/host trust, else defer to OpenSSH.

    Historically login *required* an explicit identity file plus pinned host
    trust and refused to fall back on anything ambient.  That surprised
    operators: plain ``ssh <host>`` already works from their standard
    ``~/.ssh`` setup, yet ``mac admin login --ssh <host>`` demanded its own files.

    Login now follows the principle of least astonishment — when an input is
    not supplied (by flag or fleet config) it lets OpenSSH resolve it the usual
    way: default identities and the agent for the key, and the default
    ``~/.ssh/known_hosts`` for host verification.  (mac still runs ssh with
    ``-F /dev/null``, so ``~/.ssh/config`` host aliases are not consulted, but
    the default key and known_hosts files are.)  Any input that *is* supplied
    is still validated and strictly pinned exactly as before, so
    explicitly-configured fleets and exported client profiles keep their
    reproducible, no-ambient-state guarantees.
    """

    # --- Identity: validate an explicit key file, else defer to ssh. ---
    if spec.identity_file:
        identity = _private_identity(spec.identity_file)
        spec = replace(spec, identity_file=identity, identity_ref=None)
    else:
        # No key file given: let ssh pick its default identities / the agent.
        # Drop any non-file identity_ref so argv construction stays valid.
        spec = replace(spec, identity_file=None, identity_ref=None)

    # --- Host trust: pin an explicit file/fingerprint, else defer to ssh. ---
    trust_file = spec.known_hosts_file or spec.host_ca
    fingerprint = str(spec.host_key_fingerprint or "").strip()
    if trust_file:
        spec = replace(spec, host_key_policy="strict")
        trust_path = Path(trust_file).expanduser().resolve()
        if not trust_path.is_file():
            raise ClientLoginError("SSH host-key file does not exist: %s" % trust_path)
        if fingerprint and fingerprint not in _existing_fingerprints(trust_path):
            raise ClientLoginError("the SSH host-key file does not contain the pinned fingerprint")
        if spec.known_hosts_file:
            spec = replace(spec, known_hosts_file=str(trust_path))
        else:
            spec = replace(spec, host_ca=str(trust_path))
        return spec, None
    if fingerprint:
        spec = replace(spec, host_key_policy="strict")
        path = _pin_scanned_fingerprint(spec, profile, fingerprint, timeout=connect_timeout)
        return replace(spec, known_hosts_file=path), Path(path)

    # No explicit trust: verify against the operator's default known_hosts using
    # accept-new (TOFU), mirroring interactive ssh's first-connect behavior
    # rather than failing in batch mode.  A pre-existing host key is still
    # enforced; a brand-new one is recorded on first use.  An explicit
    # ``insecure`` policy (host verification deliberately off) is preserved.
    policy = "insecure" if spec.host_key_policy == "insecure" else "accept-new"
    return replace(spec, host_key_policy=policy), None


def resolve_login_spec(
    *,
    ssh_target: Optional[str],
    fleet: Optional[str],
    agent: Optional[str],
    fleets_config: Optional[str],
    ssh_port: Optional[int],
    proxy_jump: Optional[str],
    identity_file: Optional[str],
    known_hosts_file: Optional[str],
    host_key_fingerprint: Optional[str],
    host_ca: Optional[str],
    remote_port: Optional[int],
) -> FleetSshSpec:
    """Resolve login options into an SSH connection spec."""
    if ssh_target:
        try:
            parsed = parse_ssh_target(ssh_target, port=ssh_port)
        except ValueError as exc:
            raise ClientLoginError(str(exc)) from exc
        return FleetSshSpec(
            fleet=str(fleet or "default"),
            fleet_name=str(fleet or "default"),
            agent=str(agent or "hub"),
            target=parsed.user_host,
            port=parsed.port,
            proxy_jump=proxy_jump,
            identity_file=str(Path(identity_file).expanduser()) if identity_file else None,
            identity_ref=None,
            known_hosts_file=str(Path(known_hosts_file).expanduser()) if known_hosts_file else None,
            host_key_policy="strict",
            host_key_fingerprint=host_key_fingerprint,
            host_ca=str(Path(host_ca).expanduser()) if host_ca else None,
            supervisor="client-login",
            os_kind="linux",
            control_port=int(remote_port or 8789),
        )
    if not fleet:
        raise ClientLoginError("select --ssh <user@hub> or --fleet <name>")
    try:
        spec = resolve_fleet_ssh(
            load_fleet_config(fleets_config),
            fleet,
            agent,
            port_override=ssh_port,
            portable=False,
        )
    except FleetSshError as exc:
        raise ClientLoginError(str(exc)) from exc
    return replace(
        spec,
        proxy_jump=proxy_jump or spec.proxy_jump,
        identity_file=str(Path(identity_file).expanduser())
        if identity_file
        else spec.identity_file,
        known_hosts_file=str(Path(known_hosts_file).expanduser())
        if known_hosts_file
        else spec.known_hosts_file,
        host_key_fingerprint=host_key_fingerprint or spec.host_key_fingerprint,
        host_ca=str(Path(host_ca).expanduser()) if host_ca else spec.host_ca,
        control_port=int(remote_port or spec.control_port),
    )


def _port_open(port: int, *, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def choose_local_port(requested: Optional[int] = None) -> int:
    """Return a bindable local port, defaulting to an OS-assigned one."""
    port = int(requested or 0)
    if port < 0 or port > 65535:
        raise ClientLoginError("local port must be between 1 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ClientLoginError("local port %d is already in use" % port) from exc
        return int(probe.getsockname()[1])


def _tunnel_argv(
    spec: FleetSshSpec, local_port: int, remote_host: str, remote_port: int, timeout: int
) -> list[str]:
    forward = "127.0.0.1:%d:%s:%d" % (local_port, remote_host, remote_port)
    return ssh_argv(
        spec,
        extra=(
            "-N",
            "-T",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            forward,
        ),
        connect_timeout=timeout,
    )


def _terminate_process(process: Any) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _start_tunnel(
    spec: FleetSshSpec,
    local_port: int,
    remote_host: str,
    remote_port: int,
    *,
    timeout: int,
) -> Any:
    try:
        process = subprocess.Popen(
            _tunnel_argv(spec, local_port, remote_host, remote_port, timeout),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as exc:
        raise ClientLoginError("could not start ssh; verify that OpenSSH is installed") from exc
    deadline = time.monotonic() + max(1, timeout)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ClientLoginError(
                "SSH tunnel exited before becoming ready; verify identity, host key, and route"
            )
        if _port_open(local_port):
            return process
        time.sleep(0.05)
    _terminate_process(process)
    raise ClientLoginError("SSH tunnel did not become ready; verify the hub API port and route")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _managed_process(state: Mapping[str, Any]) -> bool:
    pid = int(state.get("ssh_pid") or 0)
    target = str(state.get("ssh_target") or "")
    if pid <= 0 or not target or not _pid_alive(pid):
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    command = result.stdout if result.returncode == 0 else ""
    return bool(command and "ssh" in command and "-L" in command and target in command)


def _stop_managed_state(state: Mapping[str, Any], *, timeout: float = 3.0) -> bool:
    if not _managed_process(state):
        return False
    pid = int(state["ssh_pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + max(0.1, timeout)
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _pid_alive(pid)


def _api_url(local_port: int) -> str:
    return "http://127.0.0.1:%d" % local_port


def _validate_token(api_url: str, token: str, *, timeout: int) -> Tuple[bool, str]:
    request = urllib.request.Request(
        api_url.rstrip("/") + "/tasks/stats",
        headers={"Accept": "application/json", "Authorization": "Bearer %s" % token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1, timeout)) as response:
            value = json.loads(response.read().decode("utf-8") or "{}")
        return (
            isinstance(value, dict),
            "authenticated" if isinstance(value, dict) else "invalid_response",
        )
    except urllib.error.HTTPError as exc:
        return False, "credential_rejected" if exc.code in {401, 403} else "hub_http_error"
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False, "hub_unreachable"


# POSIX-sh probe that prints ``MAC_BIN=<abs path>`` for the first usable remote
# ``mac``.  A non-interactive ``ssh host cmd`` shell does not source the login
# profile, so a user-scoped install is not on PATH — we look where console
# scripts actually land before falling back to the shell's own ``command -v``.
# Output is sentinel-tagged so any profile banner noise is trivially ignored.
_REMOTE_MAC_PROBE = (
    'for c in "$HOME/.local/bin/mac" "$HOME/.venv/bin/mac" '
    '"/usr/local/bin/mac" "/opt/homebrew/bin/mac" "/usr/bin/mac"; do '
    'if [ -x "$c" ]; then printf "MAC_BIN=%s\\n" "$c"; exit 0; fi; done; '
    'p="$(command -v mac 2>/dev/null || true)"; '
    'if [ -n "$p" ]; then printf "MAC_BIN=%s\\n" "$p"; exit 0; fi; '
    "exit 3"
)


def _resolve_remote_mac(spec: FleetSshSpec, *, timeout: int) -> Optional[str]:
    """Best-effort discovery of the remote ``mac`` executable's absolute path.

    Because a non-interactive SSH command shell skips the operator's login
    profile, a bare ``mac`` invocation fails with exit 127 when MAC is installed
    under ``~/.local/bin`` or a venv.  Rather than force the operator to hand
    ``--remote-mac <abs path>`` (a deployment detail they should not have to
    know), probe the well-known install locations and the shell's own
    ``command -v``.  Returns the discovered path, or None when discovery cannot
    run or finds nothing — leaving the caller to fall back to a bare ``mac``.
    """

    try:
        result = subprocess.run(
            ssh_argv(spec, _REMOTE_MAC_PROBE, extra=("-T",), connect_timeout=timeout),
            capture_output=True,
            text=True,
            check=False,
            timeout=max(30, timeout * 3),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        if line.startswith("MAC_BIN="):
            path = line[len("MAC_BIN=") :].strip()
            if path:
                return path
    return None


def _run_remote_json(spec: FleetSshSpec, command: Iterable[str], *, timeout: int) -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ssh_argv(
                spec,
                shlex.join([str(item) for item in command]),
                extra=("-T",),
                connect_timeout=timeout,
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=max(30, timeout * 3),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientLoginError("SSH enrollment command could not complete") from exc
    if result.returncode != 0:
        # Reporting only the exit code threw away the one thing that explained
        # the failure. A caller saw
        #     "SSH enrollment command failed (exit 2); verify hub MAC
        #      installation and requested scopes"
        # while the remote had said, precisely:
        #     "mac: `client` moved under `admin`. Run `mac admin client`"
        # The advice was wrong -- installation and scopes were both fine -- and
        # diagnosing it required SSHing in and running the command by hand.
        #
        # But stderr is NOT safe to echo wholesale: enrollment carries tokens,
        # and `test_remote_json_and_action_fail_closed` exists precisely to
        # ensure a secret in the remote's output never reaches this message.
        # That test is right, and it caught an earlier version of this change.
        #
        # So: surface only the CLI's OWN guidance lines. Those begin with
        # "mac: " because the CLI emits them itself, they are program-authored
        # rather than data, and they are exactly the class of message that
        # explains a command-shape mismatch. Anything else stays withheld.
        guidance = [
            line.strip()
            for line in (result.stderr or "").splitlines()
            if line.strip().startswith("mac: ")
        ]
        if guidance:
            raise ClientLoginError(
                "SSH enrollment command failed (exit %d): %s"
                % (result.returncode, guidance[0][:300])
            )
        raise ClientLoginError(
            "SSH enrollment command failed (exit %d); verify hub MAC "
            "installation and requested scopes" % result.returncode
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ClientLoginError("hub returned a malformed enrollment manifest") from exc
    if not isinstance(value, dict):
        raise ClientLoginError("hub returned a malformed enrollment manifest")
    return value


def _run_remote_action(spec: FleetSshSpec, command: Iterable[str], *, timeout: int) -> None:
    try:
        result = subprocess.run(
            ssh_argv(
                spec,
                shlex.join([str(item) for item in command]),
                extra=("-T",),
                connect_timeout=timeout,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(30, timeout * 3),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientLoginError("SSH revocation command could not complete") from exc
    if result.returncode != 0:
        raise ClientLoginError("SSH revocation command failed (exit %d)" % result.returncode)


def _profile_manifest(
    manifest: Mapping[str, Any],
    spec: FleetSshSpec,
    *,
    profile: str,
    local_port: int,
    remote_host: str,
    remote_port: int,
) -> Dict[str, Any]:
    value = json.loads(json.dumps(dict(manifest)))
    value["profile"] = profile
    value["fleet"] = spec.fleet_name or spec.fleet
    value["connection"] = {
        "api_url": _api_url(local_port),
        "mode": "ssh-tunnel",
        "local_port": local_port,
        "remote_host": remote_host,
        "remote_port": remote_port,
    }
    value["ssh"] = {
        key: item
        for key, item in {
            "target": spec.target,
            "port": spec.port,
            "proxy_jump": spec.proxy_jump,
            "identity_file": spec.identity_file,
            "known_hosts_file": spec.known_hosts_file,
            "host_key_policy": spec.host_key_policy,
            "host_key_fingerprint": spec.host_key_fingerprint,
            "host_ca": spec.host_ca,
        }.items()
        if item not in (None, "")
    }
    validate_enrollment_manifest(value, profile_override=profile)
    return value


def _spec_from_profile(profile: Mapping[str, Any]) -> FleetSshSpec:
    ssh = dict(profile.get("ssh") or {})
    connection = dict(profile.get("connection") or {})
    return FleetSshSpec(
        fleet=str(profile.get("fleet") or profile.get("profile") or "default"),
        fleet_name=str(profile.get("fleet") or profile.get("profile") or "default"),
        agent="hub",
        target=str(ssh.get("target") or ""),
        port=int(ssh["port"]) if ssh.get("port") is not None else None,
        proxy_jump=str(ssh.get("proxy_jump") or "") or None,
        identity_file=str(ssh.get("identity_file") or "") or None,
        identity_ref=str(ssh.get("identity_ref") or "") or None,
        known_hosts_file=str(ssh.get("known_hosts_file") or "") or None,
        host_key_policy=str(ssh.get("host_key_policy") or "strict"),
        host_key_fingerprint=str(ssh.get("host_key_fingerprint") or "") or None,
        host_ca=str(ssh.get("host_ca") or "") or None,
        supervisor="client-login",
        os_kind="linux",
        control_port=int(connection.get("remote_port") or 8789),
    )


def _state_for(
    profile: str,
    spec: FleetSshSpec,
    process: Any,
    *,
    local_port: int,
    remote_host: str,
    remote_port: int,
) -> Dict[str, Any]:
    return _write_state(
        profile,
        {
            "status": "running",
            "ssh_pid": int(process.pid),
            "ssh_target": spec.target,
            "local_port": local_port,
            "remote_host": remote_host,
            "remote_port": remote_port,
            "started_at": _timestamp(),
        },
    )


def login(
    *,
    spec: FleetSshSpec,
    profile: str,
    client_id: str,
    display_name: str = "",
    scopes: Iterable[str] = DEFAULT_SCOPES,
    capabilities: Iterable[str] = (),
    expires_in: int = 30 * 24 * 60 * 60,
    local_port: Optional[int] = None,
    remote_host: str = "127.0.0.1",
    remote_port: int = 8789,
    allow_elevated: bool = False,
    rotate: bool = False,
    remote_mac: str = "mac",
    connect_timeout: int = 10,
) -> Dict[str, Any]:
    """Log in through the resolved SSH spec and persist the client profile."""
    profile = _name(profile)
    created_pin: Optional[Path] = None
    process = None
    issued = False
    with _session_lock(profile):
        exists = any(item.get("profile") == profile for item in list_profiles())
        if exists and not rotate:
            raise ClientLoginError(
                "client profile %r already exists; use `mac admin login renew` or --rotate"
                % profile
            )
        previous_state = _read_state(profile)
        if previous_state:
            _stop_managed_state(previous_state)
            _remove_state(profile)
        previous_pin = None
        managed_pin = managed_known_hosts_path(profile)
        if managed_pin.is_file():
            previous_pin = managed_pin.read_text(encoding="utf-8")
        try:
            prepared, created_pin = prepare_login_spec(
                spec, profile, connect_timeout=connect_timeout
            )
            # Discover where ``mac`` lives on the hub unless the operator pinned
            # it explicitly.  This keeps the remote install path an internal
            # detail rather than something the operator must pass by hand.
            if remote_mac == "mac":
                remote_mac = _resolve_remote_mac(prepared, timeout=connect_timeout) or remote_mac
            selected_port = choose_local_port(local_port)
            process = _start_tunnel(
                prepared,
                selected_port,
                remote_host,
                remote_port,
                timeout=connect_timeout,
            )
            command = [
                remote_mac,
                "--json",
                "admin",
                "client",
                "enroll",
                client_id,
                "--name",
                display_name or client_id,
                "--fleet",
                prepared.fleet_name or prepared.fleet,
                "--profile",
                profile,
                "--api-url",
                "http://%s:%d" % (remote_host, remote_port),
                "--scopes",
                ",".join(str(item) for item in scopes),
                "--expires-in",
                str(int(expires_in)),
                "--actor",
                "mac-login",
            ]
            if capabilities:
                command += ["--capabilities", ",".join(str(item) for item in capabilities)]
            if allow_elevated:
                command.append("--allow-elevated")
            if rotate:
                command.append("--rotate")
            remote_manifest = _run_remote_json(prepared, command, timeout=connect_timeout)
            issued = True
            manifest = _profile_manifest(
                remote_manifest,
                prepared,
                profile=profile,
                local_port=selected_port,
                remote_host=remote_host,
                remote_port=remote_port,
            )
            token = str((manifest.get("credential") or {}).get("token") or "")
            valid, reason = _validate_token(_api_url(selected_port), token, timeout=connect_timeout)
            if not valid:
                raise ClientLoginError(
                    "hub rejected the enrolled credential through the SSH tunnel (%s)" % reason
                )
            _state_for(
                profile,
                prepared,
                process,
                local_port=selected_port,
                remote_host=remote_host,
                remote_port=remote_port,
            )
            result = install_enrollment_manifest(manifest, profile_override=profile, activate=True)
            return {
                "status": "logged_in",
                "profile": profile,
                "client_id": manifest.get("client_id"),
                "fleet": manifest.get("fleet"),
                "api_url": _api_url(selected_port),
                "scopes": list((manifest.get("credential") or {}).get("scopes") or []),
                "expires_at": (manifest.get("credential") or {}).get("expires_at"),
                "session": {"status": "running", "local_port": selected_port},
                "changed": bool(result.get("changed")),
            }
        except Exception as exc:
            _remove_state(profile)
            _terminate_process(process)
            if issued:
                try:
                    _run_remote_action(
                        prepared,
                        [
                            remote_mac,
                            "admin",
                            "client",
                            "revoke",
                            client_id,
                            "--actor",
                            "mac-login-rollback",
                        ],
                        timeout=connect_timeout,
                    )
                except Exception:
                    pass
            if created_pin is not None:
                if previous_pin is None:
                    created_pin.unlink(missing_ok=True)
                else:
                    _atomic_text(created_pin, previous_pin)
            if isinstance(exc, (ClientLoginError, ClientProfileError)):
                raise ClientLoginError(str(exc)) from exc
            raise ClientLoginError("login failed before credentials could be committed") from exc


def local_console_login(
    *,
    profile: str,
    client_id: str,
    display_name: str = "",
    fleet: str = "",
    scopes: Iterable[str] = DEFAULT_SCOPES,
    capabilities: Iterable[str] = (),
    expires_in: int = 30 * 24 * 60 * 60,
    api_url: Optional[str] = None,
    socket_path: Optional[str] = None,
    allow_elevated: bool = False,
    rotate: bool = False,
    connect_timeout: int = 10,
) -> Dict[str, Any]:
    """Enroll through the API service's kernel-authenticated Unix socket."""
    from mac.client_principals import ELEVATED_SCOPES
    from mac.local_console import (
        DEFAULT_SCOPES as LOCAL_DEFAULT_SCOPES,
        LocalConsoleError,
        default_api_url,
        request_local_console,
    )

    profile = _name(profile)
    normalized_scopes = [str(item).strip().lower() for item in scopes if str(item).strip()]
    privileged = set(normalized_scopes) - set(LOCAL_DEFAULT_SCOPES)
    if privileged and not allow_elevated:
        raise ClientLoginError(
            "local-console scopes outside read,write,dispatch require explicit "
            "--allow-elevated and authorization by the service"
        )
    if set(normalized_scopes) & ELEVATED_SCOPES and not allow_elevated:
        raise ClientLoginError("elevated scopes require explicit --allow-elevated")
    resolved_api_url = (api_url or default_api_url()).rstrip("/")
    issued = False
    with _session_lock(profile):
        exists = any(item.get("profile") == profile for item in list_profiles())
        if exists and not rotate:
            raise ClientLoginError("client profile %r already exists; use --rotate" % profile)
        request = {
            "action": "enroll",
            "client_id": client_id,
            "display_name": display_name or client_id,
            "fleet": fleet,
            "profile": profile,
            "scopes": normalized_scopes,
            "capabilities": [str(item) for item in capabilities],
            "expires_in": int(expires_in),
            "api_url": resolved_api_url,
            "allow_elevated": bool(allow_elevated),
            "rotate": bool(rotate),
        }
        try:
            manifest = request_local_console(
                request, socket_path=socket_path, timeout=connect_timeout
            )
            issued = True
            validate_enrollment_manifest(manifest, profile_override=profile)
            manifest_api_url = str((manifest.get("connection") or {}).get("api_url") or "").rstrip(
                "/"
            )
            if manifest_api_url != resolved_api_url:
                raise ClientLoginError("local-console manifest changed the requested API authority")
            token = str((manifest.get("credential") or {}).get("token") or "")
            valid, reason = _validate_token(resolved_api_url, token, timeout=connect_timeout)
            if not valid:
                raise ClientLoginError("hub rejected the local-console credential (%s)" % reason)
            result = install_enrollment_manifest(manifest, profile_override=profile, activate=True)
            return {
                "status": "logged_in",
                "profile": profile,
                "client_id": manifest.get("client_id"),
                "fleet": manifest.get("fleet"),
                "api_url": resolved_api_url,
                "scopes": list((manifest.get("credential") or {}).get("scopes") or []),
                "expires_at": (manifest.get("credential") or {}).get("expires_at"),
                "session": {"status": "direct"},
                "changed": bool(result.get("changed")),
            }
        except Exception as exc:
            if issued:
                try:
                    request_local_console(
                        {"action": "revoke", "client_id": client_id},
                        socket_path=socket_path,
                        timeout=connect_timeout,
                    )
                except Exception:
                    pass
            if isinstance(exc, (ClientLoginError, ClientProfileError, LocalConsoleError)):
                raise ClientLoginError(str(exc)) from exc
            raise ClientLoginError(
                "local-console login failed before credentials could be committed"
            ) from exc


def renew_local_console_login(
    profile: Optional[str] = None,
    *,
    expires_in: int = 30 * 24 * 60 * 60,
    socket_path: Optional[str] = None,
    allow_elevated: bool = False,
    connect_timeout: int = 10,
) -> Dict[str, Any]:
    """Rotate a direct login through the kernel-authenticated Unix socket."""
    from mac.local_console import LocalConsoleError, request_local_console

    selected = _name(profile) if profile else active_profile_name()
    if not selected:
        raise ClientLoginError("no active login profile")
    with _session_lock(selected):
        current = load_profile(selected, include_token=True)
        connection = dict(current.get("connection") or {})
        if connection.get("mode") != "direct":
            raise ClientLoginError(
                "--local-console renewal requires a direct local-console profile"
            )
        credential = dict(current.get("credential") or {})
        privileged = set(credential.get("scopes") or []) - {
            "read",
            "write",
            "dispatch",
        }
        if privileged and not allow_elevated:
            raise ClientLoginError(
                "renewing local-console scopes outside read,write,dispatch "
                "requires explicit --allow-elevated and authorization by the service"
            )
        client_id = str(current.get("client_id") or "")
        api_url = str(connection.get("api_url") or "").rstrip("/")
        if int(expires_in) < 60:
            raise ClientLoginError("expires-in must be at least 60 seconds")
        renew_attempted = False
        issued = False
        install_started = False
        try:
            renew_attempted = True
            manifest = request_local_console(
                {
                    "action": "renew",
                    "client_id": client_id,
                    "expires_in": int(expires_in),
                    "allow_elevated": bool(allow_elevated),
                },
                socket_path=socket_path,
                timeout=connect_timeout,
            )
            issued = True
            validate_enrollment_manifest(manifest, profile_override=selected)
            manifest_api_url = str((manifest.get("connection") or {}).get("api_url") or "").rstrip(
                "/"
            )
            if manifest_api_url != api_url:
                raise ClientLoginError("local-console renewal changed the profile API authority")
            token = str((manifest.get("credential") or {}).get("token") or "")
            valid, reason = _validate_token(api_url, token, timeout=connect_timeout)
            if not valid:
                raise ClientLoginError(
                    "renewed local-console credential failed validation (%s)" % reason
                )
            install_started = True
            result = install_enrollment_manifest(manifest, profile_override=selected, activate=True)
            return {
                "status": "renewed",
                "profile": selected,
                "client_id": client_id,
                "api_url": api_url,
                "scopes": list((manifest.get("credential") or {}).get("scopes") or []),
                "expires_at": (manifest.get("credential") or {}).get("expires_at"),
                "changed": bool(result.get("changed")),
            }
        except Exception as exc:
            if renew_attempted:
                try:
                    request_local_console(
                        {"action": "revoke", "client_id": client_id},
                        socket_path=socket_path,
                        timeout=connect_timeout,
                    )
                except Exception as rollback_exc:
                    if install_started:
                        raise ClientLoginError(
                            "local-console renewal failed and rollback revocation "
                            "could not be confirmed; local profile state should be "
                            "inspected"
                        ) from rollback_exc
                    raise ClientLoginError(
                        "local-console renewal outcome and rollback revocation "
                        "could not be confirmed; the local profile was not "
                        "replaced and its credential validity is unknown"
                    ) from rollback_exc
                if install_started:
                    raise ClientLoginError(
                        "%s; the new credential was revoked, but profile "
                        "installation failed and local state should be inspected" % exc
                    ) from exc
                credential = "new credential" if issued else "hub credential"
                raise ClientLoginError(
                    "%s; the %s was revoked and the local profile was not "
                    "replaced; its old credential is no longer valid" % (exc, credential)
                ) from exc
            if isinstance(exc, (ClientLoginError, ClientProfileError, LocalConsoleError)):
                raise ClientLoginError(str(exc)) from exc
            raise ClientLoginError(
                "local-console renewal failed before a credential was issued"
            ) from exc


def _ensure_session_unlocked(profile_name: str) -> Dict[str, Any]:
    profile = load_profile(profile_name, include_token=True)
    connection = dict(profile.get("connection") or {})
    if connection.get("mode") != "ssh-tunnel":
        return {"status": "direct", "profile": profile_name}
    local_port = int(connection.get("local_port") or 0)
    remote_host = str(connection.get("remote_host") or "127.0.0.1")
    remote_port = int(connection.get("remote_port") or 8789)
    state = _read_state(profile_name)
    if state.get("local_port") == local_port and _managed_process(state) and _port_open(local_port):
        return {"status": "running", "profile": profile_name, "local_port": local_port}
    if state:
        _stop_managed_state(state)
        _remove_state(profile_name)
    if _port_open(local_port):
        raise ClientLoginError(
            "local port %d is occupied by an unmanaged process; stop it or log in again"
            % local_port
        )
    spec, _created = prepare_login_spec(_spec_from_profile(profile), profile_name)
    process = _start_tunnel(spec, local_port, remote_host, remote_port, timeout=10)
    token = str((profile.get("credential") or {}).get("token") or "")
    valid, reason = _validate_token(_api_url(local_port), token, timeout=10)
    if not valid:
        _terminate_process(process)
        raise ClientLoginError("reconnected tunnel failed authentication (%s)" % reason)
    _state_for(
        profile_name,
        spec,
        process,
        local_port=local_port,
        remote_host=remote_host,
        remote_port=remote_port,
    )
    return {"status": "reconnected", "profile": profile_name, "local_port": local_port}


def ensure_session(profile: str) -> Dict[str, Any]:
    """Ensure a live SSH-tunnel session exists for the profile."""
    profile = _name(profile)
    with _session_lock(profile):
        return _ensure_session_unlocked(profile)


def login_status(profile: Optional[str] = None) -> Dict[str, Any]:
    """Report connection and authentication status for a login profile."""
    selected = _name(profile) if profile else active_profile_name()
    if not selected:
        raise ClientLoginError("no active login profile")
    value = load_profile(selected, include_token=True)
    connection = dict(value.get("connection") or {})
    credential = dict(value.get("credential") or {})
    mode = str(connection.get("mode") or "direct")
    state = _read_state(selected) if mode == "ssh-tunnel" else {}
    local_port = int(connection.get("local_port") or 0)
    managed = bool(state and _managed_process(state))
    reachable = _port_open(local_port) if local_port else mode == "direct"
    authenticated = False
    reason = "not_connected"
    if reachable:
        authenticated, reason = _validate_token(
            str(connection.get("api_url") or ""),
            str(credential.get("token") or ""),
            timeout=3,
        )
    status = (
        "connected" if authenticated else ("stopped" if not reachable else "credential_rejected")
    )
    return {
        "status": status,
        "profile": selected,
        "client_id": value.get("client_id"),
        "fleet": value.get("fleet"),
        "mode": mode,
        "api_url": connection.get("api_url"),
        "local_port": local_port or None,
        "managed_tunnel": managed,
        "authenticated": authenticated,
        "reason": reason,
        "scopes": list(credential.get("scopes") or []),
        "expires_at": credential.get("expires_at"),
    }


def renew_login(
    profile: Optional[str] = None,
    *,
    expires_in: int = 30 * 24 * 60 * 60,
    remote_mac: str = "mac",
    connect_timeout: int = 10,
) -> Dict[str, Any]:
    """Renew the credential for the named (or active) login profile."""
    selected = _name(profile) if profile else active_profile_name()
    if not selected:
        raise ClientLoginError("no active login profile")
    with _session_lock(selected):
        current = load_profile(selected, include_token=True)
        _ensure_session_unlocked(selected)
        spec, _created = prepare_login_spec(
            _spec_from_profile(current), selected, connect_timeout=connect_timeout
        )
        client_id = str(current.get("client_id") or "")
        manifest = _run_remote_json(
            spec,
            [
                remote_mac,
                "--json",
                "admin",
                "client",
                "renew",
                client_id,
                "--expires-in",
                str(int(expires_in)),
                "--actor",
                "mac-login-renew",
            ],
            timeout=connect_timeout,
        )
        connection = dict(current.get("connection") or {})
        updated = _profile_manifest(
            manifest,
            spec,
            profile=selected,
            local_port=int(connection.get("local_port") or 0),
            remote_host=str(connection.get("remote_host") or "127.0.0.1"),
            remote_port=int(connection.get("remote_port") or 8789),
        )
        token = str((updated.get("credential") or {}).get("token") or "")
        valid, reason = _validate_token(
            str((updated.get("connection") or {}).get("api_url")),
            token,
            timeout=connect_timeout,
        )
        if not valid:
            try:
                _run_remote_action(
                    spec,
                    [
                        remote_mac,
                        "admin",
                        "client",
                        "revoke",
                        client_id,
                        "--actor",
                        "mac-login-renew-rollback",
                    ],
                    timeout=connect_timeout,
                )
            finally:
                raise ClientLoginError("renewed credential failed validation (%s)" % reason)
        install_enrollment_manifest(updated, profile_override=selected, activate=True)
        return {
            "status": "renewed",
            "profile": selected,
            "client_id": client_id,
            "scopes": list((updated.get("credential") or {}).get("scopes") or []),
            "expires_at": (updated.get("credential") or {}).get("expires_at"),
        }


def logout(
    profile: Optional[str] = None,
    *,
    revoke: bool = False,
    remote_mac: str = "mac",
    connect_timeout: int = 10,
) -> Dict[str, Any]:
    """Tear down the session for the named (or active) login profile."""
    selected = _name(profile) if profile else active_profile_name()
    if not selected:
        raise ClientLoginError("no active login profile")
    with _session_lock(selected):
        value = load_profile(selected, include_token=False)
        client_id = str(value.get("client_id") or "")
        if revoke:
            spec, _created = prepare_login_spec(
                _spec_from_profile(value), selected, connect_timeout=connect_timeout
            )
            _run_remote_action(
                spec,
                [remote_mac, "admin", "client", "revoke", client_id, "--actor", "mac-logout"],
                timeout=connect_timeout,
            )
        state = _read_state(selected)
        stopped = _stop_managed_state(state) if state else False
        remove_profile(selected)
        _remove_state(selected)
        managed_pin = managed_known_hosts_path(selected)
        known_hosts = str((value.get("ssh") or {}).get("known_hosts_file") or "")
        if known_hosts and Path(known_hosts).resolve() == managed_pin.resolve():
            managed_pin.unlink(missing_ok=True)
        return {
            "status": "logged_out",
            "profile": selected,
            "client_id": client_id,
            "revoked": revoke,
            "tunnel_stopped": stopped,
        }
