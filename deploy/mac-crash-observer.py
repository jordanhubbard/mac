#!/usr/bin/env python3
"""Supervisor-side MAC crash observer.

This program is intentionally standard-library-only and installed outside the
MAC virtualenv. It survives a broken MAC import graph, runs the worker as its
child, enables fatal Python tracebacks/core production, forwards termination
signals, and durably spools a normalized report before returning the child's
failure status to systemd, launchd, or supervisord.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import platform
import re
import resource
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, Optional

SCHEMA = "mac.agent_crash_occurrence.v1"
MAX_STDERR_BYTES = 64 * 1024
MAX_CORE_INFO_BYTES = 32 * 1024
POST_TIMEOUT_SECONDS = 15
MAX_SPOOL_FILES = 512


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _stable_agent_id(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.lower()).strip("_") or "default"
    return "agent_%s" % safe


def _load_env_file(path: Path, environ: Dict[str, str]) -> None:
    """Load simple shell assignment lines without executing the file."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        try:
            parts = shlex.split(value, posix=True)
            parsed = parts[0] if len(parts) == 1 else value
        except ValueError:
            continue
        environ.setdefault(name, parsed)


def _git_value(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _resource_snapshot() -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "schema": "mac.crash_resource_snapshot.v1",
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
    }
    try:
        usage = shutil.disk_usage(Path.home())
        snapshot["disk"] = {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        }
    except OSError:
        pass
    try:
        snapshot["observer_rusage"] = {
            "max_rss": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        }
    except (ValueError, OSError):
        pass
    return snapshot


def _enable_core_dumps() -> Dict[str, Any]:
    detail: Dict[str, Any] = {"enabled": False}
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        target = hard if hard != resource.RLIM_INFINITY else resource.RLIM_INFINITY
        resource.setrlimit(resource.RLIMIT_CORE, (target, hard))
        new_soft, new_hard = resource.getrlimit(resource.RLIMIT_CORE)
        detail.update({"enabled": new_soft != 0, "soft": new_soft, "hard": new_hard})
    except (ValueError, OSError) as exc:
        detail["error"] = "%s: %s" % (type(exc).__name__, exc)
    return detail


