#!/usr/bin/env bash
set -euo pipefail

# Lightweight, fail-safe agent resource watchdog.  It never kills MAC
# services; it only cleans bounded caches and records starvation evidence.
LOG_DIR="${MAC_HOME:-$HOME/.mac}/logs"
mkdir -p "$LOG_DIR"
REPORT="$LOG_DIR/resource-health.json"
python3 - "$REPORT" <<'PY'
import json, os, shutil, subprocess, sys, time
from pathlib import Path

report = {"schema":"mac.agent_resource_health.v1", "ts":time.time(), "conditions":[], "actions":[]}
home = Path.home()
def add(kind, detail): report["conditions"].append({"kind":kind, **detail})
usage = shutil.disk_usage(home)
free = usage.free / usage.total
if free < 0.05: add("disk_critical", {"free_ratio":free, "free_bytes":usage.free})
elif free < 0.10: add("disk_low", {"free_ratio":free, "free_bytes":usage.free})

mem_total = mem_avail = 0
try:
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        if k == "MemTotal": mem_total = int(v.split()[0]) * 1024
        if k == "MemAvailable": mem_avail = int(v.split()[0]) * 1024
except OSError: pass
if mem_total and mem_avail / mem_total < 0.10: add("memory_low", {"available_ratio":mem_avail/mem_total})

try:
    load1 = os.getloadavg()[0]; cpus = os.cpu_count() or 1
    if load1 / cpus > 2.0: add("cpu_starved", {"load1":load1, "cpus":cpus})
except OSError: pass

# Remove only known disposable caches, oldest first, and only when disk is low.
if free < 0.10:
    for root in (home/".cache", home/".npm"/"_cacache"):
        if not root.exists(): continue
        try:
            files = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p:p.stat().st_mtime)
            removed = 0
            for p in files:
                if shutil.disk_usage(home).free / usage.total >= 0.15: break
                try: removed += p.stat().st_size; p.unlink()
                except OSError: pass
            if removed: report["actions"].append({"action":"clean_cache", "path":str(root), "bytes":removed})
        except OSError: pass
Path(sys.argv[1]).write_text(json.dumps(report, sort_keys=True) + "\n")
if any(c["kind"] in {"disk_critical", "memory_low", "cpu_starved"} for c in report["conditions"]):
    print(json.dumps(report, sort_keys=True))
PY
