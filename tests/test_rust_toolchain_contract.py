"""The runtime probe fails before publishing an unusable Rust toolchain."""

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "deploy" / "verify-rust-contract.sh"


def executable(path, body):
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)


def test_missing_cargo_is_explicit(tmp_path):
    result = subprocess.run(
        ["/bin/bash", str(PROBE), "1.95.0"],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing required Rust tool: cargo" in result.stderr


@pytest.mark.parametrize("tool", ["rustc", "cargo"])
def test_wrong_compiler_or_cargo_release_is_rejected(tmp_path, tool):
    for name in ("rustc", "cargo", "rustfmt"):
        version = "1.94.0" if name == tool else "1.95.0"
        executable(tmp_path / name, f"echo '{name} {version} (fixture)' ")
    result = subprocess.run(
        ["/bin/bash", str(PROBE), "1.95.0"],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unexpected" in result.stderr
    assert "expected 1.95.0" in result.stderr


@pytest.mark.parametrize("failure", ["fmt", "run"])
def test_present_tools_must_format_and_execute(tmp_path, failure):
    executable(tmp_path / "rustc", "echo 'rustc 1.95.0 (fixture)'")
    executable(tmp_path / "rustfmt", "exit 0")
    executable(
        tmp_path / "cargo",
        'case "$1" in\n'
        "  --version) echo 'cargo 1.95.0 (fixture)' ;;\n"
        f"  {failure}) exit 19 ;;\n"
        "  *) exit 0 ;;\n"
        "esac",
    )
    result = subprocess.run(
        ["/bin/bash", str(PROBE), "1.95.0"],
        env={**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "standard library, linker and formatter passed" not in result.stdout


def test_unreviewed_rust_version_is_rejected_before_asset_download(tmp_path):
    result = subprocess.run(
        [
            str(ROOT / "deploy/openshell/prepare-runtime-image-assets.sh"),
            "--output",
            str(tmp_path / "assets"),
        ],
        env={**os.environ, "RUST_VERSION": "1.94.0"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "runtime tool version is unreviewed" in result.stderr
    assert not (tmp_path / "assets").exists()
