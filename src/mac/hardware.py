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
import re
import shutil
import subprocess
from pathlib import Path
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
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_MIB = 1024 * 1024


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


def _read_text(path: Path) -> Optional[str]:
    """Read a small kernel capacity file without making inventory brittle."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 - capacity detection is best-effort
        return None


def _first_text(paths: list[Path]) -> Optional[str]:
    for path in paths:
        value = _read_text(path)
        if value is not None:
            return value
    return None


def _parse_cpuset_count(value: Optional[str]) -> Optional[int]:
    """Count CPUs in a Linux cpuset expression such as ``0-3,8,10-11``."""
    if not value:
        return None
    cpus: set[int] = set()
    try:
        for item in value.split(","):
            start_text, separator, end_text = item.strip().partition("-")
            start = int(start_text)
            end = int(end_text) if separator else start
            if start < 0 or end < start:
                return None
            cpus.update(range(start, end + 1))
    except (TypeError, ValueError):
        return None
    return len(cpus) or None


def _effective_cpu_capacity(host_count: int) -> tuple[float | int, Dict[str, Any]]:
    """Return CPU capacity visible to this process, including cgroup limits."""
    details: Dict[str, Any] = {"host_count": host_count}
    candidates: list[float] = [float(host_count)] if host_count > 0 else []

    try:
        affinity_count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity_count = 0
    if affinity_count:
        details["affinity_count"] = affinity_count
        candidates.append(float(affinity_count))

    cpuset_count = _parse_cpuset_count(
        _first_text(
            [
                _CGROUP_ROOT / "cpuset.cpus.effective",
                _CGROUP_ROOT / "cpuset.cpus",
                _CGROUP_ROOT / "cpuset" / "cpuset.cpus",
            ]
        )
    )
    if cpuset_count:
        details["cpuset_count"] = cpuset_count
        candidates.append(float(cpuset_count))

    quota_cores: Optional[float] = None
    cpu_max = _read_text(_CGROUP_ROOT / "cpu.max")
    if cpu_max:
        fields = cpu_max.split()
        if len(fields) >= 2 and fields[0] != "max":
            try:
                quota = float(fields[0])
                period = float(fields[1])
                if quota >= 0 and period > 0:
                    quota_cores = quota / period
            except ValueError:
                pass
    if quota_cores is None:
        quota_text = _read_text(_CGROUP_ROOT / "cpu" / "cpu.cfs_quota_us")
        period_text = _read_text(_CGROUP_ROOT / "cpu" / "cpu.cfs_period_us")
        try:
            quota = float(quota_text) if quota_text is not None else -1
            period = float(period_text) if period_text is not None else 0
            if quota >= 0 and period > 0:
                quota_cores = quota / period
        except ValueError:
            pass
    if quota_cores is not None:
        details["quota_cores"] = quota_cores
        candidates.append(quota_cores)

    effective = min(candidates) if candidates else 0
    # Preserve the historical integer shape when capacity is an integral number
    # of cores, while retaining fractional quotas for accurate scheduling.
    normalized: float | int = int(effective) if effective.is_integer() else effective
    details["effective_count"] = normalized
    return normalized, details


def _effective_memory_capacity(host_mb: int) -> tuple[int, Dict[str, Any]]:
    """Return memory usable by this process, bounded by cgroup v1/v2."""
    details: Dict[str, Any] = {"host_total_mb": host_mb}
    raw_limit = _first_text(
        [
            _CGROUP_ROOT / "memory.max",
            _CGROUP_ROOT / "memory" / "memory.limit_in_bytes",
        ]
    )
    limit_mb: Optional[int] = None
    if raw_limit and raw_limit != "max":
        try:
            limit_bytes = int(raw_limit)
            # cgroup v1 uses a near-LONG_MAX value to mean unlimited.
            if 0 <= limit_bytes < (1 << 60):
                limit_mb = int(limit_bytes / _MIB)
        except ValueError:
            pass
    if limit_mb is not None:
        details["limit_mb"] = limit_mb
    candidates = [host_mb] if host_mb > 0 else []
    if limit_mb is not None:
        candidates.append(limit_mb)
    effective = min(candidates) if candidates else 0
    details["effective_total_mb"] = effective
    return effective, details


def _cpu_model() -> str:
    """Return the processor model without guessing from its architecture."""
    system = platform.system()
    try:
        if system == "Darwin":
            value = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
            if value:
                return value
        elif system == "Linux":
            with open("/proc/cpuinfo", encoding="utf-8") as handle:
                fields: Dict[str, str] = {}
                for line in handle:
                    key, separator, value = line.partition(":")
                    if separator and key.strip() not in fields:
                        fields[key.strip()] = value.strip()
                for key in ("model name", "Hardware", "Processor"):
                    if fields.get(key):
                        return fields[key]
    except Exception:  # noqa: BLE001 - inventory is best-effort
        pass
    try:
        return str(platform.processor() or "").strip()
    except Exception:  # noqa: BLE001 - inventory is best-effort
        return ""


def _disk_usage_mb() -> Dict[str, int]:
    """Return total and currently available capacity of the worker filesystem."""
    configured = str(os.environ.get("MAC_WORKER_WORKSPACE") or "").strip()
    path = Path(configured).expanduser() if configured else Path.home()
    if not path.exists():
        path = Path.home()
    try:
        usage = shutil.disk_usage(path)
    except Exception:  # noqa: BLE001 - inventory is best-effort
        return {}
    mib = 1024 * 1024
    return {
        "total_mb": int(usage.total / mib),
        "available_mb": int(usage.free / mib),
    }


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
    if all(g.get("flavor") == "mig" for g in gpus):
        kind = "mig-slices"
    elif any(g.get("flavor") == "hgx" for g in gpus):
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
    fields = [field.strip() for field in row.split(",", 3)]
    uuid = ""
    if len(fields) == 4:
        index_field, memory_field, uuid, name = fields
    elif len(fields) == 3:
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
        "render_capable": True,
        "rtx_capable": "RTX" in name.upper(),
    }
    if uuid:
        gpu["uuid"] = uuid
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


_NVIDIA_LIST_GPU = re.compile(
    r"^GPU\s+(?P<index>\d+):\s+(?P<name>.+?)\s+\(UUID:\s*(?P<uuid>GPU-[^)]+)\)"
)
_NVIDIA_LIST_MIG = re.compile(
    r"^\s*MIG\s+(?P<profile>\S+)\s+Device\s+(?P<device>\d+):"
    r"\s+\(UUID:\s*(?P<uuid>MIG-[^)]+)\)"
)
_MIG_MEMORY = re.compile(r"(?:^|\.)?(?P<gb>\d+(?:\.\d+)?)gb(?:$|\.)", re.IGNORECASE)


def _parse_nvidia_mig_devices(output: Optional[str]) -> list[Dict[str, Any]]:
    """Parse effective MIG slices from ``nvidia-smi -L`` output."""
    devices: list[Dict[str, Any]] = []
    parent_index = 0
    parent_name = "NVIDIA GPU"
    parent_uuid = ""
    for line in (output or "").splitlines():
        gpu_match = _NVIDIA_LIST_GPU.match(line)
        if gpu_match:
            parent_index = int(gpu_match.group("index"))
            parent_name = gpu_match.group("name")
            parent_uuid = gpu_match.group("uuid")
            continue
        mig_match = _NVIDIA_LIST_MIG.match(line)
        if not mig_match:
            continue
        profile = mig_match.group("profile")
        memory_match = _MIG_MEMORY.search(profile)
        memory_mb = int(float(memory_match.group("gb")) * 1024) if memory_match else 0
        device: Dict[str, Any] = {
            "index": len(devices),
            "parent_index": parent_index,
            "parent_uuid": parent_uuid,
            "mig_device_index": int(mig_match.group("device")),
            "uuid": mig_match.group("uuid"),
            "accelerator": "cuda",
            "name": "%s MIG %s" % (parent_name, profile),
            "flavor": "mig",
            "mig_profile": profile,
            "render_capable": False,
            "rtx_capable": False,
        }
        if memory_mb:
            device["vram_mb"] = memory_mb
            device["memory"] = _dedicated_memory(memory_mb)
        else:
            device["memory"] = {"type": "unknown"}
        devices.append(device)
    return devices


def _cuda_visible_devices(
    physical: list[Dict[str, Any]], mig_devices: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Apply CUDA visibility to the inventory used by scheduling."""
    candidates = mig_devices or physical
    raw = os.environ.get("CUDA_VISIBLE_DEVICES")
    if raw is None or raw.strip().lower() == "all":
        return candidates
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens or any(token.lower() in {"none", "void", "-1"} for token in tokens):
        return []

    selected: list[Dict[str, Any]] = []
    for candidate in candidates:
        identifiers = {
            str(candidate.get("index", "")),
            str(candidate.get("parent_index", "")),
            str(candidate.get("uuid", "")),
            str(candidate.get("parent_uuid", "")),
        }
        if any(
            token in identifiers
            or any(identifier.startswith(token) for identifier in identifiers if identifier)
            for token in tokens
        ):
            selected.append(candidate)
    return selected


