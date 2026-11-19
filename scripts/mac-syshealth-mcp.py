#!/usr/bin/env python3
"""
mac-syshealth-mcp: MCP server exposing system health tools to the fleet.

Tools:
  get_system_health    - full snapshot: disk, memory, swap, load, patch staleness
  get_disk_usage       - disk free/used/% for key paths
  get_memory_pressure  - VM pressure, wired, compressed, active/inactive pages
  get_patch_staleness  - days since last OS/brew security update
  check_fleet_node     - ping a fleet node and report its disk/mem via SSH

Thresholds (defaults, overridable via env):
  HEALTH_DISK_WARN_PCT   = 80  (warn at 80% full)
  HEALTH_DISK_CRIT_PCT   = 90
  HEALTH_MEM_WARN_MB     = 512 (warn if less than 512 MB free)
  HEALTH_MEM_CRIT_MB     = 256
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

DISK_WARN_PCT = int(os.environ.get("HEALTH_DISK_WARN_PCT", 80))
DISK_CRIT_PCT = int(os.environ.get("HEALTH_DISK_CRIT_PCT", 90))
MEM_WARN_MB   = int(os.environ.get("HEALTH_MEM_WARN_MB", 512))
MEM_CRIT_MB   = int(os.environ.get("HEALTH_MEM_CRIT_MB", 256))


def _disk_info(path="/"):
    total, used, free = shutil.disk_usage(path)
    pct = round(used / total * 100, 1)
    def _fmt(b):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"
    status = "ok"
    if pct >= DISK_CRIT_PCT:
        status = "critical"
    elif pct >= DISK_WARN_PCT:
        status = "warning"
    return {
        "path": path,
        "total": _fmt(total),
        "used": _fmt(used),
        "free": _fmt(free),
        "used_pct": pct,
        "status": status,
        "threshold_warn": DISK_WARN_PCT,
        "threshold_crit": DISK_CRIT_PCT,
    }


def _memory_info():
    """Parse vm_stat for macOS memory pressure."""
    try:
        out = subprocess.check_output(["vm_stat"], text=True)
    except Exception as e:
        return {"error": str(e)}
    page_size = 16384  # bytes, standard on Apple Silicon
    stats = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            v = v.strip().rstrip(".")
            try:
                stats[k.strip()] = int(v) * page_size
            except ValueError:
                pass

    def mb(b): return round(b / 1024 / 1024, 1)

    free_mb = mb(stats.get("Pages free", 0))
    inactive_mb = mb(stats.get("Pages inactive", 0))
    active_mb = mb(stats.get("Pages active", 0))
    wired_mb = mb(stats.get("Pages wired down", 0))
    compressed_mb = mb(stats.get("Pages stored in compressor", 0))
    available_mb = free_mb + inactive_mb  # reclaimable

    status = "ok"
    if free_mb < MEM_CRIT_MB:
        status = "critical"
    elif free_mb < MEM_WARN_MB:
        status = "warning"

    return {
        "free_mb": free_mb,
        "available_mb": available_mb,
        "active_mb": active_mb,
        "inactive_mb": inactive_mb,
        "wired_mb": wired_mb,
        "compressed_mb": compressed_mb,
        "status": status,
        "threshold_warn_mb": MEM_WARN_MB,
        "threshold_crit_mb": MEM_CRIT_MB,
        "note": "free_mb is the strict free pool; available_mb includes reclaimable inactive pages",
    }


def _patch_staleness():
    """Days since last softwareupdate / brew upgrade."""
    result = {}
    try:
        plist_path = "/Library/Preferences/com.apple.SoftwareUpdate.plist"
        out = subprocess.check_output(
            ["defaults", "read", plist_path, "LastSuccessfulDate"],
            text=True, stderr=subprocess.DEVNULL
        ).strip()
        if out:
            ts = datetime.strptime(out[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - ts).days
            result["os_update_last_check_days"] = days
            result["os_update_status"] = "ok" if days < 7 else ("warning" if days < 30 else "stale")
    except Exception as e:
        result["os_update_error"] = str(e)

    try:
        cellar = "/opt/homebrew/Cellar"
        if os.path.exists(cellar):
            mtime = os.path.getmtime(cellar)
            days = int((time.time() - mtime) / 86400)
            result["brew_cellar_last_changed_days"] = days
            result["brew_status"] = "ok" if days < 7 else ("warning" if days < 30 else "stale")
    except Exception as e:
        result["brew_error"] = str(e)

    return result


def _load_avg():
    la = os.getloadavg()
    return {"1m": round(la[0], 2), "5m": round(la[1], 2), "15m": round(la[2], 2)}


def _system_health():
    disk = _disk_info("/")
    mem = _memory_info()
    patches = _patch_staleness()
    load = _load_avg()

    statuses = [disk.get("status", "ok"), mem.get("status", "ok"),
                patches.get("os_update_status", "ok"), patches.get("brew_status", "ok")]
    if "critical" in statuses:
        overall = "critical"
    elif "warning" in statuses or "stale" in statuses:
        overall = "warning"
    else:
        overall = "ok"

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "overall": overall,
        "disk": disk,
        "memory": mem,
        "load_avg": load,
        "patches": patches,
    }


def _check_fleet_node(hostname: str, agent_id: str = None):
    """SSH to a fleet node and collect its disk + memory."""
    cmd = (
        "df -k / | awk 'NR==2{used=$3; avail=$4; pct=$5; print used, avail, pct}' "
        "&& vm_stat | grep 'Pages free' | awk '{print $3}' | tr -d '.'"
    )
    try:
        out = subprocess.check_output(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             f"jkh@{hostname}", cmd],
            text=True, timeout=15
        ).strip().splitlines()
        used_kb = int(out[0].split()[0])
        avail_kb = int(out[0].split()[1])
        pct_str = out[0].split()[2]
        free_pages = int(out[1]) if len(out) > 1 else 0
        free_mb = round(free_pages * 16384 / 1024 / 1024, 1)

        disk_pct = int(pct_str.rstrip("%"))
        disk_status = "critical" if disk_pct >= DISK_CRIT_PCT else ("warning" if disk_pct >= DISK_WARN_PCT else "ok")

        return {
            "hostname": hostname,
            "agent_id": agent_id,
            "disk_used_pct": disk_pct,
            "disk_avail_gb": round(avail_kb / 1024 / 1024, 1),
            "disk_status": disk_status,
            "mem_free_mb": free_mb,
            "mem_status": "critical" if free_mb < MEM_CRIT_MB else ("warning" if free_mb < MEM_WARN_MB else "ok"),
        }
    except Exception as e:
        return {"hostname": hostname, "error": str(e)}


# ── MCP server (mcp 2.x / MCPServer API) ───────────────────────────────────

def main():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("mac-syshealth")

    @mcp.tool()
    def get_system_health() -> str:
        """Full system health snapshot: disk, memory, load average, patch staleness.
        Returns overall status (ok/warning/critical) plus per-subsystem detail.
        Call at session start or when you suspect resource pressure."""
        return json.dumps(_system_health(), indent=2)

    @mcp.tool()
    def get_disk_usage(paths: list[str] = None) -> str:
        """Disk usage for one or more paths. Returns warning/critical flags.
        Args:
            paths: Filesystem paths to check (default: ['/'])
        """
        return json.dumps([_disk_info(p) for p in (paths or ["/"])], indent=2)

    @mcp.tool()
    def get_memory_pressure() -> str:
        """macOS VM stats in MB: free, available, active, inactive, wired, compressed.
        Returns warning/critical if free memory is low."""
        return json.dumps(_memory_info(), indent=2)

    @mcp.tool()
    def get_patch_staleness() -> str:
        """Days since last macOS software update check and last Homebrew cellar change.
        Returns warning if >7 days, stale if >30 days."""
        return json.dumps(_patch_staleness(), indent=2)

    @mcp.tool()
    def check_fleet_node(hostname: str, agent_id: str = "") -> str:
        """SSH to a fleet node and collect its disk + memory stats.
        Buddy-check: catch exhaustion on peer nodes before they notice.
        Args:
            hostname: IP or hostname (e.g. '100.121.27.109' for Bullwinkle)
            agent_id: Agent label for display (e.g. 'agent_bullwinkle')
        """
        return json.dumps(_check_fleet_node(hostname, agent_id or None), indent=2)

    mcp.run()


if __name__ == "__main__":
    main()
