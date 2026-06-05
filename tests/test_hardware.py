"""First-class hardware self-reporting: detection (best-effort, never raises) +
summary. Subprocess probes are monkeypatched so tests are host-independent."""
from __future__ import annotations

import mac.hardware as hw


def test_detect_nvidia_parses_csv(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "NVIDIA GB10, [N/A]")
    gpu = hw.detect_nvidia()
    assert gpu == {"accelerator": "cuda", "name": "NVIDIA GB10", "vram_mb": 0, "count": 1}


def test_detect_nvidia_multi_gpu_with_vram(monkeypatch):
    monkeypatch.setattr(hw, "_run", lambda cmd, timeout=5.0: "NVIDIA RTX 6000 Ada, 49140\nNVIDIA RTX 6000 Ada, 49140")
    gpu = hw.detect_nvidia()
    assert gpu["count"] == 2 and gpu["vram_mb"] == 49140 and gpu["accelerator"] == "cuda"


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
    monkeypatch.setattr(hw, "detect_nvidia", lambda: {"accelerator": "cuda", "name": "NVIDIA GB10", "vram_mb": 0, "count": 1})
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
