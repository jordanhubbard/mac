"""First-class hardware self-reporting: detection (best-effort, never raises)
and summary. Subprocess probes are monkeypatched so tests are host-independent.

Fixture outputs match real nvidia-smi --query-gpu=index,memory.total,name
--format=csv,noheader,nounits responses for:
  - RTX 5090 (discrete, 32 GB VRAM)
  - RTX PRO 6000 Blackwell (discrete, 96 GB VRAM)
  - GB10 (unified memory, [N/A] for memory.total)
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import mac.hardware as hw
import pytest
from mac.roles_service import machine_hardware_satisfies


_FIXTURE_RTX5090 = "0, 32576, NVIDIA GeForce RTX 5090"
_FIXTURE_RTX_PRO_6000 = "0, 98304, NVIDIA RTX PRO 6000 Blackwell"
_FIXTURE_GB10 = "0, [N/A], NVIDIA GB10"
_FIXTURE_RTX_PRO_6000_X2 = (
    "0, 98304, NVIDIA RTX PRO 6000 Blackwell\n"
    "1, 98304, NVIDIA RTX PRO 6000 Blackwell"
)


@pytest.fixture(autouse=True)
def _clear_cuda_visibility(monkeypatch):
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)


def test_cpu_model_reports_darwin_brand(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hw, "_run", lambda _cmd: "Apple M4 Pro")

    assert hw._cpu_model() == "Apple M4 Pro"

    monkeypatch.setattr(hw, "_run", lambda _cmd: None)
    monkeypatch.setattr(hw.platform, "processor", lambda: "Darwin fallback")
    assert hw._cpu_model() == "Darwin fallback"


def test_cpu_model_parses_linux_cpuinfo(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    cpuinfo = "processor: 0\nHardware: NVIDIA Grace\nHardware: ignored\n"
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: io.StringIO(cpuinfo))

    assert hw._cpu_model() == "NVIDIA Grace"

    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.StringIO("processor: 0\n"),
    )
    monkeypatch.setattr(hw.platform, "processor", lambda: "Linux fallback")
    assert hw._cpu_model() == "Linux fallback"


def test_cpu_model_falls_back_and_never_raises(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")

    def fail_open(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("builtins.open", fail_open)
    monkeypatch.setattr(hw.platform, "processor", lambda: "Fallback CPU")
    assert hw._cpu_model() == "Fallback CPU"

    def fail_processor():
        raise RuntimeError("unavailable")

    monkeypatch.setattr(hw.platform, "processor", fail_processor)
    assert hw._cpu_model() == ""


def test_disk_usage_uses_workspace_capacity_and_never_raises(monkeypatch):
    monkeypatch.setenv("MAC_WORKER_WORKSPACE", "/missing/workspace")
    monkeypatch.setattr(hw.Path, "exists", lambda _path: False)
    monkeypatch.setattr(
        hw.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(
            total=10 * 1024 * 1024,
            free=4 * 1024 * 1024,
        ),
    )
    assert hw._disk_usage_mb() == {"total_mb": 10, "available_mb": 4}

    def fail_usage(_path):
        raise OSError("unavailable")

    monkeypatch.setattr(hw.shutil, "disk_usage", fail_usage)
    assert hw._disk_usage_mb() == {}


def test_detect_nvidia_single_gpu_reports_structured_discrete_memory(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_RTX5090)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["accelerator"] == "cuda"
    assert gpu["name"] == "NVIDIA GeForce RTX 5090"
    assert gpu["vram_mb"] == 32576
    assert gpu["count"] == 1
    assert gpu.get("shared") is not True
    assert gpu["memory"] == {"type": "dedicated", "vram_mb": 32576}
    assert gpu["gpus"] == [
        {
            "index": 0,
            "accelerator": "cuda",
            "name": "NVIDIA GeForce RTX 5090",
            "flavor": "pcie",
            "render_capable": True,
            "rtx_capable": True,
            "vram_mb": 32576,
            "memory": {"type": "dedicated", "vram_mb": 32576},
        }
    ]


def test_detect_nvidia_rtx_pro_6000_reports_nonzero_measured_vram(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_RTX_PRO_6000)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["name"] == "NVIDIA RTX PRO 6000 Blackwell"
    assert gpu["vram_mb"] == 98304
    assert gpu["memory"] == {"type": "dedicated", "vram_mb": 98304}
    assert gpu["gpus"][0]["vram_mb"] == 98304


def test_detect_nvidia_multi_gpu_reports_every_gpu(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_RTX_PRO_6000_X2)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["count"] == 2
    assert gpu["name"] == "NVIDIA RTX PRO 6000 Blackwell"
    assert gpu["vram_mb"] == 98304
    assert gpu.get("shared") is not True
    assert [g["index"] for g in gpu["gpus"]] == [0, 1]
    assert [g["name"] for g in gpu["gpus"]] == [
        "NVIDIA RTX PRO 6000 Blackwell",
        "NVIDIA RTX PRO 6000 Blackwell",
    ]
    assert [g["memory"] for g in gpu["gpus"]] == [
        {"type": "dedicated", "vram_mb": 98304},
        {"type": "dedicated", "vram_mb": 98304},
    ]


def test_detect_nvidia_unified_memory_reports_shared_capacity(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_GB10)
    monkeypatch.setattr(hw, "_memory_mb", lambda: 131072)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["name"] == "NVIDIA GB10"
    assert gpu["vram_mb"] == 131072
    assert gpu["shared"] is True
    assert gpu["count"] == 1
    assert gpu["accelerator"] == "cuda"
    assert gpu["memory"] == {"type": "unified", "shared_mb": 131072}
    assert gpu["gpus"] == [
        {
            "index": 0,
            "accelerator": "cuda",
            "name": "NVIDIA GB10",
            "flavor": "unified",
            "shared": True,
            "render_capable": True,
            "rtx_capable": False,
            "memory": {"type": "unified", "shared_mb": 131072},
            "vram_mb": 131072,
        }
    ]


def test_detect_nvidia_keeps_old_memory_name_output_readable(monkeypatch):
    monkeypatch.setattr(
        hw,
        "_run",
        lambda cmd, timeout=5.0: "49140 MiB, NVIDIA RTX 6000 Ada\n49140, NVIDIA RTX 6000 Ada",
    )

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["count"] == 2
    assert gpu["vram_mb"] == 49140
    assert [g["index"] for g in gpu["gpus"]] == [0, 1]
    assert all(g["memory"] == {"type": "dedicated", "vram_mb": 49140} for g in gpu["gpus"])


def test_detect_nvidia_absent_or_unusable_returns_none(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: None)
    assert hw.detect_nvidia() is None

    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "not enough fields")
    assert hw.detect_nvidia() is None


def test_detect_nvidia_invalid_memory_is_unknown_not_shared(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "gpu0, unknown, NVIDIA Mystery")

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["index"] == 0
    assert gpu["memory"] == {"type": "unknown"}
    assert "vram_mb" not in gpu
    assert gpu.get("shared") is not True


def test_detect_nvidia_unavailable_probe_on_non_unified_gpu_is_unknown_not_shared(monkeypatch):
    """A discrete GPU that reports [N/A] for memory.total (e.g. when nvidia-smi
    cannot query VRAM on this host) must be tagged as memory.type=unknown, not
    fabricated as unified/shared by borrowing system RAM.  The GB10 unified-memory
    heuristic must not apply to unrelated parts (e.g. RTX A6000) just because
    their probe returned [N/A]."""
    # Simulate a discrete GPU (e.g. RTX A6000) where nvidia-smi returns [N/A].
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "0, [N/A], NVIDIA RTX A6000")
    monkeypatch.setattr(hw, "_memory_mb", lambda: 131072)  # system RAM must NOT appear

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["name"] == "NVIDIA RTX A6000"
    assert gpu["accelerator"] == "cuda"
    assert gpu["memory"] == {"type": "unknown"}
    assert "vram_mb" not in gpu
    assert gpu.get("shared") is not True


def test_detect_nvidia_unavailable_probe_on_unified_gpu_stays_shared(monkeypatch):
    """A known unified-memory GPU (GB10) that reports [N/A] is still correctly
    classified as shared/unified — unified is a property of the *part*, not just
    the probe output."""
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "0, [N/A], NVIDIA GB10")
    monkeypatch.setattr(hw, "_memory_mb", lambda: 131072)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["name"] == "NVIDIA GB10"
    assert gpu["shared"] is True
    assert gpu["memory"] == {"type": "unified", "shared_mb": 131072}
    assert gpu["vram_mb"] == 131072


def test_detect_apple_metal_reports_unified_memory(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hw.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "Apple M4 Pro")
    monkeypatch.setattr(hw, "_memory_mb", lambda: 65536)

    assert hw.detect_apple_metal() == {
        "index": 0,
        "accelerator": "metal",
        "name": "Apple M4 Pro",
        "flavor": "unified",
        "shared": True,
        "render_capable": True,
        "rtx_capable": False,
        "memory": {"type": "unified", "shared_mb": 65536},
        "vram_mb": 65536,
        "count": 1,
        "gpus": [
            {
                "index": 0,
                "accelerator": "metal",
                "name": "Apple M4 Pro",
                "flavor": "unified",
                "shared": True,
                "render_capable": True,
                "rtx_capable": False,
                "memory": {"type": "unified", "shared_mb": 65536},
                "vram_mb": 65536,
            }
        ],
    }

    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    assert hw.detect_apple_metal() is None


def test_detect_hardware_composes_and_never_raises(monkeypatch):
    detected_gpu = {
        "accelerator": "cuda",
        "name": "NVIDIA GB10",
        "vram_mb": 122000,
        "shared": True,
        "count": 1,
        "gpus": [
            {
                "index": 0,
                "accelerator": "cuda",
                "name": "NVIDIA GB10",
                "vram_mb": 122000,
                "shared": True,
                "render_capable": True,
                "rtx_capable": False,
                "memory": {"type": "unified", "shared_mb": 122000},
            }
        ],
    }
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hw.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(hw.os, "cpu_count", lambda: 20)
    monkeypatch.setattr(hw, "_memory_mb", lambda: 122000)
    monkeypatch.setattr(
        hw,
        "_effective_cpu_capacity",
        lambda count: (count, {"host_count": count, "effective_count": count}),
    )
    monkeypatch.setattr(
        hw,
        "_effective_memory_capacity",
        lambda total: (
            total,
            {"host_total_mb": total, "effective_total_mb": total},
        ),
    )
    monkeypatch.setattr(hw, "_cpu_model", lambda: "NVIDIA Grace")
    monkeypatch.setattr(
        hw,
        "_disk_usage_mb",
        lambda: {"total_mb": 1048576, "available_mb": 524288},
    )
    monkeypatch.setattr(hw, "detect_nvidia", lambda: detected_gpu)

    info = hw.detect_hardware()

    assert info["accelerator"] == "cuda"
    assert info["gpu"]["name"] == "NVIDIA GB10"
    assert info["gpus"] == detected_gpu["gpus"]
    assert info["cpu_count"] == 20
    assert info["memory_mb"] == 122000
    assert info["arch"] == "aarch64"
    assert info["cpu_arch"] == "aarch64"
    assert info["cpu_model"] == "NVIDIA Grace"
    assert info["cpu"] == {
        "architecture": "aarch64",
        "logical_cores": 20,
        "model": "NVIDIA Grace",
    }
    assert info["memory_total_mb"] == 122000
    assert info["memory_gb"] == 122000 / 1024
    assert info["disk"] == {"total_mb": 1048576, "available_mb": 524288}
    assert info["disk_gb"] == 512
    assert info["accelerators"] == [
        {
            "kind": "gpu",
            "vendor": "nvidia",
            "model": "NVIDIA GB10",
            "memory_gb": 122000 / 1024,
            "count": 1,
            "render_capable": True,
            "rtx_capable": False,
        }
    ]


def test_detect_hardware_no_accelerator(monkeypatch):
    monkeypatch.setattr(hw, "detect_nvidia", lambda: None)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)
    monkeypatch.setattr(hw, "_cpu_model", lambda: "")
    monkeypatch.setattr(hw, "_disk_usage_mb", lambda: {})

    info = hw.detect_hardware()

    assert info["accelerator"] == "none"
    assert "gpu" not in info
    assert "gpus" not in info
    assert info["accelerators"] == []
    assert "cpu_model" not in info
    assert "disk" not in info


def test_detect_hardware_ignores_malformed_gpu_inventory(monkeypatch):
    monkeypatch.setattr(
        hw,
        "detect_nvidia",
        lambda: {"accelerator": "cuda", "gpus": ["invalid"]},
    )

    info = hw.detect_hardware()

    assert info["gpus"] == []
    assert info["accelerators"] == []

    monkeypatch.setattr(hw, "detect_nvidia", lambda: {"accelerator": "metal"})
    info = hw.detect_hardware()
    assert "gpus" not in info
    assert info["accelerators"] == []


def test_detect_hardware_survives_probe_exception(monkeypatch):
    def _boom():
        raise RuntimeError("nvidia-smi exploded")

    monkeypatch.setattr(hw, "detect_nvidia", _boom)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)

    info = hw.detect_hardware()

    assert info["accelerator"] == "none"


def test_summarize():
    assert "NVIDIA GB10" in hw.summarize(
        {
            "accelerator": "cuda",
            "gpu": {"name": "NVIDIA GB10", "count": 1},
            "os": "linux",
            "arch": "aarch64",
            "cpu_count": 20,
        }
    )
    assert hw.summarize(None) == "(no hardware reported)"
    assert "accelerator=none" in hw.summarize({"accelerator": "none", "os": "linux", "arch": "x86_64"})
    assert "16GB" in hw.summarize({"accelerator": "none", "os": "linux", "arch": "x86_64", "memory_mb": 16384})


def test_summarize_shared_gpu_uses_structured_memory():
    hw_info = {
        "accelerator": "cuda",
        "os": "linux",
        "arch": "aarch64",
        "cpu_count": 20,
        "memory_mb": 131072,
        "gpu": {
            "accelerator": "cuda",
            "name": "NVIDIA GB10",
            "shared": True,
            "count": 1,
            "memory": {"type": "unified", "shared_mb": 131072},
        },
    }

    summary = hw.summarize(hw_info)

    assert "GB10" in summary
    assert "128GB shared" in summary


def test_probe_and_memory_edges(monkeypatch):
    monkeypatch.setattr(
        hw.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="  ok\n"),
    )
    assert hw._run(["probe"]) == "ok"

    monkeypatch.setattr(
        hw.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="ignored"),
    )
    assert hw._run(["missing-probe"]) is None

    def raise_probe(*_args, **_kwargs):
        raise OSError("missing")

    monkeypatch.setattr(hw.subprocess, "run", raise_probe)
    assert hw._run(["missing-probe"]) is None

    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        "builtins.open",
        lambda *_a, **_k: io.StringIO("MemTotal:       2097152 kB\n"),
    )
    assert hw._memory_mb() == 2048


def test_memory_probe_darwin_and_fallbacks(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: str(32 * 1024 * 1024 * 1024))
    assert hw._memory_mb() == 32768
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: None)
    assert hw._memory_mb() == 0

    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: io.StringIO("Other: 1024 kB\n"),
    )
    assert hw._memory_mb() == 0

    def fail_open(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("builtins.open", fail_open)
    assert hw._memory_mb() == 0

    monkeypatch.setattr(hw.platform, "system", lambda: "Plan9")
    assert hw._memory_mb() == 0


def test_effective_capacity_uses_cgroup_v2_affinity_cpuset_and_quota(monkeypatch):
    values = {
        str(hw._CGROUP_ROOT / "cpuset.cpus.effective"): "0-3,8",
        str(hw._CGROUP_ROOT / "cpu.max"): "150000 100000",
        str(hw._CGROUP_ROOT / "memory.max"): str(4 * 1024 * 1024 * 1024),
    }
    monkeypatch.setattr(hw, "_read_text", lambda path: values.get(str(path)))
    monkeypatch.setattr(
        hw.os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False
    )

    cpu_count, cpu = hw._effective_cpu_capacity(32)
    memory_mb, memory = hw._effective_memory_capacity(65536)

    assert cpu_count == 1.5
    assert cpu == {
        "host_count": 32,
        "affinity_count": 8,
        "cpuset_count": 5,
        "quota_cores": 1.5,
        "effective_count": 1.5,
    }
    assert memory_mb == 4096
    assert memory == {
        "host_total_mb": 65536,
        "limit_mb": 4096,
        "effective_total_mb": 4096,
    }


def test_effective_capacity_supports_cgroup_v1(monkeypatch):
    values = {
        str(hw._CGROUP_ROOT / "cpuset" / "cpuset.cpus"): "0-7",
        str(hw._CGROUP_ROOT / "cpu" / "cpu.cfs_quota_us"): "400000",
        str(hw._CGROUP_ROOT / "cpu" / "cpu.cfs_period_us"): "100000",
        str(hw._CGROUP_ROOT / "memory" / "memory.limit_in_bytes"): str(
            8 * 1024 * 1024 * 1024
        ),
    }
    monkeypatch.setattr(hw, "_read_text", lambda path: values.get(str(path)))
    monkeypatch.setattr(
        hw.os, "sched_getaffinity", lambda _pid: set(range(16)), raising=False
    )

    cpu_count, cpu = hw._effective_cpu_capacity(32)
    memory_mb, memory = hw._effective_memory_capacity(65536)

    assert cpu_count == 4
    assert cpu["cpuset_count"] == 8
    assert cpu["quota_cores"] == 4
    assert memory_mb == 8192
    assert memory["limit_mb"] == 8192


def test_detected_effective_capacity_is_what_scheduler_matches(monkeypatch):
    monkeypatch.setattr(hw.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(
        hw,
        "_effective_cpu_capacity",
        lambda _count: (
            2,
            {"host_count": 64, "quota_cores": 2, "effective_count": 2},
        ),
    )
    monkeypatch.setattr(hw, "_memory_mb", lambda: 262144)
    monkeypatch.setattr(
        hw,
        "_effective_memory_capacity",
        lambda _total: (
            4096,
            {
                "host_total_mb": 262144,
                "limit_mb": 4096,
                "effective_total_mb": 4096,
            },
        ),
    )
    monkeypatch.setattr(hw, "detect_nvidia", lambda: None)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)

    info = hw.detect_hardware()
    ok, reasons = machine_hardware_satisfies(
        {"cpu_count_min": 4, "memory_gb_min": 8},
        info,
    )

    assert info["cpu_count"] == 2
    assert info["memory_mb"] == 4096
    assert info["memory_gb"] == 4
    assert info["capacity"]["scope"] == "sandbox"
    assert ok is False
    assert "cpu_count=2 < required 4.0" in reasons
    assert "memory_gb=4.0 < required 8.0" in reasons


def test_cuda_visible_devices_filters_scheduler_gpu_inventory(monkeypatch):
    physical = [
        {"index": 0, "uuid": "GPU-zero"},
        {"index": 1, "uuid": "GPU-one"},
    ]
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    assert hw._cuda_visible_devices(physical, []) == [physical[1]]

    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    assert hw._cuda_visible_devices(physical, []) == []


def test_mig_visibility_reports_slice_capacity_instead_of_parent(monkeypatch):
    listing = """\
