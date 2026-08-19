"""First-class hardware self-reporting.

Agents detect their local accelerator + CPU/memory and report it into the
registry (``resources["hardware"]``) at registration, so the fleet OWNS a
hardware inventory instead of relying on hand-maintained notes (which drift and
get an agent's silicon wrong). The hub can then derive compute/gen capability
from facts — e.g. "which agent has a CUDA GPU" — and operators query it with
``mac agent hardware``.

The snapshot stays on ``mac.hardware.v1`` as an ADDITIVE change: every
pre-existing top-level key (``cpu_count``, ``memory_mb``, ``memory_gb``,
``disk_gb``, ``gpu``, ``gpus``, ``accelerators``, ``capacity``, ``topology``,
``flavor``, …) keeps its historical meaning so CLI, roles, allocator, catalog,
and media-routing consumers stay byte-compatible. Two new blocks sit beside
those keys and must be read independently:

* ``effective_allocation`` (``mac.effective_allocation.v1``) is measured from
  this process's own execution boundary (cgroup v2 ``cpu.max`` /
  ``cpuset.cpus.effective`` / ``memory.max``, cgroup v1 quota/limit fallbacks,
  workspace filesystem, MIG + ``CUDA_VISIBLE_DEVICES``). Each dimension is
  ``{value, known, source}``. Unresolved probes, cgroup ``max``, missing
  cgroup files, and non-Linux stay ``known=false, source="unknown"`` — host
  inventory is NEVER substituted. Equality with host inventory is emitted only
  when proven (no cgroup confinement and no accelerator visibility filtering),
  with ``source="host_equals_allocation"``.
* ``host_inventory`` (``mac.host_inventory.v1``) preserves raw provider /
  physical inventory (host logical CPUs, host RAM, parent GPU parts, device
  disk totals). Allocation data never overwrites it.

Probe roots are injectable on ``detect_hardware`` (cgroup root, workspace path,
nvidia query/list output) so fixture trees can drive detection. The default
cgroup root is the module-level ``_CGROUP_ROOT``, resolved at call time, so
existing ``hw._CGROUP_ROOT`` monkeypatches still work.

Pure stdlib + best-effort subprocess probes. Detection NEVER raises: a probe
failure yields ``accelerator: "none"`` / omitted fields / unknown allocation
dimensions, never a failed registration. The snapshot shape::

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
from typing import Any, Callable, Dict, Optional, Union

SCHEMA = "mac.hardware.v1"
EFFECTIVE_ALLOCATION_SCHEMA = "mac.effective_allocation.v1"
HOST_INVENTORY_SCHEMA = "mac.host_inventory.v1"

_ProbeText = Union[str, Callable[[], Optional[str]], None]

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


def _effective_cpu_capacity(
    host_count: int, cgroup_root: Optional[os.PathLike] = None
) -> tuple[float | int, Dict[str, Any]]:
    """Return CPU capacity visible to this process, including cgroup limits.

    Legacy helper used by top-level ``cpu_count`` / ``capacity.cpu``. It still
    takes ``min(host, confinement)`` so historical consumers keep their shape.
    ``effective_allocation`` does **not** use this helper — it refuses to
    substitute host inventory when confinement is unknown.
    """
    root = Path(cgroup_root) if cgroup_root is not None else _CGROUP_ROOT
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
                root / "cpuset.cpus.effective",
                root / "cpuset.cpus",
                root / "cpuset" / "cpuset.cpus",
            ]
        )
    )
    if cpuset_count:
        details["cpuset_count"] = cpuset_count
        candidates.append(float(cpuset_count))

    quota_cores: Optional[float] = None
    cpu_max = _read_text(root / "cpu.max")
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
        quota_text = _read_text(root / "cpu" / "cpu.cfs_quota_us")
        period_text = _read_text(root / "cpu" / "cpu.cfs_period_us")
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


def _effective_memory_capacity(
    host_mb: int, cgroup_root: Optional[os.PathLike] = None
) -> tuple[int, Dict[str, Any]]:
    """Return memory usable by this process, bounded by cgroup v1/v2.

    Legacy helper for top-level ``memory_mb`` / ``capacity.memory``; still mins
    against host total. ``effective_allocation.memory_mb`` does not.
    """
    root = Path(cgroup_root) if cgroup_root is not None else _CGROUP_ROOT
    details: Dict[str, Any] = {"host_total_mb": host_mb}
    raw_limit = _first_text(
        [
            root / "memory.max",
            root / "memory" / "memory.limit_in_bytes",
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


def _allocation_dim(
    *,
    value: Any = None,
    known: bool = False,
    source: str = "unknown",
    unit: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Normalize one effective_allocation dimension."""
    dim: Dict[str, Any] = {
        "value": value if known else None,
        "known": bool(known),
        "source": source if known else "unknown",
    }
    if unit:
        dim["unit"] = unit
    if detail:
        dim["detail"] = detail
    if extra:
        dim.update(extra)
    return dim


