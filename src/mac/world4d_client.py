"""Manage a Hermes client's canonical World4D binding and runtime presence.

Standalone management entrypoint. Configuration writes use Hermes' native cron
API; no model turn, cron delivery or database credentials are needed here.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request


def rpc(command, method, params=None):
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    proc = subprocess.run(
        command, input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=30
    )
    if proc.returncode:
        raise RuntimeError("World4D transport failed (details withheld)")
    for line in proc.stdout.splitlines():
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if reply.get("id") == 1:
            if "error" in reply:
                raise RuntimeError("World4D management request rejected")
            return reply["result"]
    raise RuntimeError("World4D transport returned no reply")


def identity(manifest):
    return {k: manifest[k] for k in ("realm_id", "world", "backend")}


def verify_realm(manifest):
    if rpc(manifest["command"], "mac/world4d_identity") != identity(manifest):
        raise ValueError("World4D client points at a different realm")
    endpoint = manifest["monitor_url"].removesuffix("/snapshot-on-change") + "/identity"
    with urllib.request.urlopen(endpoint, timeout=5) as response:
        if json.load(response) != identity(manifest):
            raise ValueError("World4D reporter points at a different realm")


def gateway_health(home, now=None):
    """Require a fresh heartbeat, matching process birth and a live event loop."""
    now = time.time() if now is None else now
    try:
        heartbeat = json.loads((home / "state/gateway.heartbeat").read_text())
        pid = int(heartbeat["pid"])
        if pid <= 1:
            return "unknown"
        os.kill(pid, 0)
        age = now - datetime.fromisoformat(heartbeat["updated_at"]).timestamp()
        if not 0 <= age <= 60:
            return "unknown"
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=3,
            env={**os.environ, "LC_ALL": "C"},
        )
        born = datetime.strptime(proc.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
        if abs(born - float(heartbeat["start_time"])) > 2:
            return "unknown"
        if not heartbeat.get("loop_tick_socket"):
            return "unknown"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(2)
            sock.connect(str(home / "state" / f"gateway.loop-tick.{pid}.sock"))
            if sock.recv(1) != b"1":
                return "unknown"
        latest = json.loads((home / "state/gateway.heartbeat").read_text())
        return (
            "online"
            if (latest["pid"], latest["start_time"]) == (pid, heartbeat["start_time"])
            else "unknown"
        )
    except ProcessLookupError:
        return "offline"
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return "unknown"


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def publish(manifest, path):
    verify_realm(manifest)
    status = gateway_health(Path(manifest["hermes_home"]))
    observed = datetime.now(timezone.utc).isoformat()
    reply = rpc(
        manifest["command"],
        "mac/world4d_runtime_presence",
        {"realm_id": manifest["realm_id"], "status": status, "observed_at": observed},
    )
    atomic_json(
        path.with_name("world4d-presence-receipt.json"),
        {
            **identity(manifest),
            "agent": manifest["agent"],
            "observed_at": observed,
            "status": status,
            "published": reply.get("updated", False),
        },
    )
    return {
        "agent": manifest["agent"],
        "runtime_status": status,
        "published": reply.get("updated", False),
    }


def report_patch(manifest):
    """Move an explicitly managed legacy report to native change detection/delivery."""
    return {
        "prompt": "The monitor contains JSON from the supervised shared World4D realm. "
        "Return its summary string exactly. Do not query other world stores, write soul "
        "snapshots or post to Slack yourself; cron delivers changed observations.",
        "monitor_url": manifest["monitor_url"],
        "monitor_script": None,
        "script": None,
        "enabled_toolsets": ["no_mcp"],
        "deliver": "origin",
        "skills": [],
    }


def audit(manifest, config, jobs, hook):
    errors = []
    servers = config.get("mcp_servers", {})
    actual = servers.get("world4d", {})
    if [actual.get("command"), *actual.get("args", [])] != manifest["command"] or actual.get(
        "enabled"
    ) is False:
        errors.append("canonical MCP binding differs")
    for name in manifest.get("retired_servers", []):
        if name in servers and servers[name].get("enabled", True):
            errors.append(f"competing world server enabled: {name}")
    by_id = {job["id"]: job for job in jobs}
    for job_id in manifest.get("work_jobs", []):
        job = by_id.get(job_id, {})
        skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
        if "world4d-coordination" not in skills:
            errors.append(f"work job lacks coordination skill: {job_id}")
    for job_id in manifest.get("report_jobs", []):
        job = by_id.get(job_id, {})
        if any(job.get(key) != value for key, value in report_patch(manifest).items()):
            errors.append(f"report job uses unmanaged source or delivery: {job_id}")
    if not hook.is_file() or not os.access(hook, os.X_OK):
        errors.append("runtime presence hook missing")
    elif hook.read_text() != launcher(manifest):
        errors.append("runtime presence hook differs")
    refresh = Path(manifest["refresh_launcher"])
    if not refresh.is_file() or "bin/mac-world4d-presence" not in refresh.read_text():
        errors.append("fleet-context service does not invoke runtime presence")
    skill = Path(manifest["hermes_home"]) / "skills/world4d-coordination/SKILL.md"
    if not skill.is_file():
        errors.append("coordination skill is not installed")
    return errors


def launcher(manifest):
    return (
        "#!/bin/sh\nexec "
        + shlex.join(
            [manifest["python"], manifest["client_module"], "presence", manifest["manifest_path"]]
        )
        + "\n"
    )


def install(manifest):
    """Caller snapshots private configuration first; only declared objects change."""
    import yaml
    from cron.jobs import get_job, load_jobs, update_job

    verify_realm(manifest)
    # Validate every referenced job and delivery before any configuration write.
    for job_id in manifest.get("work_jobs", []) + manifest.get("report_jobs", []):
        job = get_job(job_id)
        if job is None:
            raise ValueError(f"managed job missing: {job_id}")
        if job_id in manifest.get("report_jobs", []) and not job.get("origin"):
            raise ValueError("report job has no original delivery destination")
    config_path = Path(manifest["hermes_home"]) / "config.yaml"
    backup = (
        Path(manifest["manifest_path"]).parent
        / "backups"
        / ("world4d-client-" + str(time.time_ns()))
    )
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(config_path, backup / "config.yaml")
    (backup / "config.yaml").chmod(0o600)
    atomic_json(
        backup / "jobs.json",
        [get_job(j) for j in manifest.get("work_jobs", []) + manifest.get("report_jobs", [])],
    )
    config = yaml.safe_load(config_path.read_text())
    servers = config.setdefault("mcp_servers", {})
    servers["world4d"] = {
        "command": manifest["command"][0],
        "args": manifest["command"][1:],
        "enabled": True,
    }
    for name in manifest.get("retired_servers", []):
        if name in servers:
            servers[name]["enabled"] = False
    temp = config_path.with_suffix(".world4d.tmp")
    temp.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    temp.chmod(0o600)
    temp.replace(config_path)
    for job_id in manifest.get("work_jobs", []):
        job = get_job(job_id)
        skills = list(job.get("skills") or ([job["skill"]] if job.get("skill") else []))
        if "world4d-coordination" not in skills:
            update_job(job_id, {"skills": [*skills, "world4d-coordination"]})
    for job_id in manifest.get("report_jobs", []):
        if not update_job(job_id, report_patch(manifest)):
            raise RuntimeError("could not migrate managed reporting job")
    hook = Path(manifest["hook"])
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(launcher(manifest))
    hook.chmod(0o700)
    errors = audit(manifest, config, load_jobs(), hook)
    if errors:
        raise RuntimeError("; ".join(errors))
    return {"agent": manifest["agent"], "installed": True, "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["install", "audit", "presence"])
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.operation != "presence":
        sys.path.insert(0, manifest["hermes_runtime"])
    if args.operation == "presence":
        result = publish(manifest, args.manifest)
    elif args.operation == "install":
        result = install(manifest)
    else:
        import yaml
        from cron.jobs import load_jobs

        verify_realm(manifest)
        errors = audit(
            manifest,
            yaml.safe_load((Path(manifest["hermes_home"]) / "config.yaml").read_text()),
            load_jobs(),
            Path(manifest["hook"]),
        )
        receipt_path = args.manifest.with_name("world4d-presence-receipt.json")
        try:
            receipt = json.loads(receipt_path.read_text())
            age = time.time() - datetime.fromisoformat(receipt["observed_at"]).timestamp()
            if not 0 <= age <= 420 or receipt.get("status") != "online":
                errors.append("runtime presence receipt is stale or unhealthy")
        except (OSError, ValueError, KeyError, TypeError):
            errors.append("runtime presence receipt missing or invalid")
        result = {"agent": manifest["agent"], "errors": errors}
        if errors:
            print(json.dumps(result))
            raise SystemExit(1)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
