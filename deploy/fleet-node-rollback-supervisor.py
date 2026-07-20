#!/usr/bin/env python3
"""Fail-closed supervisor protocol for a fleet-node rollback.

The generated rollback shell script owns artifact restoration.  This helper
owns the two supervisor boundaries around that mutation:

* ``quiesce`` stops every MAC service identity and proves that the control
  plane port is closed before artifacts or service definitions are changed.
* ``restore`` starts the explicitly supplied prior control-plane, gateway, and
  agent topology, keeps every non-owner identity inactive, and proves both
  exact manager state and the control-plane HTTP health endpoint before issuing
  a receipt.

Only a successful proof is written to ``--receipt`` or stdout.  Manager output
is used for exact parsing in memory, but is never copied into durable evidence
or failure messages because it may contain paths or environment fragments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


SCHEMA = "mac.fleet_node_rollback_supervisor.v1"
MAX_MANAGER_OUTPUT = 64 * 1024
IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")
HEALTH_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$")
SAFE_MANAGER_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SAFE_ENV_NAMES = (
    "HOME",
    "USER",
    "LOGNAME",
    "PATH",
    "TMPDIR",
    "SHELL",
)
UNSAFE_ENV_PREFIXES = (
    "DBUS_",
    "LAUNCHD_",
    "SUPERVISOR_",
    "SYSTEMD_",
    "XDG_",
)


class ProtocolError(RuntimeError):
    """A proof could not be completed exactly inside its deadline."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        if self.stdout and self.stderr:
            return self.stdout + "\n" + self.stderr
        return self.stdout or self.stderr


@dataclass(frozen=True)
class ServiceNames:
    control_plane: str
    hermes_gateway: str
    openclaw_gateway: str
    nemoclaw_gateway: str
    agent: str

    def items(self) -> Tuple[Tuple[str, str], ...]:
        return (
            ("control_plane", self.control_plane),
            ("hermes_gateway", self.hermes_gateway),
            ("openclaw_gateway", self.openclaw_gateway),
            ("nemoclaw_gateway", self.nemoclaw_gateway),
            ("agent", self.agent),
        )


@dataclass(frozen=True)
class SystemdState:
    present: bool
    load: str
    active: str
    sub: str
    pid: int
    restarts: int

    @property
    def inactive(self) -> bool:
        return (not self.present) or (
            self.active == "inactive"
            and self.sub in {"dead", "exited"}
            and self.pid == 0
        )

    @property
    def healthy(self) -> bool:
        return (
            self.present
            and self.load == "loaded"
            and self.active == "active"
            and self.sub == "running"
            and self.pid > 0
        )


@dataclass(frozen=True)
class SupervisordState:
    present: bool
    state: str
    pid: int

    @property
    def inactive(self) -> bool:
        return (not self.present) or self.state in {"STOPPED", "EXITED", "FATAL"}

    @property
    def healthy(self) -> bool:
        return self.present and self.state == "RUNNING" and self.pid > 0


@dataclass(frozen=True)
class LaunchdState:
    present: bool
    state: str
    pid: int

    @property
    def healthy(self) -> bool:
        return self.present and self.state == "running" and self.pid > 0


