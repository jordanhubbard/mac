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
     "gpu": {"accelerator": "cuda", "name": "NVIDIA GB10", "vram_mb": 122000,
             "shared": True, "count": 1}}

Unified-memory GPUs (GB10, Apple Silicon) share system memory; they are
reported with ``shared=True`` and ``vram_mb`` set to the total system memory so
the hub can still reason about available memory capacity.
"""
from __future__ import annotations

import os
import platform
import subprocess
from typing import Any, Dict, Optional

SCHEMA = "mac.hardware.v1"

# GPU names that indicate unified / shared memory (no dedicated VRAM).
# These parts report [N/A] for memory.total in nvidia-smi.
_UNIFIED_MEMORY_SUBSTRINGS = ("GB10",)


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


def _is_unified_memory_gpu(name: str) -> bool:
    """Return True for GPUs that use unified / shared system memory (no VRAM)."""
    name_upper = name.upper()
    return any(s.upper() in name_upper for s in _UNIFIED_MEMORY_SUBSTRINGS)


def detect_nvidia() -> Optional[Dict[str, Any]]:
    """CUDA GPU via nvidia-smi.  Queries all GPUs in one call.

    Uses ``--query-gpu=memory.total,name`` (memory first so the name field may
    contain commas without ambiguity).  Each CSV row represents one physical GPU.

    Unified-memory parts (e.g. GB10) report ``[N/A]`` for ``memory.total``; we
    mark them with ``shared=True`` and use system memory for ``vram_mb`` so the
    hub has a meaningful capacity figure.
    """
    out = _run([
        "nvidia-smi",
        "--query-gpu=memory.total,name",
        "--format=csv,noheader,nounits",
    ])
    rows = [r.strip() for r in (out or "").splitlines() if r.strip()]
    if not rows:
        return None

    # Parse first row to determine GPU type; all rows should be the same GPU
    # family for a homogeneous host, but count is always total rows.
    first = rows[0]
    # memory.total is the first field; name is everything after the first comma
    comma_idx = first.find(",")
    if comma_idx == -1:
        return None
    mem_field = first[:comma_idx].strip()
    name = first[comma_idx + 1:].strip()

    if not name:
        name = "NVIDIA GPU"

    # Determine VRAM.  Unified-memory GPUs report "[N/A]" for memory.total.
    unified = _is_unified_memory_gpu(name)
    vram_mb: int
    if unified or mem_field.upper() in ("[N/A]", "N/A", ""):
        # Shared unified memory: report system RAM as vram_mb and flag shared.
        vram_mb = _memory_mb()
        shared = True
    else:
        try:
            vram_mb = int(float(mem_field))
        except ValueError:
            vram_mb = 0
        shared = False

    result: Dict[str, Any] = {
        "accelerator": "cuda",
        "name": name,
        "vram_mb": vram_mb,
        "count": len(rows),
    }
    if shared:
        result["shared"] = True
    return result


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
        shared = gpu.get("shared", False)
        label = "%s x%d" % (gpu.get("name", "GPU"), count) if count > 1 else str(gpu.get("name", "GPU"))
        vram_label = (" %dGB%s" % (vram / 1024, " shared" if shared else "")) if vram else ""
        parts.append("%s [%s%s]" % (label, accel, vram_label))
    else:
        parts.append("accelerator=%s" % accel)
    parts.append("%s/%s" % (hardware.get("os", "?"), hardware.get("arch", "?")))
    if hardware.get("cpu_count"):
        parts.append("%dcpu" % hardware["cpu_count"])
    if hardware.get("memory_mb"):
        parts.append("%dGB" % (hardware["memory_mb"] / 1024))
    return " · ".join(parts)