def detect_nvidia() -> Optional[Dict[str, Any]]:
    """CUDA GPU via nvidia-smi.  Queries all GPUs in one call.

    Uses ``--query-gpu=index,memory.total,uuid,name`` (name last so it may
    contain commas without ambiguity), with a compatibility fallback for older
    drivers. Each CSV row represents one physical GPU.

    Unified-memory parts (e.g. GB10) report ``[N/A]`` for ``memory.total``; we
    mark them with ``shared=True`` and structured unified-memory metadata.
    """
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.total,uuid,name",
            "--format=csv,noheader,nounits",
        ]
    )
    if not out:
        out = _run(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.total,name",
                "--format=csv,noheader,nounits",
            ]
        )
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
    gpus = _cuda_visible_devices(gpus, _parse_nvidia_mig_devices(_run(["nvidia-smi", "-L"])))
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
        "render_capable": True,
        "rtx_capable": False,
        "memory": _unified_memory(memory_mb),
    }
    if memory_mb:
        gpu["vram_mb"] = memory_mb
    result = _legacy_gpu_summary([gpu])
    result["gpus"] = [gpu]
    return result


def detect_hardware() -> Dict[str, Any]:
    """Best-effort local hardware snapshot for the agent registry. Never raises."""
    architecture = platform.machine()
    host_cpu_count = os.cpu_count() or 0
    cpu_count, cpu_capacity = _effective_cpu_capacity(host_cpu_count)
    cpu_model = _cpu_model()
    host_memory_mb = _memory_mb()
    memory_mb, memory_capacity = _effective_memory_capacity(host_memory_mb)
    disk = _disk_usage_mb()
    info: Dict[str, Any] = {
        "schema": SCHEMA,
        "os": platform.system().lower(),
        "arch": architecture,
        "cpu_arch": architecture,
        "cpu_count": cpu_count,
        "memory_mb": memory_mb,
        "memory_total_mb": memory_mb,
        "memory_gb": memory_mb / 1024 if memory_mb else 0,
        "accelerator": "none",
        "accelerators": [],
        "cpu": {
            "architecture": architecture,
            "logical_cores": cpu_count,
            **({"model": cpu_model} if cpu_model else {}),
        },
        "memory": {"total_mb": memory_mb},
        "capacity": {
            "scope": (
                "sandbox"
                if cpu_count != host_cpu_count
                or memory_mb != host_memory_mb
                or "CUDA_VISIBLE_DEVICES" in os.environ
                else "host"
            ),
            "cpu": cpu_capacity,
            "memory": memory_capacity,
            "accelerators": {
                "effective_count": 0,
                "mig_count": 0,
                "visibility": (
                    "cuda_visible_devices"
                    if "CUDA_VISIBLE_DEVICES" in os.environ
                    else "runtime"
                ),
            },
        },
    }
    if cpu_model:
        info["cpu_model"] = cpu_model
    if disk:
        info["disk"] = disk
        info["disk_total_mb"] = disk["total_mb"]
        info["disk_available_mb"] = disk["available_mb"]
        # ``disk_gb_min`` means usable workspace capacity, not device size.
        info["disk_gb"] = disk["available_mb"] / 1024
    try:
        gpu = detect_nvidia() or detect_apple_metal()
    except Exception:  # noqa: BLE001 - detection must never break registration
        gpu = None
    if gpu:
        if memory_mb != host_memory_mb:
            for item in gpu.get("gpus") or []:
                if isinstance(item, dict) and item.get("shared"):
                    item["memory"] = _unified_memory(memory_mb)
                    if memory_mb:
                        item["vram_mb"] = memory_mb
                    else:
                        item.pop("vram_mb", None)
            if gpu.get("shared"):
                gpu["memory"] = _unified_memory(memory_mb)
                if memory_mb:
                    gpu["vram_mb"] = memory_mb
                else:
                    gpu.pop("vram_mb", None)
        info["gpu"] = gpu
        if isinstance(gpu.get("gpus"), list):
            info["gpus"] = [
                item for item in gpu["gpus"] if isinstance(item, dict)
            ]
        info["accelerator"] = gpu.get("accelerator", "none")
        groups: Dict[tuple[str, str, int], Dict[str, Any]] = {}
        vendor = "nvidia" if gpu.get("accelerator") == "cuda" else "apple"
        for item in info.get("gpus") or []:
            model = str(item.get("name") or "GPU")
            memory_mb_value = _gpu_capacity_mb(item)
            key = (vendor, model, memory_mb_value)
            candidate = groups.setdefault(
                key,
                {
                    "kind": "gpu",
                    "vendor": vendor,
                    "model": model,
                    "memory_gb": memory_mb_value / 1024,
                    "count": 0,
                    "render_capable": bool(item.get("render_capable")),
                    "rtx_capable": bool(item.get("rtx_capable")),
                },
            )
            candidate["count"] += 1
        info["accelerators"] = list(groups.values())
        info["capacity"]["accelerators"] = {
            "effective_count": len(info.get("gpus") or []),
            "mig_count": sum(
                1 for item in info.get("gpus") or [] if item.get("flavor") == "mig"
            ),
            "visibility": (
                "cuda_visible_devices"
                if "CUDA_VISIBLE_DEVICES" in os.environ
                else "runtime"
            ),
        }
        if info["capacity"]["accelerators"]["mig_count"]:
            info["capacity"]["scope"] = "sandbox"
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
