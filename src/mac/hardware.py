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
             "shared": True, "count": 1},
     "gpus": [{"index": 0, "accelerator": "cuda", "name": "NVIDIA GB10",
               "shared": True,
               "memory": {"type": "unified", "shared_mb": 122000}}]}

Unified-memory GPUs (GB10, Apple Silicon) share system memory; they are
reported with ``shared=True`` and a structured ``memory.type`` of ``unified``.
When system memory is measurable, the compatibility ``vram_mb`` field is set to
that capacity so existing route filters can still reason about usable memory.
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
_NVIDIA_UNAVAILABLE_MEMORY_VALUES = ("", "N/A", "[N/A]")


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


def _parse_nvidia_vram_mb(value: str) -> Optional[int]:
    """Parse nvidia-smi memory.total output in MiB, or None when unavailable."""
    cleaned = value.strip()
    if cleaned.upper() in _NVIDIA_UNAVAILABLE_MEMORY_VALUES:
        return None
    if cleaned.lower().endswith("mib"):
        cleaned = cleaned[:-3].strip()
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _unified_memory(shared_memory_mb: int) -> Dict[str, Any]:
    memory: Dict[str, Any] = {"type": "unified"}
    if shared_memory_mb:
        memory["shared_mb"] = shared_memory_mb
    return memory


def _dedicated_memory(vram_mb: int) -> Dict[str, Any]:
    return {"type": "dedicated", "vram_mb": vram_mb}


def _gpu_capacity_mb(gpu: Dict[str, Any]) -> int:
    memory = gpu.get("memory") if isinstance(gpu.get("memory"), dict) else {}
    if memory.get("type") == "unified":
        return int(memory.get("shared_mb") or gpu.get("vram_mb") or 0)
    return int(memory.get("vram_mb") or gpu.get("vram_mb") or 0)


def _legacy_gpu_summary(gpus: list[Dict[str, Any]]) -> Dict[str, Any]:
    first = dict(gpus[0])
    first["count"] = len(gpus)
    capacity_mb = _gpu_capacity_mb(first)
    if capacity_mb:
        first["vram_mb"] = capacity_mb
    return first


def _parse_nvidia_gpu_row(row: str, fallback_index: int) -> Optional[Dict[str, Any]]:
    # Current probe shape is index,memory.total,name. Keep memory,total fallback
    # parsing so older fixtures and manually captured output remain readable.
    fields = [field.strip() for field in row.split(",", 2)]
    if len(fields) == 3:
        index_field, memory_field, name = fields
    elif len(fields) == 2:
        index_field, memory_field, name = str(fallback_index), fields[0], fields[1]
    else:
        return None

    try:
        index = int(index_field)
    except ValueError:
        index = fallback_index

    name = name or "NVIDIA GPU"
    vram_mb = _parse_nvidia_vram_mb(memory_field)
    # Only treat as unified when the GPU is a known unified-memory part (e.g. GB10).
    # A non-unified GPU that reports [N/A] or an empty/unparseable value has an
    # *unknown* memory configuration — fabricating a shared_mb from system RAM would
    # be incorrect and misleading.
    unified = _is_unified_memory_gpu(name)
    gpu: Dict[str, Any] = {"index": index, "accelerator": "cuda", "name": name}
    if unified:
        shared_memory_mb = _memory_mb()
        gpu["shared"] = True
        gpu["memory"] = _unified_memory(shared_memory_mb)
        if shared_memory_mb:
            gpu["vram_mb"] = shared_memory_mb
    elif vram_mb:
        gpu["vram_mb"] = vram_mb
        gpu["memory"] = _dedicated_memory(vram_mb)
    else:
        # Probe returned [N/A], empty, or an unrecognised value for a non-unified
        # GPU.  Record the state explicitly so callers can distinguish "probe not
        # available" from "no GPU" rather than inferring a fabricated zero.
        gpu["memory"] = {"type": "unknown"}
    return gpu


def detect_nvidia() -> Optional[Dict[str, Any]]:
    """CUDA GPU via nvidia-smi.  Queries all GPUs in one call.

    Uses ``--query-gpu=index,memory.total,name`` (name last so it may contain
    commas without ambiguity).  Each CSV row represents one physical GPU.

    Unified-memory parts (e.g. GB10) report ``[N/A]`` for ``memory.total``; we
    mark them with ``shared=True`` and structured unified-memory metadata.
    """
    out = _run([
        "nvidia-smi",
        "--query-gpu=index,memory.total,name",
        "--format=csv,noheader,nounits",
    ])
    rows = [r.strip() for r in (out or "").splitlines() if r.strip()]
    if not rows:
        return None

    gpus = [
        gpu
        for idx, row in enumerate(rows)
        for gpu in [_parse_nvidia_gpu_row(row, idx)]
        if gpu is not None
    ]
    if not gpus:
        return None
    result = _legacy_gpu_summary(gpus)
    result["gpus"] = gpus
    return result


def detect_apple_metal() -> Optional[Dict[str, Any]]:
    """Apple Silicon integrated GPU (Metal). Present on every arm64 Mac."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    chip = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
    memory_mb = _memory_mb()
    gpu: Dict[str, Any] = {
        "index": 0,
        "accelerator": "metal",
        "name": chip,
        "shared": True,
        "memory": _unified_memory(memory_mb),
    }
    if memory_mb:
        gpu["vram_mb"] = memory_mb
    result = _legacy_gpu_summary([gpu])
    result["gpus"] = [gpu]
    return result


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
        if isinstance(gpu.get("gpus"), list):
            info["gpus"] = gpu["gpus"]
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
        vram = _gpu_capacity_mb(gpu)
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
