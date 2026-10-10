"""Install or repair the periodic fleet-context refresh on this host.

``mac admin fleet refresh-context`` rewrites the live "Fleet — your teammates"
and mood blocks in this agent's runtime-context markdown, which Hermes loads
into every session. It only helps if something runs it, and on 2026-10-07 none
of the three hosts did: rocky's hand-made launchd job and natasha's systemd unit
still called the removed ``mac fleet refresh-context``, natasha's timer had sat
in ``failed`` since Sep 13 (systemd never retries a failed timer), and
bullwinkle never had the unit because ``deploy/install-fleet-context-service.sh``
looks for the user in a ``mac.service`` that only the hub has.

``install`` makes this host match the repository, idempotently:

* **Linux:** ``deploy/systemd/mac-fleet-context.{service,timer}`` rendered for
  the current user into ``/etc/systemd/system`` (``sudo -n``), any failed state
  reset, and the timer enabled and started.
* **macOS:** ``~/.mac/bin/fleet-context`` and the per-user LaunchAgent
  ``com.mac.fleet-context`` (every 180 s), reloaded when either changed.

It then runs one refresh through the installed job, so a broken job is reported
now rather than discovered later. ``scripts/fleet-update`` runs this on every
host; it prints one JSON line and never touches dispatch holds or other units.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import plistlib
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

UNIT = "mac-fleet-context"
LABEL = "com.mac.fleet-context"
INTERVAL_SECONDS = 180

#: Runs a command and returns (exit code, combined output). Injected by tests.
Runner = Callable[[Sequence[str]], "tuple[int, str]"]

LAUNCHER = """#!/bin/bash
# Managed by mac.fleet_context_service; replaced on every fleet update.
set -a; [ -f "{home}/mac.env" ] && . "{home}/mac.env"; set +a
exec "{home}/venv/bin/mac" admin fleet refresh-context --agent "${{MAC_AGENT_ID:-${{MAC_WORKER_AGENT_ID:-}}}}"
"""


def run(cmd: Sequence[str]) -> "tuple[int, str]":
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _write_if_changed(path: Path, text: str, mode: int) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        if path.stat().st_mode & 0o777 != mode:
            os.chmod(path, mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)
    return True


def _last_line(output: str) -> str:
    lines = [ln for ln in output.splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


# --- Linux ------------------------------------------------------------------


def render_units(source: Path, user: str, mac_home: Path) -> Dict[str, str]:
    unit_dir = source / "deploy" / "systemd"
    service = (unit_dir / (UNIT + ".service")).read_text(encoding="utf-8")
    service = service.replace("__MAC_USER__", user).replace("__MAC_HOME__", str(mac_home))
    return {
        UNIT + ".service": service,
        UNIT + ".timer": (unit_dir / (UNIT + ".timer")).read_text(encoding="utf-8"),
    }


def install_systemd(
    *, source: Path, user: str, mac_home: Path, unit_dir: Path, runner: Runner
) -> Dict[str, str]:
    changed: List[str] = []
    for name, text in render_units(source, user, mac_home).items():
        dest = unit_dir / name
        if dest.is_file() and dest.read_text(encoding="utf-8", errors="replace") == text:
            continue
        with tempfile.NamedTemporaryFile("w", suffix="." + name, delete=False) as tmp:
            tmp.write(text)
        try:
            code, out = runner(["sudo", "-n", "install", "-m", "0644", tmp.name, str(dest)])
        finally:
            os.unlink(tmp.name)
        if code:
            return {"status": "error", "error": "installing %s: %s" % (dest, _last_line(out))}
        changed.append(name)
    if changed:
        runner(["sudo", "-n", "systemctl", "daemon-reload"])
    # A failed timer stays failed until reset; that is how natasha lost it.
    runner(["sudo", "-n", "systemctl", "reset-failed", UNIT + ".timer", UNIT + ".service"])
    code, out = runner(["sudo", "-n", "systemctl", "enable", "--now", UNIT + ".timer"])
    if code:
        return {"status": "error", "error": "enabling the timer: %s" % _last_line(out)}
    code, out = runner(["sudo", "-n", "systemctl", "start", UNIT + ".service"])
    result = {
        "status": "ok" if code == 0 else "error",
        "units": "updated: %s" % ", ".join(changed) if changed else "unchanged",
    }
    if code:
        _, journal = runner(["journalctl", "-u", UNIT + ".service", "-n", "5", "--no-pager"])
        result["error"] = "first refresh failed: %s" % _last_line(journal or out)
    else:
        result["refresh"] = "ok"
    return result


# --- macOS ------------------------------------------------------------------


def render_plist(launcher: Path, log: Path) -> str:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(launcher)],
            "StartInterval": INTERVAL_SECONDS,
            "RunAtLoad": True,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
    ).decode("utf-8")


def install_launchd(*, mac_home: Path, agents_dir: Path, runner: Runner) -> Dict[str, str]:
    launcher = mac_home / "bin" / "fleet-context"
    plist = agents_dir / (LABEL + ".plist")
    changed = [
        name
        for name, path, text, mode in (
            ("launcher", launcher, LAUNCHER.format(home=mac_home), 0o755),
            ("plist", plist, render_plist(launcher, mac_home / "logs" / "fleet-context.log"), 0o644),
        )
        if _write_if_changed(path, text, mode)
    ]
    domain = "gui/%d" % os.getuid()
    loaded = runner(["launchctl", "print", "%s/%s" % (domain, LABEL)])[0] == 0
    if loaded and "plist" in changed:
        runner(["launchctl", "bootout", "%s/%s" % (domain, LABEL)])
        loaded = False
    if not loaded:
        code, out = runner(["launchctl", "bootstrap", domain, str(plist)])
        if code:
            return {"status": "error", "error": "loading %s: %s" % (LABEL, _last_line(out))}
    code, out = runner([str(launcher)])
    result = {
        "status": "ok" if code == 0 else "error",
        "units": "updated: %s" % ", ".join(changed) if changed else "unchanged",
    }
    if code:
        result["error"] = "first refresh failed: %s" % _last_line(out)
    else:
        result["refresh"] = "ok"
    return result


# --- entry point ------------------------------------------------------------


def install(
    *,
    source: Path,
    mac_home: Optional[Path] = None,
    system: Optional[str] = None,
    unit_dir: Path = Path("/etc/systemd/system"),
    agents_dir: Optional[Path] = None,
    systemd_running: Optional[bool] = None,
    runner: Runner = run,
) -> Dict[str, str]:
    if mac_home is None:
        from mac import mac_paths

        mac_home = mac_paths.mac_home()
    system = system or platform.system()
    if system == "Darwin":
        return install_launchd(
            mac_home=mac_home,
            agents_dir=agents_dir or Path.home() / "Library" / "LaunchAgents",
            runner=runner,
        )
    if system == "Linux":
        if systemd_running is None:
            systemd_running = Path("/run/systemd/system").is_dir()
        if not systemd_running:
            return {"status": "skipped", "reason": "systemd is not running"}
        return install_systemd(
            source=source, user=getpass.getuser(), mac_home=mac_home, unit_dir=unit_dir, runner=runner
        )
    return {"status": "skipped", "reason": "unsupported platform %s" % system}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="the MAC checkout holding deploy/systemd")
    parser.add_argument("--mac-home", help="default: $MAC_HOME, else ~/.mac")
    args = parser.parse_args(argv)
    try:
        result = install(
            source=Path(args.source).expanduser(),
            mac_home=Path(args.mac_home).expanduser() if args.mac_home else None,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never half-silent
        result = {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
