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


_FIXTURE_RTX5090 = "0, 32576, NVIDIA GeForce RTX 5090"
_FIXTURE_RTX_PRO_6000 = "0, 98304, NVIDIA RTX PRO 6000 Blackwell"
_FIXTURE_GB10 = "0, [N/A], NVIDIA GB10"
_FIXTURE_RTX_PRO_6000_X2 = (
    "0, 98304, NVIDIA RTX PRO 6000 Blackwell\n"
    "1, 98304, NVIDIA RTX PRO 6000 Blackwell"
)


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
            "shared": True,
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
        "shared": True,
        "memory": {"type": "unified", "shared_mb": 65536},
        "vram_mb": 65536,
        "count": 1,
        "gpus": [
            {
                "index": 0,
                "accelerator": "metal",
                "name": "Apple M4 Pro",
                "shared": True,
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
                "memory": {"type": "unified", "shared_mb": 122000},
            }
        ],
    }
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hw.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(hw.os, "cpu_count", lambda: 20)
    monkeypatch.setattr(hw, "_memory_mb", lambda: 122000)
    monkeypatch.setattr(hw, "detect_nvidia", lambda: detected_gpu)

    info = hw.detect_hardware()

    assert info["accelerator"] == "cuda"
    assert info["gpu"]["name"] == "NVIDIA GB10"
    assert info["gpus"] == detected_gpu["gpus"]
    assert info["cpu_count"] == 20
    assert info["memory_mb"] == 122000
    assert info["arch"] == "aarch64"


def test_detect_hardware_no_accelerator(monkeypatch):
    monkeypatch.setattr(hw, "detect_nvidia", lambda: None)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)

    info = hw.detect_hardware()

    assert info["accelerator"] == "none"
    assert "gpu" not in info
    assert "gpus" not in info


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

    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")

    def fail_open(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("builtins.open", fail_open)
    assert hw._memory_mb() == 0

    monkeypatch.setattr(hw.platform, "system", lambda: "Plan9")
    assert hw._memory_mb() == 0


def test_is_unified_memory_gpu():
    assert hw._is_unified_memory_gpu("NVIDIA GB10") is True
    assert hw._is_unified_memory_gpu("nvidia gb10") is True
    assert hw._is_unified_memory_gpu("NVIDIA GeForce RTX 5090") is False
    assert hw._is_unified_memory_gpu("NVIDIA RTX PRO 6000 Blackwell") is False
