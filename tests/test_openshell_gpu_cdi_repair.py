"""A stale CDI mount must not cost a GPU node its accelerator.

Measured on all five GKE workers 2026-08-05. Their CDI spec mounts the
nvidia-persistenced socket into every GPU container, but nvidia-persistenced is
not running on those nodes: /run/nvidia-persistenced/socket is a stale entry
that stat(2) can see and bind(2) cannot mount. Every GPU container therefore
failed to start --

  error during container init: error mounting "/run/nvidia-persistenced/socket"
  to rootfs ... no such file or directory

-- and not just under OpenShell: plain `docker run --gpus all` failed
identically. Five GPU nodes ran as CPU-only nodes and nothing said so, because
the bootstrap's GPU smoke failure was a warning.

The mount is unnecessary: persistence mode is a daemon feature and nothing in a
task container speaks to that socket. Removing the entry made GPU containers
start immediately.

The repair has to be narrow, because the same spec is correct on a node where
persistenced really is running, and it must survive pod recreation -- a GKE
worker comes up from an image with a fresh filesystem, so a one-off manual edit
is lost on the next bring-up.

These tests extract the real shell function from the bootstrap and run it
against fixture specs with a real listening socket, a real stale socket, and no
socket at all.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"

SPEC_WITH_SOCKET = """\
cdiVersion: 0.5.0
kind: nvidia.com/gpu
devices:
  - name: all
    containerEdits:
      mounts:
        - hostPath: /usr/lib/libcuda.so.1
          containerPath: /usr/lib/libcuda.so.1
          options:
            - ro
            - nosuid
        - hostPath: {socket}
          containerPath: {socket}
          options:
            - nosuid
            - nodev
            - rbind
        - hostPath: /usr/bin/nvidia-persistenced
          containerPath: /usr/bin/nvidia-persistenced
          options:
            - ro
            - nosuid
"""


def _extract_function(name: str) -> str:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("\n%s() {\n" % name) + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _harness(spec: Path, sock: Path) -> str:
    """Run the real function with $spec/$socket redirected at fixtures."""
    body = _extract_function("repair_stale_cdi_persistenced_mount")
    body = body.replace("local spec=/etc/cdi/nvidia.yaml", 'local spec="%s"' % spec)
    body = body.replace(
        "local socket=/run/nvidia-persistenced/socket", 'local socket="%s"' % sock
    )
    return "log() { printf '%s\\n' \"$*\" >&2; }\n" + body + "\nrepair_stale_cdi_persistenced_mount\n"


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-uo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )


def _write_spec(tmp_path: Path, sock: Path) -> Path:
    spec = tmp_path / "nvidia.yaml"
    spec.write_text(SPEC_WITH_SOCKET.format(socket=sock), encoding="utf-8")
    return spec


def test_a_stale_socket_mount_is_removed(tmp_path):
    """The regression: socket file exists, nothing listening."""
    sock = tmp_path / "socket"
    sock.write_text("", encoding="utf-8")  # present, but no listener
    spec = _write_spec(tmp_path, sock)

    result = _run(_harness(spec, sock))

    text = spec.read_text(encoding="utf-8")
    assert "hostPath: %s" % sock not in text, (
        "the stale socket mount survived, so every GPU container still fails:\n%s"
        % result.stderr
    )
    assert "removing stale nvidia-persistenced socket mount" in result.stderr


def test_the_binary_mount_and_other_devices_survive(tmp_path):
    """Only the socket entry goes. /usr/bin/nvidia-persistenced is a real file."""
    sock = tmp_path / "socket"
    sock.write_text("", encoding="utf-8")
    spec = _write_spec(tmp_path, sock)

    _run(_harness(spec, sock))

    text = spec.read_text(encoding="utf-8")
    assert "hostPath: /usr/bin/nvidia-persistenced" in text, (
        "the persistenced BINARY mount was removed; only the socket is stale"
    )
    assert "hostPath: /usr/lib/libcuda.so.1" in text, "an unrelated mount was removed"
    assert "containerPath: /usr/lib/libcuda.so.1" in text
    assert "cdiVersion: 0.5.0" in text, "the spec header was damaged"
    # The removed item must not leave its option list orphaned behind.
    assert "- rbind" not in text, "the removed entry left dangling options"


def test_a_live_persistenced_is_left_alone(tmp_path):
    """On a node where the daemon really runs, the mount is correct."""
    # AF_UNIX paths are capped near 104 bytes, well under pytest's tmp_path.
    short_dir = Path(tempfile.mkdtemp(prefix="cdi", dir="/tmp"))
    sock = short_dir / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    server.listen(1)
    stop = threading.Event()

    def _accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                continue

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    try:
        spec = _write_spec(tmp_path, sock)
        result = _run(_harness(spec, sock))
        text = spec.read_text(encoding="utf-8")
        assert "hostPath: %s" % sock in text, (
            "removed a mount for a LIVE nvidia-persistenced; that would break "
            "persistence mode on a healthy node:\n%s" % result.stderr
        )
        assert "leaving its CDI socket mount in place" in result.stderr
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()


def test_no_socket_at_all_is_left_alone(tmp_path):
    """Nothing to repair when the host never had the socket."""
    sock = tmp_path / "socket"  # never created
    spec = _write_spec(tmp_path, sock)
    before = spec.read_text(encoding="utf-8")

    _run(_harness(spec, sock))

    assert spec.read_text(encoding="utf-8") == before


def test_a_spec_without_the_mount_is_untouched(tmp_path):
    sock = tmp_path / "socket"
    sock.write_text("", encoding="utf-8")
    spec = tmp_path / "nvidia.yaml"
    spec.write_text("cdiVersion: 0.5.0\nkind: nvidia.com/gpu\n", encoding="utf-8")
    before = spec.read_text(encoding="utf-8")

    _run(_harness(spec, sock))

    assert spec.read_text(encoding="utf-8") == before


def test_a_missing_spec_is_not_an_error(tmp_path):
    """Non-GPU nodes have no CDI spec; the repair must be a quiet no-op."""
    sock = tmp_path / "socket"
    sock.write_text("", encoding="utf-8")
    result = _run(_harness(tmp_path / "absent.yaml", sock))
    assert result.returncode == 0, result.stderr


def test_the_repair_runs_before_the_gpu_smoke():
    """A repair that runs after the probe it fixes would prove nothing."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    repair = text.index("repair_stale_cdi_persistenced_mount\n    gpu_smoke_name")
    smoke = text.index("gpu_smoke_name=")
    assert repair < smoke, (
        "the CDI repair must precede the GPU smoke, or the smoke still fails "
        "and the node still advertises no GPU"
    )