class Deadline:
    def __init__(self, seconds: float) -> None:
        if seconds <= 0:
            raise ProtocolError("total deadline must be positive")
        self.started = time.monotonic()
        self.ends = self.started + seconds

    def remaining(self) -> float:
        return max(0.0, self.ends - time.monotonic())

    def require(self, context: str) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise ProtocolError("deadline expired while " + context)
        return remaining

    def pause(self, seconds: float, context: str) -> None:
        remaining = self.require(context)
        time.sleep(min(seconds, remaining))

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class CommandRunner:
    """Bounded command execution with process-group cleanup and capped output."""

    def __init__(self, deadline: Deadline, command_timeout: float) -> None:
        if command_timeout <= 0:
            raise ProtocolError("per-command timeout must be positive")
        self.deadline = deadline
        self.command_timeout = command_timeout
        self.env = self._clean_environment()

    @staticmethod
    def _clean_environment() -> Dict[str, str]:
        clean: Dict[str, str] = {}
        for name in SAFE_ENV_NAMES:
            value = os.environ.get(name)
            if value and not any(
                name.startswith(prefix) for prefix in UNSAFE_ENV_PREFIXES
            ):
                clean[name] = value
        # Never resolve a privileged manager through a caller-controlled PATH.
        # Non-standard installations remain supported via an explicit absolute
        # --systemctl/--supervisorctl/--launchctl/--sudo-bin argument.
        clean["PATH"] = SAFE_MANAGER_PATH
        clean["LANG"] = "C"
        clean["LC_ALL"] = "C"
        return clean

    @staticmethod
    def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise ProtocolError(
                "could not terminate timed out manager process group"
            ) from exc
        # Wait on the group, not merely the leader: a manager can fork a child
        # and then exit in response to TERM while leaving that child alive.
        grace_ends = time.monotonic() + 0.5
        while time.monotonic() < grace_ends:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                break
            except PermissionError as exc:
                raise ProtocolError(
                    "could not inspect timed out manager process group"
                ) from exc
            time.sleep(0.02)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise ProtocolError(
                "could not kill timed out manager process group"
            ) from exc
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _read_capped(handle: object) -> str:
        file_handle = handle
        file_handle.seek(0)  # type: ignore[attr-defined]
        data = file_handle.read(MAX_MANAGER_OUTPUT + 1)  # type: ignore[attr-defined]
        if len(data) > MAX_MANAGER_OUTPUT:
            raise ProtocolError("manager output exceeded the safe inspection limit")
        return data.decode("utf-8", errors="replace")

    def run(self, argv: Sequence[str], context: str) -> CommandResult:
        if not argv or any("\x00" in arg for arg in argv):
            raise ProtocolError("invalid manager command")
        remaining = self.deadline.require(context)
        timeout = min(self.command_timeout, remaining)
        with (
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            try:
                proc = subprocess.Popen(
                    list(argv),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=self.env,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ProtocolError(
                    "could not execute manager command for " + context
                ) from exc
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                self._terminate_group(proc)
                raise ProtocolError(
                    "manager command timed out while " + context
                ) from exc
            stdout = self._read_capped(stdout_file)
            stderr = self._read_capped(stderr_file)
        return CommandResult(proc.returncode, stdout, stderr)


def resolve_executable(value: str, clean_path: str) -> str:
    if os.path.isabs(value):
        path = value
    else:
        path = shutil.which(value, path=clean_path) or ""
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise ProtocolError("required manager executable is unavailable")
    return os.path.realpath(path)


def validate_identity(value: str, label: str) -> str:
    if not value or not IDENTITY_RE.fullmatch(value):
        raise ProtocolError("invalid " + label + " service identity")
    return value


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write_receipt(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


class BaseSupervisor:
    def __init__(
        self,
        names: ServiceNames,
        runner: CommandRunner,
        deadline: Deadline,
        poll_seconds: float,
        stable_observations: int,
        active_gateway: str,
        agent_active: bool,
    ) -> None:
        self.names = names
        self.runner = runner
        self.deadline = deadline
        self.poll_seconds = poll_seconds
        self.stable_observations = stable_observations
        self.active_gateway = active_gateway
        self.agent_active = agent_active

    def desired_service_items(
        self, *, control_plane_active: bool
    ) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
        active_logicals = set()
        if control_plane_active:
            active_logicals.add("control_plane")
        if self.active_gateway != "none":
            active_logicals.add(self.active_gateway + "_gateway")
        if self.agent_active:
            active_logicals.add("agent")
        active: List[Tuple[str, str]] = []
        inactive: List[Tuple[str, str]] = []
        for logical, identity in self.names.items():
            (active if logical in active_logicals else inactive).append(
                (logical, identity)
            )
        return active, inactive

    def _wait(self, probe: object, predicate: object, context: str) -> object:
        while True:
            value = probe()  # type: ignore[operator]
            if predicate(value):  # type: ignore[operator]
                return value
            self.deadline.pause(self.poll_seconds, context)

    def quiesce(self) -> Dict[str, object]:
        raise NotImplementedError

    def restore(self) -> Dict[str, object]:
        raise NotImplementedError


class SystemdSupervisor(BaseSupervisor):
    REQUIRED_PROPERTIES = {
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "NRestarts",
    }

    def __init__(
        self,
        *args: object,
        systemctl: str,
        privileged: Sequence[str],
        control_plane_active: bool,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.command = list(privileged) + [systemctl]
        self.control_plane_active = control_plane_active

    def _run(self, args: Sequence[str], context: str) -> CommandResult:
        return self.runner.run(self.command + list(args), context)

    def inspect(self, identity: str) -> SystemdState:
        result = self._run(
            [
                "show",
                identity,
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                "--property=NRestarts",
            ],
            "inspecting systemd service",
        )
        values: Dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in self.REQUIRED_PROPERTIES:
                if key in values:
                    raise ProtocolError("systemd returned ambiguous service properties")
                values[key] = value.strip()
        if set(values) != self.REQUIRED_PROPERTIES:
            raise ProtocolError("systemd did not return a complete service state")
        try:
            pid = int(values["MainPID"])
            restarts = int(values["NRestarts"])
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                "systemd returned a malformed numeric service property"
            ) from exc
        if pid < 0 or restarts < 0:
            raise ProtocolError("systemd returned an invalid numeric service property")
        load = values["LoadState"]
        if load == "not-found":
            if values["ActiveState"] != "inactive" or pid != 0:
                raise ProtocolError("systemd returned contradictory not-found state")
            return SystemdState(
                False,
                load,
                values["ActiveState"],
                values["SubState"],
                pid,
                restarts,
            )
        if result.returncode != 0:
            raise ProtocolError("systemd service inspection failed")
        if load not in {"loaded", "masked", "error"}:
            raise ProtocolError("systemd returned an unrecognized load state")
        return SystemdState(
            True, load, values["ActiveState"], values["SubState"], pid, restarts
        )

    def _stop_and_prove(self, identity: str) -> SystemdState:
        initial = self.inspect(identity)
        if not initial.inactive:
            self._run(["stop", identity], "stopping systemd service")
        return self._wait(
            lambda: self.inspect(identity),
            lambda state: state.inactive,
            "waiting for systemd service quiescence",
        )  # type: ignore[return-value]

    def quiesce(self) -> Dict[str, object]:
        observations: Dict[str, object] = {}
        for logical, identity in reversed(self.names.items()):
            state = self._stop_and_prove(identity)
            observations[logical] = {
                "identity": identity,
                "expected": "inactive",
                "observed": "absent" if not state.present else "inactive",
            }
        return observations

    def _enabled_state(self, identity: str) -> str:
        result = self._run(["is-enabled", identity], "inspecting systemd enablement")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            raise ProtocolError("systemd returned ambiguous enablement state")
        value = lines[0]
        if value not in {"enabled", "disabled", "masked", "not-found", "static"}:
            raise ProtocolError("systemd returned an unrecognized enablement state")
        if value == "enabled" and result.returncode != 0:
            raise ProtocolError("systemd contradicted enabled state")
        return value

    def _require_ok(self, args: Sequence[str], context: str) -> None:
        if self._run(args, context).returncode != 0:
            raise ProtocolError(context + " failed")

    def restore(self) -> Dict[str, object]:
        self._require_ok(["daemon-reload"], "reloading systemd definitions")
        active, inactive = self.desired_service_items(
            control_plane_active=self.control_plane_active
        )
        for _logical, identity in inactive:
            result = self._run(
                ["disable", "--now", identity],
                "disabling inactive restored service",
            )
            state = self._wait(
                lambda identity=identity: self.inspect(identity),
                lambda current: current.inactive,
                "waiting for inactive restored service shutdown",
            )
            enabled = self._enabled_state(identity)
            if enabled not in {"disabled", "masked", "not-found", "static"}:
                raise ProtocolError("inactive restored service remained enabled")
            if (
                result.returncode != 0
                and state.present
                and enabled not in {"masked", "static"}
            ):
                raise ProtocolError("could not disable inactive restored service")
        for _logical, identity in active:
            self._require_ok(["enable", identity], "enabling restored systemd service")
            self._require_ok(
                ["restart", identity], "restarting restored systemd service"
            )

        samples: List[Dict[str, SystemdState]] = []
        for index in range(self.stable_observations):
            sample: Dict[str, SystemdState] = {}
            for logical, identity in active:
                state = self._wait(
                    lambda identity=identity: self.inspect(identity),
                    lambda current: current.healthy,
                    "waiting for restored systemd service",
                )
                if self._enabled_state(identity) != "enabled":
                    raise ProtocolError("restored systemd service is not enabled")
                sample[logical] = state  # type: ignore[assignment]
            for logical, identity in inactive:
                state = self.inspect(identity)
                if not state.inactive:
                    raise ProtocolError(
                        "successor gateway became active during restore"
                    )
                sample[logical] = state
            samples.append(sample)
            if index + 1 < self.stable_observations:
                self.deadline.pause(
                    self.poll_seconds, "sampling restored systemd topology"
                )
        first = samples[0]
        for sample in samples[1:]:
            for logical, _identity in active:
                if (sample[logical].pid, sample[logical].restarts) != (
                    first[logical].pid,
                    first[logical].restarts,
                ):
                    raise ProtocolError("restored systemd service was unstable")
        active_names = {logical for logical, _identity in active}
        return {
            logical: {
                "identity": identity,
                "expected": ("running" if logical in active_names else "inactive"),
                "observed": ("running" if logical in active_names else "inactive"),
                "stable_observations": self.stable_observations,
            }
            for logical, identity in self.names.items()
        }


class SupervisordSupervisor(BaseSupervisor):
    KNOWN_STATES = {
        "BACKOFF",
        "EXITED",
        "FATAL",
        "RUNNING",
        "STARTING",
        "STOPPED",
        "STOPPING",
        "UNKNOWN",
    }

    def __init__(
        self,
        *args: object,
        supervisorctl: str,
        privileged: Sequence[str],
        control_plane_active: bool,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.command = list(privileged) + [supervisorctl]
        self.control_plane_active = control_plane_active

    def _run(self, args: Sequence[str], context: str) -> CommandResult:
        return self.runner.run(self.command + list(args), context)

    def inspect(self, identity: str) -> SupervisordState:
        result = self._run(["status", identity], "inspecting supervisord program")
        lines = [line.strip() for line in result.combined.splitlines() if line.strip()]
        absent = identity + ": ERROR (no such process)"
        if result.returncode != 0 and lines == [absent]:
            return SupervisordState(False, "ABSENT", 0)
        if result.returncode != 0 or len(lines) != 1:
            raise ProtocolError("supervisord returned ambiguous program state")
        fields = lines[0].split(None, 2)
        if (
            len(fields) < 2
            or fields[0] != identity
            or fields[1] not in self.KNOWN_STATES
        ):
            raise ProtocolError("supervisord returned malformed program state")
        state = fields[1]
        pid = 0
        if state == "RUNNING":
            match = re.search(r"(?:^|\s)pid\s+([0-9]+)(?:,|\s|$)", lines[0])
            if not match or int(match.group(1)) <= 0:
                raise ProtocolError("supervisord RUNNING state lacked a valid pid")
            pid = int(match.group(1))
        return SupervisordState(True, state, pid)

    def _stop_and_prove(self, identity: str) -> SupervisordState:
        initial = self.inspect(identity)
        if not initial.inactive:
            self._run(["stop", identity], "stopping supervisord program")
        return self._wait(
            lambda: self.inspect(identity),
            lambda state: state.inactive,
            "waiting for supervisord program quiescence",
        )  # type: ignore[return-value]

    def quiesce(self) -> Dict[str, object]:
        observations: Dict[str, object] = {}
        for logical, identity in reversed(self.names.items()):
            state = self._stop_and_prove(identity)
            observations[logical] = {
                "identity": identity,
                "expected": "inactive",
                "observed": "absent" if not state.present else "inactive",
            }
        return observations

    def _require_ok(self, args: Sequence[str], context: str) -> None:
        if self._run(args, context).returncode != 0:
            raise ProtocolError(context + " failed")

    def restore(self) -> Dict[str, object]:
        self._require_ok(["reread"], "rereading supervisord definitions")
        self._require_ok(["update"], "updating supervisord definitions")
        active, inactive = self.desired_service_items(
            control_plane_active=self.control_plane_active
        )
        for _logical, identity in inactive:
            self._stop_and_prove(identity)
        for _logical, identity in active:
            state = self.inspect(identity)
            if not state.inactive:
                self._run(
                    ["stop", identity],
                    "stopping restored supervisord program before start",
                )
                self._wait(
                    lambda identity=identity: self.inspect(identity),
                    lambda current: current.inactive,
                    "waiting to restart supervisord program",
                )
            self._require_ok(
                ["start", identity], "starting restored supervisord program"
            )

        samples: List[Dict[str, SupervisordState]] = []
        for index in range(self.stable_observations):
            sample: Dict[str, SupervisordState] = {}
            for logical, identity in active:
                state = self._wait(
                    lambda identity=identity: self.inspect(identity),
                    lambda current: current.healthy,
                    "waiting for restored supervisord program",
                )
                sample[logical] = state  # type: ignore[assignment]
            for logical, identity in inactive:
                state = self.inspect(identity)
                if not state.inactive:
                    raise ProtocolError(
                        "successor gateway became active during restore"
                    )
                sample[logical] = state
            samples.append(sample)
            if index + 1 < self.stable_observations:
                self.deadline.pause(
                    self.poll_seconds, "sampling restored supervisord topology"
                )
        first = samples[0]
        for sample in samples[1:]:
            for logical, _identity in active:
                if sample[logical].pid != first[logical].pid:
                    raise ProtocolError("restored supervisord program was unstable")
        active_names = {logical for logical, _identity in active}
        return {
            logical: {
                "identity": identity,
                "expected": ("running" if logical in active_names else "inactive"),
                "observed": ("running" if logical in active_names else "inactive"),
                "stable_observations": self.stable_observations,
            }
            for logical, identity in self.names.items()
        }


class LaunchdSupervisor(BaseSupervisor):
    # launchd's printed transition vocabulary is not a stable public API.  The
    # safety boundary is structural instead: quiesce removes every present job
    # and proves it absent, while restore accepts only ``running`` with a
    # positive PID as healthy.  Require bounded printable text so malformed
    # manager output still fails closed without pretending to enumerate every
    # transient value (some macOS releases include punctuation in those values).
    @staticmethod
    def _valid_state(value: str) -> bool:
        return 1 <= len(value) <= 256 and all(32 <= ord(char) <= 126 for char in value)

    def __init__(
        self,
        *args: object,
        launchctl: str,
        privileged: Sequence[str],
        uid: int,
        system_supervisor: Optional[str],
        control_domain: Optional[str],
        system_supervisor_active: bool,
        plists: Mapping[str, Optional[Path]],
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.launchctl = launchctl
        self.privileged = list(privileged)
        self.uid = uid
        self.system_supervisor = system_supervisor
        self.control_domain = control_domain
        self.system_supervisor_active = system_supervisor_active
        self.plists = plists

    @property
    def gui_domain(self) -> str:
        return "gui/" + str(self.uid)

    def _command(self, domain: str) -> List[str]:
        prefix = self.privileged if domain == "system" else []
        return list(prefix) + [self.launchctl]

    def _run(self, domain: str, args: Sequence[str], context: str) -> CommandResult:
        return self.runner.run(self._command(domain) + list(args), context)

    @staticmethod
    def _top_level_properties(text: str) -> Dict[str, str]:
        properties: Dict[str, str] = {}
        depth = 0
        saw_root = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if depth == 1 and "=" in line and not line.endswith("{"):
                key, value = line.split("=", 1)
                key = key.strip()
                if key in {"state", "pid"}:
                    if key in properties:
                        raise ProtocolError("launchd returned ambiguous job properties")
                    properties[key] = value.strip()
            opens = raw_line.count("{")
            closes = raw_line.count("}")
            if opens:
                saw_root = True
            depth += opens - closes
            if depth < 0:
                raise ProtocolError("launchd returned malformed job structure")
        if not saw_root or depth != 0:
            raise ProtocolError("launchd returned malformed job structure")
        return properties

    def inspect(self, domain: str, identity: str) -> LaunchdState:
        target = domain + "/" + identity
        result = self._run(domain, ["print", target], "inspecting launchd job")
        if result.returncode == 113:
            lines = [
                line.strip() for line in result.combined.splitlines() if line.strip()
            ]
            legacy_absent = len(lines) == 1 and "Could not find service" in lines[0]
            current_macos_absent = (
                len(lines) == 2
                and lines[0] == "Bad request."
                and re.fullmatch(
                    r'Could not find service "[^"\r\n]+" in domain for '
                    r'(?:system|user gui: [0-9]+)',
                    lines[1],
                )
                is not None
            )
            if legacy_absent or current_macos_absent:
                return LaunchdState(False, "absent", 0)
            raise ProtocolError("launchd returned ambiguous absent state")
        if result.returncode != 0:
            raise ProtocolError("launchd job inspection failed")
        props = self._top_level_properties(result.stdout)
        state = props.get("state", "")
        if not self._valid_state(state):
            raise ProtocolError("launchd returned a malformed job state")
        pid = 0
        if "pid" in props:
            try:
                pid = int(props["pid"])
            except ValueError as exc:
                raise ProtocolError("launchd returned a malformed pid") from exc
            if pid < 0:
                raise ProtocolError("launchd returned an invalid pid")
        if state == "running" and pid <= 0:
            raise ProtocolError("launchd running state lacked a valid pid")
        return LaunchdState(True, state, pid)

    def _stop_and_prove(self, domain: str, identity: str) -> LaunchdState:
        initial = self.inspect(domain, identity)
        if initial.present:
            self._run(
                domain, ["bootout", domain + "/" + identity], "stopping launchd job"
            )
        return self._wait(
            lambda: self.inspect(domain, identity),
            lambda state: not state.present,
            "waiting for launchd job removal",
        )  # type: ignore[return-value]

    def _all_targets(self) -> List[Tuple[str, str, str]]:
        targets: List[Tuple[str, str, str]] = []
        for logical, identity in self.names.items():
            targets.append((logical, "system", identity))
            targets.append((logical, self.gui_domain, identity))
        if self.system_supervisor:
            targets.append(("system_supervisor", "system", self.system_supervisor))
        return targets

    def quiesce(self) -> Dict[str, object]:
        observations: Dict[str, object] = {}
        targets = self._all_targets()
        if self.system_supervisor:
            targets.sort(key=lambda target: target[0] != "system_supervisor")
        for logical, domain, identity in targets:
            self._stop_and_prove(domain, identity)
            key = logical + "@" + ("gui" if domain.startswith("gui/") else domain)
            observations[key] = {
                "identity": identity,
                "domain": domain,
                "expected": "absent",
                "observed": "absent",
            }
        return observations

    def _require_ok(self, domain: str, args: Sequence[str], context: str) -> None:
        if self._run(domain, args, context).returncode != 0:
            raise ProtocolError(context + " failed")

    def _start(self, domain: str, identity: str, plist_key: str) -> None:
        plist = self.plists.get(plist_key)
        if plist is None or not plist.is_absolute() or not plist.is_file():
            raise ProtocolError("restored launchd definition is unavailable")
        self._require_ok(
            domain,
            ["enable", domain + "/" + identity],
            "enabling restored launchd job",
        )
        self._require_ok(
            domain,
            ["bootstrap", domain, str(plist)],
            "bootstrapping restored launchd job",
        )
        self._require_ok(
            domain,
            ["kickstart", "-k", domain + "/" + identity],
            "starting restored launchd job",
        )

    def _expected_targets(self) -> Dict[Tuple[str, str], bool]:
        expected: Dict[Tuple[str, str], bool] = {}
        for _logical, identity in self.names.items():
            expected[("system", identity)] = False
            expected[(self.gui_domain, identity)] = False
        if self.control_domain:
            expected[(self.control_domain, self.names.control_plane)] = True
        if self.active_gateway != "none":
            gateway_identity = {
                "hermes": self.names.hermes_gateway,
                "openclaw": self.names.openclaw_gateway,
                "nemoclaw": self.names.nemoclaw_gateway,
            }[self.active_gateway]
            expected[(self.gui_domain, gateway_identity)] = True
        if self.agent_active:
            expected[(self.gui_domain, self.names.agent)] = True
        if self.system_supervisor:
            expected[("system", self.system_supervisor)] = self.system_supervisor_active
        return expected

    def restore(self) -> Dict[str, object]:
        if self.control_domain not in {None, "system", self.gui_domain}:
            raise ProtocolError("launchd restore control-plane domain is not explicit")
        expected = self._expected_targets()
        # Reassert the negative half of the topology immediately before start.
        for (domain, identity), active in expected.items():
            if not active:
                self._stop_and_prove(domain, identity)

        if self.control_domain is not None:
            control_key = (
                "control_system" if self.control_domain == "system" else "control_gui"
            )
            self._start(self.control_domain, self.names.control_plane, control_key)
        if self.system_supervisor_active:
            if not self.system_supervisor:
                raise ProtocolError(
                    "active launchd system supervisor lacks an identity"
                )
            self._start("system", self.system_supervisor, "system_supervisor")
        if self.active_gateway != "none":
            gateway_identity = {
                "hermes": self.names.hermes_gateway,
                "openclaw": self.names.openclaw_gateway,
                "nemoclaw": self.names.nemoclaw_gateway,
            }[self.active_gateway]
            self._start(self.gui_domain, gateway_identity, self.active_gateway)
        if self.agent_active:
            self._start(self.gui_domain, self.names.agent, "agent")

        samples: List[Dict[Tuple[str, str], LaunchdState]] = []
        for index in range(self.stable_observations):
            sample: Dict[Tuple[str, str], LaunchdState] = {}
            for target, active in expected.items():
                domain, identity = target
                state = self._wait(
                    lambda domain=domain, identity=identity: self.inspect(
                        domain, identity
                    ),
                    (
                        (lambda current: current.healthy)
                        if active
                        else (lambda current: not current.present)
                    ),
                    "waiting for exact restored launchd topology",
                )
                sample[target] = state  # type: ignore[assignment]
            samples.append(sample)
            if index + 1 < self.stable_observations:
                self.deadline.pause(
                    self.poll_seconds, "sampling restored launchd topology"
                )
        first = samples[0]
        for sample in samples[1:]:
            for target, active in expected.items():
                if active and sample[target].pid != first[target].pid:
                    raise ProtocolError("restored launchd job was unstable")

        observations: Dict[str, object] = {}
        for (domain, identity), active in expected.items():
            logical = next(
                (
                    name
                    for name, candidate in self.names.items()
                    if candidate == identity
                ),
                "system_supervisor",
            )
            key = logical + "@" + ("gui" if domain.startswith("gui/") else domain)
            observations[key] = {
                "identity": identity,
                "domain": domain,
                "expected": "running" if active else "absent",
                "observed": "running" if active else "absent",
                "stable_observations": self.stable_observations,
            }
        return observations


def wait_port_closed(port: int, deadline: Deadline, poll_seconds: float) -> None:
    while True:
        remaining = deadline.require("proving the control-plane port closed")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(min(0.5, remaining))
            result = sock.connect_ex(("127.0.0.1", port))
        finally:
            sock.close()
        if result != 0:
            return
        deadline.pause(poll_seconds, "waiting for the control-plane port to close")


def wait_http_healthy(
    port: int,
    path: str,
    deadline: Deadline,
    poll_seconds: float,
    stable_observations: int,
) -> None:
    successes = 0
    while successes < stable_observations:
        remaining = deadline.require("proving control-plane health")
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=min(2.0, remaining)
        )
        healthy = False
        try:
            connection.request(
                "GET", path, headers={"Host": "127.0.0.1", "Connection": "close"}
            )
            response = connection.getresponse()
            healthy = 200 <= response.status < 300
            response.read(1024)
        except (OSError, http.client.HTTPException):
            healthy = False
        finally:
            connection.close()
        if healthy:
            successes += 1
        else:
            successes = 0
        if successes < stable_observations:
            deadline.pause(poll_seconds, "waiting for stable control-plane health")


def privileged_prefix(args: argparse.Namespace, runner: CommandRunner) -> List[str]:
    use_sudo = args.sudo_mode == "always" or (
        args.sudo_mode == "auto" and hasattr(os, "geteuid") and os.geteuid() != 0
    )
    if not use_sudo:
        return []
    sudo = resolve_executable(args.sudo_bin, runner.env["PATH"])
    return [sudo, "-n"]


def build_supervisor(
    args: argparse.Namespace,
    names: ServiceNames,
    runner: CommandRunner,
    deadline: Deadline,
) -> BaseSupervisor:
    common = dict(
        names=names,
        runner=runner,
        deadline=deadline,
        poll_seconds=args.poll_seconds,
        stable_observations=args.stable_observations,
        active_gateway=args.active_gateway or "none",
        agent_active=args.agent_prior_state == "active",
    )
    prefix = privileged_prefix(args, runner)
    if args.supervisor == "systemd":
        systemctl = resolve_executable(args.systemctl, runner.env["PATH"])
        return SystemdSupervisor(
            systemctl=systemctl,
            privileged=prefix,
            control_plane_active=args.control_plane_mode == "active",
            **common,
        )
    if args.supervisor == "supervisord":
        supervisorctl = resolve_executable(args.supervisorctl, runner.env["PATH"])
        supervisor_prefix = prefix if args.supervisord_scope == "system" else []
        return SupervisordSupervisor(
            supervisorctl=supervisorctl,
            privileged=supervisor_prefix,
            control_plane_active=args.control_plane_mode == "active",
            **common,
        )
    launchctl = resolve_executable(args.launchctl, runner.env["PATH"])
    uid = args.launchd_uid
    if uid is None:
        uid = os.getuid()
    if uid < 0:
        raise ProtocolError("launchd uid must be non-negative")
    system_supervisor = args.launchd_system_supervisor
    if system_supervisor:
        system_supervisor = validate_identity(
            system_supervisor, "launchd system supervisor"
        )
    control_domain: Optional[str] = None
    if args.control_plane_mode != "inactive":
        control_domain = (
            "system" if args.control_plane_mode == "system" else "gui/" + str(uid)
        )
        if (
            args.launchd_system_supervisor_was_active
            and args.control_plane_mode != "system"
        ):
            raise ProtocolError(
                "launchd system supervisor requires system control-plane topology"
            )
    elif args.launchd_system_supervisor_was_active:
        raise ProtocolError(
            "inactive launchd control plane cannot have an active system supervisor"
        )
    plists = {
        "control_system": (
            Path(args.launchd_control_system_plist)
            if args.launchd_control_system_plist
            else None
        ),
        "control_gui": (
            Path(args.launchd_control_gui_plist)
            if args.launchd_control_gui_plist
            else None
        ),
        "system_supervisor": (
            Path(args.launchd_system_supervisor_plist)
            if args.launchd_system_supervisor_plist
            else None
        ),
        "hermes": Path(args.launchd_hermes_plist)
        if args.launchd_hermes_plist
        else None,
        "openclaw": (
            Path(args.launchd_openclaw_plist) if args.launchd_openclaw_plist else None
        ),
        "nemoclaw": (
            Path(args.launchd_nemoclaw_plist) if args.launchd_nemoclaw_plist else None
        ),
        "agent": Path(args.launchd_agent_plist) if args.launchd_agent_plist else None,
    }
    return LaunchdSupervisor(
        launchctl=launchctl,
        privileged=prefix,
        uid=uid,
        system_supervisor=system_supervisor,
        control_domain=control_domain,
        system_supervisor_active=args.launchd_system_supervisor_was_active,
        plists=plists,
        **common,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("quiesce", "restore"))
    parser.add_argument(
        "--supervisor",
        required=True,
        choices=("systemd", "supervisord", "launchd"),
    )
    parser.add_argument(
        "--control-plane-mode",
        required=True,
        choices=("active", "inactive", "system", "gui"),
        help=(
            "prior topology: active/inactive for systemd or supervisord; "
            "system/gui/inactive for launchd"
        ),
    )
    parser.add_argument("--control-plane", required=True)
    parser.add_argument("--hermes-gateway", required=True)
    parser.add_argument("--openclaw-gateway", required=True)
    parser.add_argument("--nemoclaw-gateway", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument(
        "--active-gateway",
        choices=("hermes", "openclaw", "nemoclaw", "none"),
        help="gateway owner in the prior generation; required for restore",
    )
    parser.add_argument(
        "--agent-prior-state",
        choices=("active", "inactive", "absent"),
        help="worker service state in the prior generation; required for restore",
    )
    parser.add_argument("--control-plane-port", required=True, type=int)
    parser.add_argument("--health-path", default="/health")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--deadline-seconds", type=float, default=90.0)
    parser.add_argument(
        "--compensation-deadline-seconds",
        type=float,
        help=(
            "fresh bounded budget for re-quiescing a failed restore; "
            "defaults to --deadline-seconds"
        ),
    )
    parser.add_argument("--command-timeout-seconds", type=float, default=8.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--stable-observations", type=int, default=2)
    parser.add_argument(
        "--sudo-mode", choices=("auto", "always", "never"), default="auto"
    )
    parser.add_argument("--sudo-bin", default="sudo")
    parser.add_argument("--systemctl", default="systemctl")
    parser.add_argument("--supervisorctl", default="supervisorctl")
    parser.add_argument(
        "--supervisord-scope", choices=("user", "system"), default="user"
    )
    parser.add_argument("--launchctl", default="launchctl")
    parser.add_argument("--launchd-uid", type=int)
    parser.add_argument("--launchd-system-supervisor")
    parser.add_argument("--launchd-system-supervisor-was-active", action="store_true")
    parser.add_argument("--launchd-control-system-plist")
    parser.add_argument("--launchd-control-gui-plist")
    parser.add_argument("--launchd-system-supervisor-plist")
    parser.add_argument("--launchd-hermes-plist")
    parser.add_argument("--launchd-openclaw-plist")
    parser.add_argument("--launchd-nemoclaw-plist")
    parser.add_argument("--launchd-agent-plist")
    return parser


def validate_args(args: argparse.Namespace) -> ServiceNames:
    if not 1 <= args.control_plane_port <= 65535:
        raise ProtocolError("control-plane port is out of range")
    if not HEALTH_PATH_RE.fullmatch(args.health_path):
        raise ProtocolError("health path must be a query-free absolute HTTP path")
    if args.poll_seconds <= 0:
        raise ProtocolError("poll interval must be positive")
    if (
        args.compensation_deadline_seconds is not None
        and args.compensation_deadline_seconds <= 0
    ):
        raise ProtocolError("compensation deadline must be positive")
    if args.stable_observations < 2:
        raise ProtocolError("at least two stable observations are required")
    if args.action == "restore":
        if args.active_gateway is None:
            raise ProtocolError("restore requires an explicit prior gateway owner")
        if args.agent_prior_state is None:
            raise ProtocolError("restore requires an explicit prior agent state")
    if args.supervisor in {"systemd", "supervisord"}:
        if args.control_plane_mode not in {"active", "inactive"}:
            raise ProtocolError(
                "systemd and supervisord require active or inactive control-plane mode"
            )
    elif args.control_plane_mode not in {"system", "gui", "inactive"}:
        raise ProtocolError(
            "launchd requires system, gui, or inactive control-plane mode"
        )
    receipt = Path(args.receipt)
    if not receipt.is_absolute():
        raise ProtocolError("receipt path must be absolute")
    try:
        receipt.unlink()
    except FileNotFoundError:
        pass
    names = ServiceNames(
        validate_identity(args.control_plane, "control-plane"),
        validate_identity(args.hermes_gateway, "Hermes gateway"),
        validate_identity(args.openclaw_gateway, "OpenClaw gateway"),
        validate_identity(args.nemoclaw_gateway, "NemoClaw gateway"),
        validate_identity(args.agent, "agent"),
    )
    identities = [identity for _logical, identity in names.items()]
    if len(set(identities)) != len(identities):
        raise ProtocolError("rollback service identities must be unique")
    return names


def requiesce_after_restore_failure(
    args: argparse.Namespace,
    names: ServiceNames,
) -> None:
    """Use a fresh bounded budget to return a failed restore to zero services.

    The primary restore deadline may have expired while waiting for a late
    service or exact-topology proof.  Reusing it would make compensation a
    no-op precisely when it is most important, so compensation gets one new
    action-sized deadline while retaining the same scrubbed manager contract.
    """

    deadline = Deadline(
        args.compensation_deadline_seconds or args.deadline_seconds
    )
    runner = CommandRunner(deadline, args.command_timeout_seconds)
    supervisor = build_supervisor(args, names, runner, deadline)
    supervisor.quiesce()
    wait_port_closed(args.control_plane_port, deadline, args.poll_seconds)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        names = validate_args(args)
        deadline = Deadline(args.deadline_seconds)
        runner = CommandRunner(deadline, args.command_timeout_seconds)
        supervisor = build_supervisor(args, names, runner, deadline)
        if args.action == "quiesce":
            services = supervisor.quiesce()
            wait_port_closed(args.control_plane_port, deadline, args.poll_seconds)
            health = "closed"
        else:
            try:
                services = supervisor.restore()
                if args.control_plane_mode == "inactive":
                    wait_port_closed(
                        args.control_plane_port, deadline, args.poll_seconds
                    )
                    health = "closed"
                else:
                    wait_http_healthy(
                        args.control_plane_port,
                        args.health_path,
                        deadline,
                        args.poll_seconds,
                        args.stable_observations,
                    )
                    health = "healthy"
            except ProtocolError as restore_error:
                try:
                    requiesce_after_restore_failure(args, names)
                except ProtocolError as compensation_error:
                    raise ProtocolError(
                        "restore failed ("
                        + str(restore_error)
                        + "); compensation failed ("
                        + str(compensation_error)
                        + ")"
                    ) from compensation_error
                raise ProtocolError(
                    "restore failed ("
                    + str(restore_error)
                    + "); compensation re-quiesced every exact service identity"
                ) from restore_error
        payload: Dict[str, object] = {
            "schema": SCHEMA,
            "status": "passed",
            "action": args.action,
            "supervisor": args.supervisor,
            "observed_at": utc_now(),
            "duration_ms": deadline.elapsed_ms(),
            "control_plane": {
                "host": "127.0.0.1",
                "port": args.control_plane_port,
                "health_path": args.health_path,
                "mode": args.control_plane_mode,
                "observed": health,
                "stable_observations": args.stable_observations,
            },
            "prior_topology": (
                {
                    "active_gateway": args.active_gateway,
                    "agent_state": args.agent_prior_state,
                }
                if args.action == "restore"
                else None
            ),
            "services": services,
        }
        receipt = Path(args.receipt)
        atomic_write_receipt(receipt, payload)
        sys.stdout.write(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return 0
    except ProtocolError as exc:
        # Failure text is intentionally classification-only; never echo manager
        # output, command lines, or service-definition paths.
        sys.stderr.write("rollback supervisor proof failed: " + str(exc) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
