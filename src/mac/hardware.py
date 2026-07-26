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

# HGX "flavor": NVIDIA HGX baseboards host multiple SXM GPUs on one carrier
# (e.g. "NVIDIA H100 80GB HGX", "NVIDIA H200", "NVIDIA A100-SXM4-80GB"). These
# parts are onboarded as a *fungible* pool — the fleet treats their GPUs as
# interchangeable baseboard slices — so the snapshot carries a coarse ``flavor``
# ("hgx" | "pcie" | "unified") that survives onboarding for pool placement.
# nvidia-smi does not expose the flavor directly, so it is derived from the
# product name: SXM/HGX marketing strings, or an explicit "HGX" token.
_HGX_NAME_SUBSTRINGS = ("HGX", "SXM")
# Known HGX-class SXM product families whose marketing name omits SXM/HGX in
# some driver builds (nvidia-smi shortens "NVIDIA H100 80GB HGX" to "NVIDIA
# H100"). Matched as whole product tokens so a PCIe "H100 PCIe" never matches.
_HGX_PRODUCT_TOKENS = ("H100", "H200", "H800", "GH200", "B200", "GB200")
_HGX_PCIE_MARKER = "PCIE"


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


def _gpu_flavor(name: str, *, unified: bool) -> str:
    """Coarse onboarding flavor for a GPU: ``unified`` | ``hgx`` | ``pcie``.

    The flavor is what the fungible-onboarding path carries into the registry so
    the fleet can pool HGX baseboard slices as interchangeable capacity. It is a
    best-effort classification of the *part*, derived from the product name only
    (nvidia-smi has no flavor query), and defaults to ``pcie`` when unknown so a
    discrete card is never mis-pooled as an HGX slice.
    """
    if unified:
        return "unified"
    name_upper = name.upper()
    if _HGX_PCIE_MARKER in name_upper:
        return "pcie"
    if any(marker in name_upper for marker in _HGX_NAME_SUBSTRINGS):
        return "hgx"
    tokens = set(name_upper.replace("-", " ").split())
    if any(token in tokens for token in _HGX_PRODUCT_TOKENS):
        return "hgx"
    return "pcie"


def _confinement_topology(gpus: list[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize how the host's accelerators are confined for onboarding.

    The confinement topology travels with the fungible-onboarding snapshot so a
    scheduler can reason about interchangeable capacity without re-probing the
    node. ``kind`` is:

      - ``none``            no accelerator detected
      - ``unified``         unified/shared-memory accelerator(s) (GB10, Apple)
      - ``hgx-baseboard``   one or more HGX/SXM GPUs sharing a baseboard
      - ``discrete``        one or more discrete PCIe GPUs

    ``gpus`` is the confined GPU count and ``flavors`` lists the distinct flavors
    present (a mixed host reports every flavor it carries).
    """
    if not gpus:
        return {"kind": "none", "gpus": 0}
    flavors = sorted({str(g.get("flavor")) for g in gpus if g.get("flavor")})
    count = len(gpus)
    if any(g.get("flavor") == "hgx" for g in gpus):
        kind = "hgx-baseboard"
    elif all(g.get("shared") for g in gpus):
        kind = "unified"
    else:
        kind = "discrete"
    topology: Dict[str, Any] = {"kind": kind, "gpus": count}
    if flavors:
        topology["flavors"] = flavors
    return topology


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
    gpu: Dict[str, Any] = {
        "index": index,
        "accelerator": "cuda",
        "name": name,
        "flavor": _gpu_flavor(name, unified=unified),
    }
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
        "flavor": "unified",
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
    # Carry the confinement topology (and, for a homogeneous host, the single
    # onboarding flavor) so the fungible-onboarding snapshot the fleet stores is
    # self-describing — the scheduler pools HGX baseboard slices without having
    # to re-probe the node.
    gpu_list = info.get("gpus") if isinstance(info.get("gpus"), list) else []
    info["topology"] = _confinement_topology(gpu_list)
    flavors = info["topology"].get("flavors") or []
    if len(flavors) == 1:
        info["flavor"] = flavors[0]
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
        # Surface the HGX baseboard confinement so `mac agent hardware` shows the
        # fungible-pool flavor at a glance.
        topology = hardware.get("topology") if isinstance(hardware.get("topology"), dict) else None
        if topology and topology.get("kind") == "hgx-baseboard":
            parts.append("hgx-baseboard")
    else:
        parts.append("accelerator=%s" % accel)
    parts.append("%s/%s" % (hardware.get("os", "?"), hardware.get("arch", "?")))
    if hardware.get("cpu_count"):
        parts.append("%dcpu" % hardware["cpu_count"])
    if hardware.get("memory_mb"):
        parts.append("%dGB" % (hardware["memory_mb"] / 1024))
    return " · ".join(parts)
