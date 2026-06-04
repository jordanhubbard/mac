"""First-class hardware self-reporting.

Agents detect their local accelerator + CPU/memory and report it into the
registry (``resources["hardware"]``) at registration, so the fleet OWNS a
hardware inventory instead of relying on hand-maintained notes (which drift and
get an agent's silicon wrong). The hub can then derive compute/gen capability
from facts — e.g. "which agent has a CUDA GPU" — and operators query it with
``mac agent hardware``.

Pure stdlib + best-effort subprocess probes. Detection NEVER raises: a probe
failure yields ``accelerator: "none"`` / omitted fields, never a failed
registration. The snapshot shape::

    {"os": "linux", "arch": "aarch64", "cpu_count": 20, "memory_mb": 122000,
     "accelerator": "cuda",                       # cuda | metal | none
     "gpu": {"accelerator": "cuda", "name": "NVIDIA GB10", "vram_mb": 0, "count": 1}}
"""
from __future__ import annotations

import os
import platform
import subprocess
from typing import Any, Dict, Optional

SCHEMA = "mac.hardware.v1"


def _run(cmd: list, timeout: float = 5.0) -> Optional[str]:
    """Run a probe command, returning stdout on success or None on any failure
    (missing binary, non-zero exit, timeout). Never raises."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 - probe is best-effort
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _memory_mb() -> int:
    system = platform.system()
    try:
        if system == "Linux":
            with open("/proc/meminfo", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        return int(int(line.split()[1]) / 1024)  # kB -> MB
        elif system == "Darwin":
            out = _run(["sysctl", "-n", "hw.memsize"])
            if out:
                return int(int(out) / (1024 * 1024))  # bytes -> MB
    except Exception:  # noqa: BLE001
        pass
    return 0


def detect_nvidia() -> Optional[Dict[str, Any]]:
    """CUDA GPU via nvidia-smi. One CSV row per GPU: ``name, memory.total``.
    GB10 (unified memory) reports memory.total as ``[N/A]`` → vram_mb 0."""
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total",
        "--format=csv,noheader,nounits",
    ])
    rows = [r.strip() for r in (out or "").splitlines() if r.strip()]
    if not rows:
        return None
    fields = [c.strip() for c in rows[0].split(",")]
    name = fields[0] if fields else "NVIDIA GPU"
    vram_mb = 0
    if len(fields) > 1:
        try:
            vram_mb = int(float(fields[1]))
        except ValueError:
            vram_mb = 0
    return {"accelerator": "cuda", "name": name, "vram_mb": vram_mb, "count": len(rows)}


def detect_apple_metal() -> Optional[Dict[str, Any]]:
    """Apple Silicon integrated GPU (Metal). Present on every arm64 Mac."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
    return {"accelerator": "metal", "name": chip, "vram_mb": 0, "count": 1}


def detect_hardware() -> Dict[str, Any]:
    """Best-effort local hardware snapshot for the agent registry. Never raises."""
    info: Dict[str, Any] = {
        "schema": SCHEMA,
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count() or 0,
        "memory_mb": _memory_mb(),
        "accelerator": "none",
    }
    try:
        gpu = detect_nvidia() or detect_apple_metal()
    except Exception:  # noqa: BLE001 - detection must never break registration
        gpu = None
    if gpu:
        info["gpu"] = gpu
        info["accelerator"] = gpu.get("accelerator", "none")
    return info


def summarize(hardware: Optional[Dict[str, Any]]) -> str:
    """One-line human summary for `mac agent hardware`."""
    if not isinstance(hardware, dict):
        return "(no hardware reported)"
    accel = hardware.get("accelerator", "none")
    gpu = hardware.get("gpu") if isinstance(hardware.get("gpu"), dict) else None
    parts = []
    if gpu:
        vram = gpu.get("vram_mb") or 0
        count = gpu.get("count") or 1
        label = "%s x%d" % (gpu.get("name", "GPU"), count) if count > 1 else str(gpu.get("name", "GPU"))
        parts.append("%s [%s%s]" % (label, accel, (" %dGB" % (vram / 1024)) if vram else ""))
    else:
        parts.append("accelerator=%s" % accel)
    parts.append("%s/%s" % (hardware.get("os", "?"), hardware.get("arch", "?")))
    if hardware.get("cpu_count"):
        parts.append("%dcpu" % hardware["cpu_count"])
    if hardware.get("memory_mb"):
        parts.append("%dGB" % (hardware["memory_mb"] / 1024))
    return " · ".join(parts)
