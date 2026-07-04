"""First-class hardware self-reporting: detection (best-effort, never raises) +
summary. Subprocess probes are monkeypatched so tests are host-independent.

Fixture outputs match real nvidia-smi --query-gpu=memory.total,name
--format=csv,noheader,nounits responses for:
  - RTX 5090 (discrete, 32 GB VRAM)
  - RTX PRO 6000 Blackwell (discrete, 96 GB VRAM)
  - GB10 (unified memory, [N/A] for memory.total)
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import mac.hardware as hw


# ---------------------------------------------------------------------------
# nvidia-smi fixture outputs (memory.total,name  --format=csv,noheader,nounits)
# ---------------------------------------------------------------------------
# RTX 5090: 32 576 MiB VRAM
_FIXTURE_RTX5090 = "32576, NVIDIA GeForce RTX 5090"
# RTX PRO 6000 Blackwell: 98 304 MiB VRAM
_FIXTURE_RTX_PRO_6000 = "98304, NVIDIA RTX PRO 6000 Blackwell"
# GB10: unified memory, reports [N/A] for memory.total
_FIXTURE_GB10 = "[N/A], NVIDIA GB10"
# Two RTX PRO 6000 Blackwell GPUs (multi-GPU host)
_FIXTURE_RTX_PRO_6000_x2 = "98304, NVIDIA RTX PRO 6000 Blackwell\n98304, NVIDIA RTX PRO 6000 Blackwell"


# ---------------------------------------------------------------------------
# detect_nvidia – fixture-based tests
# ---------------------------------------------------------------------------

def test_detect_nvidia_rtx5090(monkeypatch):
    """RTX 5090: discrete GPU with 32 GB VRAM."""
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_RTX5090)
    gpu = hw.detect_nvidia()
    assert gpu is not None
    assert gpu["name"] == "NVIDIA GeForce RTX 5090"
    assert gpu["vram_mb"] == 32576
    assert gpu["count"] == 1
    assert gpu["accelerator"] == "cuda"
    assert gpu.get("shared") is not True


def test_detect_nvidia_rtx_pro_6000_blackwell(monkeypatch):
    """RTX PRO 6000 Blackwell: discrete GPU with 96 GB VRAM."""
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_RTX_PRO_6000)
    gpu = hw.detect_nvidia()
    assert gpu is not None
    assert gpu["name"] == "NVIDIA RTX PRO 6000 Blackwell"
    assert gpu["vram_mb"] == 98304
    assert gpu["count"] == 1
    assert gpu["accelerator"] == "cuda"
    assert gpu.get("shared") is not True


def test_detect_nvidia_gb10_unified_memory(monkeypatch):
    """GB10: unified memory part — vram_mb equals system memory, shared=True."""
    # Patch _run and _memory_mb so the system memory is predictable
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_GB10)
    monkeypatch.setattr(hw, "_memory_mb", lambda: 131072)  # 128 GB system RAM
    gpu = hw.detect_nvidia()
    assert gpu is not None
    assert gpu["name"] == "NVIDIA GB10"
    assert gpu["vram_mb"] == 131072  # system memory
    assert gpu["shared"] is True
    assert gpu["count"] == 1
    assert gpu["accelerator"] == "cuda"


def test_detect_nvidia_multi_gpu_rtx_pro_6000(monkeypatch):
    """Two RTX PRO 6000 Blackwell GPUs: count=2, vram from first row."""
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: _FIXTURE_RTX_PRO_6000_x2)
    gpu = hw.detect_nvidia()
    assert gpu is not None
    assert gpu["count"] == 2
    assert gpu["vram_mb"] == 98304
    assert gpu["name"] == "NVIDIA RTX PRO 6000 Blackwell"
    assert gpu.get("shared") is not True


# ---------------------------------------------------------------------------
# Legacy / compatibility tests preserved from original test suite
# ---------------------------------------------------------------------------

def test_detect_nvidia_parses_csv(monkeypatch):
    # GB10 style: [N/A] memory — shared=True, vram_mb = system memory
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "[N/A], NVIDIA GB10")
    monkeypatch.setattr(hw, "_memory_mb", lambda: 65536)
    gpu = hw.detect_nvidia()
    assert gpu is not None
    assert gpu["name"] == "NVIDIA GB10"
    assert gpu["shared"] is True
    assert gpu["vram_mb"] == 65536
    assert gpu["count"] == 1


def test_detect_nvidia_multi_gpu_with_vram(monkeypatch):
    monkeypatch.setattr(
        hw, "_run",
        lambda cmd, timeout=5.0: "49140, NVIDIA RTX 6000 Ada\n49140, NVIDIA RTX 6000 Ada",
    )
    gpu = hw.detect_nvidia()
    assert gpu is not None
    assert gpu["count"] == 2
    assert gpu["vram_mb"] == 49140
    assert gpu["accelerator"] == "cuda"


def test_detect_nvidia_absent_returns_none(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: None)  # nvidia-smi missing
    assert hw.detect_nvidia() is None


def test_detect_apple_metal_only_on_arm_darwin(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hw.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "Apple M4 Pro")
    assert hw.detect_apple_metal() == {"accelerator": "metal", "name": "Apple M4 Pro", "vram_mb": 0, "count": 1}
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    assert hw.detect_apple_metal() is None


def test_detect_hardware_composes_and_never_raises(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hw.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(hw.os, "cpu_count", lambda: 20)
    monkeypatch.setattr(hw, "_memory_mb", lambda: 122000)
    monkeypatch.setattr(
        hw, "detect_nvidia",
        lambda: {"accelerator": "cuda", "name": "NVIDIA GB10", "vram_mb": 122000, "shared": True, "count": 1},
    )
    info = hw.detect_hardware()
    assert info["accelerator"] == "cuda" and info["gpu"]["name"] == "NVIDIA GB10"
    assert info["cpu_count"] == 20 and info["memory_mb"] == 122000 and info["arch"] == "aarch64"


def test_detect_hardware_no_accelerator(monkeypatch):
    monkeypatch.setattr(hw, "detect_nvidia", lambda: None)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)
    info = hw.detect_hardware()
    assert info["accelerator"] == "none" and "gpu" not in info


def test_detect_hardware_survives_probe_exception(monkeypatch):
    def _boom():
        raise RuntimeError("nvidia-smi exploded")

    monkeypatch.setattr(hw, "detect_nvidia", _boom)
    monkeypatch.setattr(hw, "detect_apple_metal", lambda: None)
    info = hw.detect_hardware()  # must not raise
    assert info["accelerator"] == "none"


def test_summarize():
    assert "NVIDIA GB10" in hw.summarize({"accelerator": "cuda", "gpu": {"name": "NVIDIA GB10", "count": 1}, "os": "linux", "arch": "aarch64", "cpu_count": 20})
    assert hw.summarize(None) == "(no hardware reported)"
    assert "accelerator=none" in hw.summarize({"accelerator": "none", "os": "linux", "arch": "x86_64"})
    assert "16GB" in hw.summarize({"accelerator": "none", "os": "linux", "arch": "x86_64", "memory_mb": 16384})


def test_summarize_shared_gpu():
    """Unified-memory GPU shows 'shared' label in summary."""
    hw_info = {
        "accelerator": "cuda",
        "os": "linux",
        "arch": "aarch64",
        "cpu_count": 20,
        "memory_mb": 131072,
        "gpu": {"accelerator": "cuda", "name": "NVIDIA GB10", "vram_mb": 131072, "shared": True, "count": 1},
    }
    summary = hw.summarize(hw_info)
    assert "GB10" in summary
    assert "shared" in summary


def test_probe_nonzero_and_linux_memory_edges(monkeypatch):
    monkeypatch.setattr(
        hw.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="ignored"),
    )
    assert hw._run(["missing-probe"]) is None

    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        "builtins.open",
        lambda *_a, **_k: io.StringIO("MemTotal:       2097152 kB\n"),
    )
    assert hw._memory_mb() == 2048


def test_memory_probe_exception_falls_back_to_zero(monkeypatch):
    monkeypatch.setattr(hw.platform, "system", lambda: "Linux")

    def fail_open(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("builtins.open", fail_open)
    assert hw._memory_mb() == 0


def test_is_unified_memory_gpu():
    """_is_unified_memory_gpu identifies known unified parts."""
    assert hw._is_unified_memory_gpu("NVIDIA GB10") is True
    assert hw._is_unified_memory_gpu("nvidia gb10") is True  # case-insensitive
    assert hw._is_unified_memory_gpu("NVIDIA GeForce RTX 5090") is False
    assert hw._is_unified_memory_gpu("NVIDIA RTX PRO 6000 Blackwell") is False