def _core_evidence(pid: int, cwd: Path) -> tuple[str, Dict[str, Any], str]:
    candidates = [
        cwd / "core",
        cwd / ("core.%d" % pid),
        Path("/cores") / ("core.%d" % pid),
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return (
                    str(candidate),
                    {"size_bytes": candidate.stat().st_size, "provider": "filesystem"},
                    "",
                )
        except OSError:
            pass
    if shutil.which("coredumpctl"):
        try:
            completed = subprocess.run(
                ["coredumpctl", "--no-pager", "info", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
                check=False,
            )
            output = completed.stdout[-MAX_CORE_INFO_BYTES:].decode("utf-8", "replace")
            if completed.returncode == 0 and output.strip():
                return (
                    "systemd-coredump:%d" % pid,
                    {"provider": "systemd-coredump", "info": output},
                    output,
                )
        except (OSError, subprocess.TimeoutExpired):
            pass
    pattern = ""
    try:
        pattern = Path("/proc/sys/kernel/core_pattern").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return "", {"provider": "none", "core_pattern": pattern}, ""


def _retain_core(
    reference: str, metadata: Dict[str, Any], mac_home: Path, event_id: str, env: Dict[str, str]
) -> tuple[str, Dict[str, Any]]:
    """Hard-link/copy a filesystem core into bounded MAC-owned retention."""
    source = (
        Path(reference) if reference and not reference.startswith("systemd-coredump:") else None
    )
    if source is None:
        return reference, metadata
    try:
        size = source.stat().st_size
    except OSError:
        return reference, metadata
    try:
        max_bytes = max(0, int(env.get("MAC_CRASH_CORE_MAX_BYTES", str(512 * 1024 * 1024))))
        retain_count = int(env.get("MAC_CRASH_CORE_RETAIN_COUNT", "3"))
        retain_count = max(1, retain_count)
    except ValueError:
        max_bytes, retain_count = 512 * 1024 * 1024, 3
    if not max_bytes or size > max_bytes:
        return reference, {**metadata, "retained": False, "retention_reason": "size_limit"}
    root = mac_home / "crashes"
    destination_dir = root / event_id
    destination = destination_dir / "core"
    try:
        destination_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.link(source, destination)
            method = "hardlink"
        except OSError:
            shutil.copy2(source, destination)
            method = "copy"
        os.chmod(destination, 0o600)
        directories = sorted(
            (item for item in root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for stale in directories[retain_count:]:
            shutil.rmtree(stale, ignore_errors=True)
        return str(destination), {
            **metadata,
            "retained": True,
            "retention_method": method,
            "original_reference": reference,
        }
    except OSError as exc:
        return reference, {
            **metadata,
            "retained": False,
            "retention_error": "%s: %s" % (type(exc).__name__, exc),
        }


def _agent_runtime_state(base_url: str, token: str, agent_id: str) -> Dict[str, Any]:
    if not base_url or not token:
        return {}
    request = urllib.request.Request(
        "%s/agents/%s" % (base_url.rstrip("/"), agent_id),
        headers={"Authorization": "Bearer %s" % token},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            value = json.loads(response.read(1_048_576).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def _post_report(base_url: str, token: str, agent_id: str, payload: Dict[str, Any]) -> bool:
    if not base_url or not token:
        return False
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        "%s/agents/%s/crash-reports" % (base_url.rstrip("/"), agent_id),
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SECONDS) as response:
            response.read(1_048_576)
        return True
    except (OSError, urllib.error.URLError):
        return False


def _spool_payload(spool_dir: Path, payload: Dict[str, Any]) -> Path:
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = spool_dir / (str(payload["event_id"]) + ".json")
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    entries = sorted(
        spool_dir.glob("*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in entries[MAX_SPOOL_FILES:]:
        stale.unlink(missing_ok=True)
    return destination


def _flush_spool(spool_dir: Path, base_url: str, token: str, agent_id: str) -> int:
    sent = 0
    if not spool_dir.is_dir():
        return sent
    for path in sorted(spool_dir.glob("*.json"))[:100]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and _post_report(base_url, token, agent_id, payload):
            path.unlink(missing_ok=True)
            sent += 1
    return sent


class _StderrTee(threading.Thread):
    def __init__(self, stream: Any, limit: int = MAX_STDERR_BYTES) -> None:
        super().__init__(name="mac-crash-stderr-tee", daemon=True)
        self.stream = stream
        self.limit = limit
        self.chunks: Deque[bytes] = collections.deque()
        self.size = 0

    def run(self) -> None:
        while True:
            chunk = self.stream.read(4096)
            if not chunk:
                break
            try:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
            except (AttributeError, OSError):
                pass
            self.chunks.append(chunk)
            self.size += len(chunk)
            while self.size > self.limit and self.chunks:
                self.size -= len(self.chunks.popleft())

    def text(self) -> str:
        return b"".join(self.chunks)[-self.limit :].decode("utf-8", "replace")


def _stack_from_stderr(stderr: str, core_info: str) -> str:
    lines = stderr.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if "Traceback (most recent call last)" in line
        or "Fatal Python error:" in line
        or re.match(r"^\s*#\d+\s", line)
    ]
    selected = lines[starts[-1] :] if starts else lines[-120:]
    text = "\n".join(selected)
    if core_info:
        text = (text + "\n" + core_info).strip()
    return text[-MAX_STDERR_BYTES:]


def _reason(returncode: int) -> tuple[Optional[int], Optional[int], str]:
    if returncode < 0:
        sig = -returncode
        try:
            label = signal.Signals(sig).name
        except ValueError:
            label = str(sig)
        return None, sig, "process terminated by signal %s" % label
    return returncode, None, "process exited unexpectedly with status %d" % returncode


def observe(supervisor: str, command: Iterable[str]) -> int:
    argv = list(command)
    if not argv:
        raise SystemExit("observer requires a child command after --")
    env = dict(os.environ)
    mac_home = Path(env.get("MAC_HOME") or (Path.home() / ".mac")).expanduser()
    _load_env_file(Path.home() / ".mac" / "mac.env", env)
    env["PYTHONFAULTHANDLER"] = "1"
    env.setdefault("PYTHONUNBUFFERED", "1")
    agent_name = env.get("MAC_WORKER_AGENT_NAME") or platform.node().split(".")[0]
    agent_id = (
        env.get("MAC_WORKER_AGENT_ID") or env.get("MAC_AGENT_ID") or _stable_agent_id(agent_name)
    )
    base_url = env.get("MAC_HUB_URL") or env.get("MAC_URL", "")
    token = env.get("MAC_WORKER_TOKEN", "")
    spool_dir = Path(env.get("MAC_CRASH_SPOOL_DIR") or (mac_home / "crash-spool"))
    _flush_spool(spool_dir, base_url, token, agent_id)
    core_setup = _enable_core_dumps()
    intentional_stop = {"value": False}
    child: Optional[subprocess.Popen[bytes]] = None

    def forward(signum: int, _frame: Any) -> None:
        intentional_stop["value"] = True
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signum)
            except (AttributeError, ProcessLookupError, OSError):
                try:
                    child.send_signal(signum)
                except OSError:
                    pass

    prior = {}
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        prior[signum] = signal.signal(signum, forward)
    try:
        # The service wrapper performs a startup self-test before exec'ing the
        # long-running worker. Give that whole transient tree its own process
        # group so a supervisor stop reaches both the wrapper and whichever
        # preflight child it is currently waiting for. Signalling only the
        # wrapper can be consumed by its shell trap while the self-test keeps
        # running, after which the wrapper would otherwise exec a fresh worker
        # that never saw the one-shot stop request.
        child = subprocess.Popen(
            argv,
            env=env,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert child.stderr is not None
        tee = _StderrTee(child.stderr)
        tee.start()
        returncode = child.wait()
        tee.join(timeout=5)
    finally:
        for signum, handler in prior.items():
            signal.signal(signum, handler)

    if returncode == 0 or intentional_stop["value"]:
        if returncode >= 0:
            return returncode
        return 128 + abs(returncode)

    exit_code, signal_number, reason = _reason(returncode)
    repo = Path(env.get("MAC_SELF_UPDATE_REPO") or (mac_home / "src" / "mac")).expanduser()
    revision = _git_value(repo, "rev-parse", "HEAD") or env.get("MAC_DEPLOY_REV", "unknown")
    tree_sha = _git_value(repo, "rev-parse", "HEAD^{tree}")
    runtime = _agent_runtime_state(base_url, token, agent_id)
    event_id = "crashevt_%s" % uuid.uuid4().hex
    core_reference, core_metadata, core_info = _core_evidence(child.pid, Path.cwd())
    core_reference, core_metadata = _retain_core(
        core_reference, core_metadata, mac_home, event_id, env
    )
    stderr_tail = tee.text()
    payload: Dict[str, Any] = {
        "schema": SCHEMA,
        "event_id": event_id,
        "observed_at": _utcnow(),
        "supervisor": supervisor,
        "process_name": Path(argv[0]).name,
        "pid": child.pid,
        "exit_code": exit_code,
        "signal": signal_number,
        "reason": reason,
        "revision": revision,
        "tree_sha": tree_sha,
        "task_id": runtime.get("current_task_id"),
        "lease_id": runtime.get("lease_id"),
        "stack_trace": _stack_from_stderr(stderr_tail, core_info),
        "stderr_tail": stderr_tail,
        "core_reference": core_reference,
        "core_metadata": {**core_setup, **core_metadata},
        "resource_snapshot": _resource_snapshot(),
        "metadata": {
            "observer_pid": os.getpid(),
            "agent_name": agent_name,
            "argv0": argv[0],
        },
    }
    path = _spool_payload(spool_dir, payload)
    if _post_report(base_url, token, agent_id, payload):
        path.unlink(missing_ok=True)
    else:
        print("MAC crash report spooled at %s" % path, file=sys.stderr)
    return 128 + signal_number if signal_number is not None else int(exit_code or 1)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--supervisor",
        required=True,
        choices=["systemd", "launchd", "supervisord", "kubernetes", "manual"],
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv) if argv is not None else None)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return observe(args.supervisor, command)


if __name__ == "__main__":
    raise SystemExit(main())