GPU 0: NVIDIA A100-SXM4-40GB (UUID: GPU-parent)
  MIG 1g.10gb Device 0: (UUID: MIG-one)
  MIG 2g.20gb Device 1: (UUID: MIG-two)
"""
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "MIG-two")

    def fake_run(command, timeout=5.0):
        if command[-1] == "-L":
            return listing
        return "0, 40960, GPU-parent, NVIDIA A100-SXM4-40GB"

    monkeypatch.setattr(hw, "_run", fake_run)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["count"] == 1
    assert gpu["gpus"][0]["uuid"] == "MIG-two"
    assert gpu["gpus"][0]["mig_profile"] == "2g.20gb"
    assert gpu["gpus"][0]["vram_mb"] == 20 * 1024
    assert gpu["gpus"][0]["render_capable"] is False
    assert hw._confinement_topology(gpu["gpus"]) == {
        "kind": "mig-slices",
        "gpus": 1,
        "flavors": ["mig"],
    }

    info = hw.detect_hardware()
    ok, reasons = machine_hardware_satisfies(
        {
            "accelerators": [
                {"kind": "gpu", "vendor": "nvidia", "memory_gb_min": 30}
            ]
        },
        info,
    )
    assert info["accelerators"][0]["memory_gb"] == 20
    assert info["capacity"]["accelerators"]["effective_count"] == 1
    assert info["capacity"]["accelerators"]["mig_count"] == 1
    assert info["capacity"]["scope"] == "sandbox"
    assert ok is False
    assert reasons and reasons[0].startswith("no accelerator matches")


def test_zero_capacity_hardware_is_reported_without_fabrication(monkeypatch):
    assert hw._unified_memory(0) == {"type": "unified"}
    monkeypatch.setattr(hw, "_memory_mb", lambda: 0)
    gpu = hw._parse_nvidia_gpu_row("0, [N/A], NVIDIA GB10", 0)
    assert gpu is not None
    assert "vram_mb" not in gpu

    monkeypatch.setattr(hw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hw.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(hw, "_run", lambda _cmd: "Apple Silicon")
    apple = hw.detect_apple_metal()
    assert apple is not None
    assert "vram_mb" not in apple["gpus"][0]


def test_is_unified_memory_gpu():
    assert hw._is_unified_memory_gpu("NVIDIA GB10") is True
    assert hw._is_unified_memory_gpu("nvidia gb10") is True
    assert hw._is_unified_memory_gpu("NVIDIA GeForce RTX 5090") is False
    assert hw._is_unified_memory_gpu("NVIDIA RTX PRO 6000 Blackwell") is False


# --- HGX flavor + confinement topology (fungible onboarding) --------------

_FIXTURE_H100_HGX = "0, 81559, NVIDIA H100 80GB HGX"
_FIXTURE_H100_HGX_X8 = "\n".join(
    "%d, 81559, NVIDIA H100 80GB HGX" % index for index in range(8)
)
_FIXTURE_H100_PCIE = "0, 81559, NVIDIA H100 PCIe"
_FIXTURE_A100_SXM = "0, 81920, NVIDIA A100-SXM4-80GB"


def test_gpu_flavor_classifies_hgx_pcie_and_unified():
    assert hw._gpu_flavor("NVIDIA H100 80GB HGX", unified=False) == "hgx"
    assert hw._gpu_flavor("NVIDIA A100-SXM4-80GB", unified=False) == "hgx"
    assert hw._gpu_flavor("NVIDIA H200", unified=False) == "hgx"
    assert hw._gpu_flavor("NVIDIA H100 PCIe", unified=False) == "pcie"
    assert hw._gpu_flavor("NVIDIA GeForce RTX 5090", unified=False) == "pcie"
    # Unified parts (GB10, Apple) are their own flavor regardless of the name.
    assert hw._gpu_flavor("NVIDIA GB10", unified=True) == "unified"


def test_detect_nvidia_carries_hgx_flavor(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_H100_HGX)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["flavor"] == "hgx"
    assert gpu["gpus"][0]["flavor"] == "hgx"


def test_detect_nvidia_pcie_h100_is_not_hgx(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_H100_PCIE)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["flavor"] == "pcie"
    assert gpu["gpus"][0]["flavor"] == "pcie"


def test_detect_nvidia_sxm_family_is_hgx(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_A100_SXM)

    gpu = hw.detect_nvidia()

    assert gpu is not None
    assert gpu["flavor"] == "hgx"


def test_confinement_topology_hgx_baseboard():
    gpus = [
        {"index": i, "accelerator": "cuda", "name": "NVIDIA H100 80GB HGX", "flavor": "hgx"}
        for i in range(8)
    ]
    topology = hw._confinement_topology(gpus)
    assert topology == {"kind": "hgx-baseboard", "gpus": 8, "flavors": ["hgx"]}


def test_confinement_topology_discrete_and_unified_and_none():
    discrete = hw._confinement_topology(
        [{"index": 0, "flavor": "pcie"}]
    )
    assert discrete == {"kind": "discrete", "gpus": 1, "flavors": ["pcie"]}

    unified = hw._confinement_topology(
        [{"index": 0, "flavor": "unified", "shared": True}]
    )
    assert unified == {"kind": "unified", "gpus": 1, "flavors": ["unified"]}

    assert hw._confinement_topology([]) == {"kind": "none", "gpus": 0}


def test_detect_hardware_carries_topology_and_flavor_through_onboarding(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hw.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(hw.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(hw, "_memory_mb", lambda: 1048576)
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_H100_HGX_X8)

    info = hw.detect_hardware()

    assert info["accelerator"] == "cuda"
    assert info["topology"] == {"kind": "hgx-baseboard", "gpus": 8, "flavors": ["hgx"]}
    assert info["flavor"] == "hgx"
    assert all(gpu["flavor"] == "hgx" for gpu in info["gpus"])


def test_detect_hardware_no_accelerator_reports_none_topology(monkeypatch):
    monkeypatch.setattr(hw, "detect_nvidia", lambda: None)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)

    info = hw.detect_hardware()

    assert info["topology"] == {"kind": "none", "gpus": 0}
    assert "flavor" not in info


def test_summarize_surfaces_hgx_baseboard():
    summary = hw.summarize(
        {
            "accelerator": "cuda",
            "os": "linux",
            "arch": "x86_64",
            "cpu_count": 128,
            "memory_mb": 1048576,
            "flavor": "hgx",
            "topology": {"kind": "hgx-baseboard", "gpus": 8, "flavors": ["hgx"]},
            "gpu": {
                "accelerator": "cuda",
                "name": "NVIDIA H100 80GB HGX",
                "count": 8,
                "vram_mb": 81559,
                "memory": {"type": "dedicated", "vram_mb": 81559},
            },
        }
    )
    assert "hgx-baseboard" in summary
    assert "NVIDIA H100 80GB HGX x8" in summary