def _normalize_cores(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _cpu_quota_from_root(root: Path) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Return (quota_cores, source, raw_text).

    ``quota_cores`` is None when the controller is missing, unreadable, or
    unlimited (``max`` / negative v1 quota). ``raw_text`` is the cpu.max or
    v1 quota file contents when present.
    """
    cpu_max = _read_text(root / "cpu.max")
    if cpu_max is not None:
        fields = cpu_max.split()
        if fields and fields[0] == "max":
            return None, "cgroup_v2_cpu_max", cpu_max
        if len(fields) >= 2:
            try:
                quota = float(fields[0])
                period = float(fields[1])
                if quota >= 0 and period > 0:
                    return quota / period, "cgroup_v2_cpu_max", cpu_max
            except ValueError:
                pass
        return None, "cgroup_v2_cpu_max", cpu_max

    quota_text = _read_text(root / "cpu" / "cpu.cfs_quota_us")
    period_text = _read_text(root / "cpu" / "cpu.cfs_period_us")
    if quota_text is None and period_text is None:
        return None, None, None
    try:
        quota = float(quota_text) if quota_text is not None else -1
        period = float(period_text) if period_text is not None else 0
        raw = "%s %s" % (quota_text, period_text)
        if quota < 0 or period <= 0:
            return None, "cgroup_v1", raw
        return quota / period, "cgroup_v1", raw
    except ValueError:
        return None, "cgroup_v1", quota_text


def _memory_limit_from_root(root: Path) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Return (limit_mb, source, raw_text). None limit means missing or unlimited."""
    v2 = _read_text(root / "memory.max")
    if v2 is not None:
        if v2 == "max":
            return None, "cgroup_v2_memory_max", v2
        try:
            limit_bytes = int(v2)
            if 0 <= limit_bytes < (1 << 60):
                return int(limit_bytes / _MIB), "cgroup_v2_memory_max", v2
            return None, "cgroup_v2_memory_max", v2
        except ValueError:
            return None, "cgroup_v2_memory_max", v2

    v1 = _read_text(root / "memory" / "memory.limit_in_bytes")
    if v1 is None:
        return None, None, None
    if v1 == "max":
        return None, "cgroup_v1", v1
    try:
        limit_bytes = int(v1)
        if 0 <= limit_bytes < (1 << 60):
            return int(limit_bytes / _MIB), "cgroup_v1", v1
        return None, "cgroup_v1", v1
    except ValueError:
        return None, "cgroup_v1", v1


def _affinity_count() -> Optional[int]:
    try:
        count = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return None
    return count or None


def _cuda_visibility_filtered() -> bool:
    raw = os.environ.get("CUDA_VISIBLE_DEVICES")
    if raw is None:
        return False
    return raw.strip().lower() != "all"


def _cpu_allocation(root: Path, host_count: int) -> Dict[str, Any]:
    """CPU cores at this process's cgroup/cpuset/affinity boundary.

    Host inventory is never used as a stand-in when the boundary is unknown.
    """
    if platform.system() != "Linux":
        return _allocation_dim(known=False, source="unknown", unit="cores")

    quota_cores, quota_source, quota_raw = _cpu_quota_from_root(root)
    cpuset_count = _parse_cpuset_count(
        _first_text(
            [
                root / "cpuset.cpus.effective",
                root / "cpuset.cpus",
                root / "cpuset" / "cpuset.cpus",
            ]
        )
    )
    affinity = _affinity_count()
    detail: Dict[str, Any] = {}
    if quota_raw is not None:
        detail["cpu_max"] = quota_raw
    if quota_cores is not None:
        detail["quota_cores"] = quota_cores
    if cpuset_count is not None:
        detail["cpuset_count"] = cpuset_count
    if affinity is not None:
        detail["affinity_count"] = affinity

    has_cgroup = quota_source is not None or cpuset_count is not None
    if not has_cgroup:
        return _allocation_dim(
            known=False,
            source="unknown",
            unit="cores",
            detail={**detail, "reason": "cgroup_unresolved"} if detail else {"reason": "cgroup_unresolved"},
        )

    bounded: Optional[float] = None
    source = "unknown"
    if quota_cores is not None and quota_source:
        bounded = quota_cores
        source = quota_source
    if cpuset_count is not None and (bounded is None or cpuset_count < bounded):
        bounded = float(cpuset_count)
        source = "cpuset"
    if affinity is not None and bounded is not None and affinity < bounded:
        bounded = float(affinity)
        source = "affinity"
    elif affinity is not None and bounded is None and host_count and affinity < host_count:
        bounded = float(affinity)
        source = "affinity"

    if bounded is not None:
        unlimited_quota = quota_cores is None and quota_source is not None
        if (
            unlimited_quota
            and source == "cpuset"
            and host_count > 0
            and cpuset_count == host_count
            and affinity in (None, host_count)
        ):
            return _allocation_dim(
                value=host_count,
                known=True,
                source="host_equals_allocation",
                unit="cores",
                detail=detail,
            )
        return _allocation_dim(
            value=_normalize_cores(bounded),
            known=True,
            source=source,
            unit="cores",
            detail=detail or None,
        )

    return _allocation_dim(
        known=False,
        source="unknown",
        unit="cores",
        detail={**detail, "reason": "cgroup_unlimited"},
    )


def _memory_allocation(root: Path, host_mb: int, cpu_dim: Dict[str, Any]) -> Dict[str, Any]:
    if platform.system() != "Linux":
        return _allocation_dim(known=False, source="unknown", unit="MiB")

    limit_mb, source, raw = _memory_limit_from_root(root)
    detail: Dict[str, Any] = {}
    if raw is not None:
        detail["memory_max"] = raw
    if limit_mb is not None:
        return _allocation_dim(
            value=limit_mb,
            known=True,
            source=source or "cgroup_v1",
            unit="MiB",
            detail=detail or None,
        )
    if source is None:
        return _allocation_dim(
            known=False,
            source="unknown",
            unit="MiB",
            detail={"reason": "cgroup_unresolved"},
        )
    # Unlimited cgroup memory: only equal host when the whole boundary is proven
    # unconfined (cpu allocation already host_equals_allocation).
    if cpu_dim.get("known") and cpu_dim.get("source") == "host_equals_allocation" and host_mb > 0:
        return _allocation_dim(
            value=host_mb,
            known=True,
            source="host_equals_allocation",
            unit="MiB",
            detail=detail or None,
        )
    return _allocation_dim(
        known=False,
        source="unknown",
        unit="MiB",
        detail={**detail, "reason": "cgroup_unlimited"},
    )


def _disk_allocation(disk: Dict[str, int], workspace: Optional[os.PathLike]) -> Dict[str, Any]:
    if workspace is not None:
        try:
            disk = _disk_usage_mb(workspace)
        except TypeError:
            pass
    if disk.get("available_mb") is None and disk.get("total_mb") is None:
        return _allocation_dim(known=False, source="unknown", unit="MiB")
    return _allocation_dim(
        value=disk.get("available_mb"),
        known=True,
        source="workspace_filesystem",
        unit="MiB",
        detail={
            "total_mb": disk.get("total_mb"),
            "available_mb": disk.get("available_mb"),
            **({"workspace": str(workspace)} if workspace is not None else {}),
        },
    )


def _resolve_probe_text(value: _ProbeText, default: Callable[[], Optional[str]]) -> Optional[str]:
    if value is None:
        try:
            return default()
        except Exception:  # noqa: BLE001
            return None
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001
            return None
    return str(value)


def _nvidia_physical_gpus(query_output: Optional[str]) -> list[Dict[str, Any]]:
    rows = [r.strip() for r in (query_output or "").splitlines() if r.strip()]
    return [
        gpu
        for idx, row in enumerate(rows)
        for gpu in [_parse_nvidia_gpu_row(row, idx)]
        if gpu is not None
    ]


def _physical_accelerator_parts(
    physical: list[Dict[str, Any]], visible: list[Dict[str, Any]]
) -> list[Dict[str, Any]]:
    """Parent GPU parts for host_inventory — never MIG slices."""
    if physical:
        return [dict(item) for item in physical]
    parts: list[Dict[str, Any]] = []
    seen: set[Any] = set()
    for item in visible:
        if not isinstance(item, dict):
            continue
        if item.get("flavor") == "mig":
            key = item.get("parent_uuid") or item.get("parent_index")
            if key in seen:
                continue
            seen.add(key)
            name = str(item.get("name") or "")
            if " MIG " in name:
                name = name.split(" MIG ", 1)[0]
            parts.append(
                {
                    "index": item.get("parent_index", 0),
                    "accelerator": item.get("accelerator", "cuda"),
                    "name": name,
                    "uuid": item.get("parent_uuid") or "",
                    "flavor": _gpu_flavor(name, unified=_is_unified_memory_gpu(name)),
                    "render_capable": True,
                }
            )
            continue
        parts.append(dict(item))
    return parts


def _accelerator_allocation(
    visible: list[Dict[str, Any]],
    physical: list[Dict[str, Any]],
    probed: bool,
) -> Dict[str, Any]:
    filtered = _cuda_visibility_filtered()
    mig = [item for item in visible if isinstance(item, dict) and item.get("flavor") == "mig"]
    if not probed and not visible:
        return _allocation_dim(
            known=False,
            source="unknown",
            extra={"devices": []},
        )
    if filtered:
        source = "cuda_visible_devices"
    elif mig:
        source = "nvidia_mig"
    elif (
        physical
        and visible
        and len(visible) == len(physical)
        and not filtered
        and platform.system() == "Linux"
    ):
        source = "host_equals_allocation"
    elif visible:
        source = "nvidia_smi" if any(i.get("accelerator") == "cuda" for i in visible) else "runtime"
    else:
        source = "runtime"
    return _allocation_dim(
        value=len(visible),
        known=True,
        source=source,
        extra={"devices": [dict(item) for item in visible if isinstance(item, dict)]},
    )


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


def _disk_usage_mb(path: Optional[os.PathLike] = None) -> Dict[str, int]:
    """Return total and currently available capacity of the worker filesystem."""
    if path is not None:
        probe = Path(path).expanduser()
        if probe.exists():
            path = probe
        else:
            path = Path.home()
    else:
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


def _nvidia_query_output() -> Optional[str]:
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.total,uuid,name",
            "--format=csv,noheader,nounits",
        ]
    )
    if out:
        return out
    return _run(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.total,name",
            "--format=csv,noheader,nounits",
        ]
    )


def detect_nvidia(
    query_output: Optional[str] = None,
    list_output: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """CUDA GPU via nvidia-smi.  Queries all GPUs in one call.

    Uses ``--query-gpu=index,memory.total,uuid,name`` (name last so it may
    contain commas without ambiguity), with a compatibility fallback for older
    drivers. Each CSV row represents one physical GPU.

    Unified-memory parts (e.g. GB10) report ``[N/A]`` for ``memory.total``; we
    mark them with ``shared=True`` and structured unified-memory metadata.
    """
    out = query_output if query_output is not None else _nvidia_query_output()
    physical = _nvidia_physical_gpus(out)
    if not physical:
        return None
    listing = list_output if list_output is not None else _run(["nvidia-smi", "-L"])
    gpus = _cuda_visible_devices(physical, _parse_nvidia_mig_devices(listing))
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


def detect_hardware(
    cgroup_root: Optional[os.PathLike] = None,
    workspace: Optional[os.PathLike] = None,
    nvidia_query: _ProbeText = None,
    nvidia_list: _ProbeText = None,
) -> Dict[str, Any]:
    """Best-effort local hardware snapshot for the agent registry. Never raises.

    ``cgroup_root``, ``workspace``, and ``nvidia_query`` / ``nvidia_list`` are
    injectable probe roots for tests. Defaults resolve at call time from
    ``_CGROUP_ROOT``, ``MAC_WORKER_WORKSPACE``, and nvidia-smi so existing
    ``hw._CGROUP_ROOT`` monkeypatches still drive detection.
    """
    try:
        return _detect_hardware(
            cgroup_root=cgroup_root,
            workspace=workspace,
            nvidia_query=nvidia_query,
            nvidia_list=nvidia_list,
        )
    except Exception:  # noqa: BLE001 - detection must never break registration
        architecture = platform.machine()
        return {
            "schema": SCHEMA,
            "os": platform.system().lower(),
            "arch": architecture,
            "cpu_arch": architecture,
            "cpu_count": 0,
            "memory_mb": 0,
            "memory_gb": 0,
            "accelerator": "none",
            "accelerators": [],
            "effective_allocation": {
                "schema": EFFECTIVE_ALLOCATION_SCHEMA,
                "cpu": _allocation_dim(known=False, unit="cores"),
                "memory_mb": _allocation_dim(known=False, unit="MiB"),
                "disk_mb": _allocation_dim(known=False, unit="MiB"),
                "accelerators": _allocation_dim(known=False, extra={"devices": []}),
            },
            "host_inventory": {
                "schema": HOST_INVENTORY_SCHEMA,
                "cpu_count": 0,
                "memory_mb": 0,
                "gpus": [],
            },
        }


def _detect_hardware(
    cgroup_root: Optional[os.PathLike],
    workspace: Optional[os.PathLike],
    nvidia_query: _ProbeText,
    nvidia_list: _ProbeText,
) -> Dict[str, Any]:
    root = Path(cgroup_root) if cgroup_root is not None else _CGROUP_ROOT
    architecture = platform.machine()
    host_cpu_count = os.cpu_count() or 0
    try:
        cpu_count, cpu_capacity = _effective_cpu_capacity(
            host_cpu_count, cgroup_root=root
        )
    except TypeError:
        cpu_count, cpu_capacity = _effective_cpu_capacity(host_cpu_count)
    cpu_model = _cpu_model()
    host_memory_mb = _memory_mb()
    try:
        memory_mb, memory_capacity = _effective_memory_capacity(
            host_memory_mb, cgroup_root=root
        )
    except TypeError:
        memory_mb, memory_capacity = _effective_memory_capacity(host_memory_mb)
    try:
        disk = _disk_usage_mb(workspace) if workspace is not None else _disk_usage_mb()
    except TypeError:
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
    query_text = _resolve_probe_text(nvidia_query, _nvidia_query_output)
    list_text = _resolve_probe_text(nvidia_list, lambda: _run(["nvidia-smi", "-L"]))
    physical_gpus = _nvidia_physical_gpus(query_text)
    nvidia_probed = query_text is not None or nvidia_query is not None or nvidia_list is not None
    gpu = None
    try:
        gpu = detect_nvidia(
            query_output=query_text or "",
            list_output=list_text or "",
        )
    except TypeError:
        try:
            gpu = detect_nvidia()
        except Exception:  # noqa: BLE001
            gpu = None
    except Exception:  # noqa: BLE001 - detection must never break registration
        gpu = None
    if not gpu:
        try:
            gpu = detect_apple_metal()
        except Exception:  # noqa: BLE001
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

    cpu_alloc = _cpu_allocation(root, host_cpu_count)
    memory_alloc = _memory_allocation(root, host_memory_mb, cpu_alloc)
    disk_alloc = _disk_allocation(disk, workspace)
    accel_alloc = _accelerator_allocation(gpu_list, physical_gpus, nvidia_probed)
    info["effective_allocation"] = {
        "schema": EFFECTIVE_ALLOCATION_SCHEMA,
        "cpu": cpu_alloc,
        "memory_mb": memory_alloc,
        "disk_mb": disk_alloc,
        "accelerators": accel_alloc,
    }
    host_gpus = _physical_accelerator_parts(physical_gpus, gpu_list)
    host_inventory: Dict[str, Any] = {
        "schema": HOST_INVENTORY_SCHEMA,
        "cpu_count": host_cpu_count,
        "memory_mb": host_memory_mb,
        "gpus": host_gpus,
    }
    if cpu_model:
        host_inventory["cpu_model"] = cpu_model
    if disk.get("total_mb") is not None:
        host_inventory["disk_total_mb"] = disk["total_mb"]
        if disk.get("available_mb") is not None:
            host_inventory["disk_available_mb"] = disk["available_mb"]
    info["host_inventory"] = host_inventory
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
