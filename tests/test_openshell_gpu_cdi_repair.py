"""Unmountable CDI entries must not cost a GPU node its accelerator.

Measured on all five GKE workers 2026-08-05: every node had a real RTX PRO
6000 MIG device and not one could start a GPU container. Their CDI spec lists
driver files the container runtime cannot bind-mount, and any single such entry
kills the container at init --

  error during container init: error mounting "<path>" to rootfs ...
  no such file or directory

-- so five GPU nodes ran as CPU-only nodes, silently, because the bootstrap's
GPU smoke failure was only a warning.

Two plausible fixes were tried on worker1 first, and both are recorded here
because they look right and are not:

* "drop the entries whose path is missing" -- every path is PRESENT to a shell
  on the node. dockerd does not share our mount namespace, so existence is
  simply not the property that decides this.
* `nvidia-ctk cdi generate` -- regenerating put the bad entries straight back,
  because nvidia-ctk resolves them in its own view too.

The only authority on what the runtime can mount is the runtime. So the repair
asks it: start a GPU container, and when it names a mount it cannot make, drop
that entry and retry. On worker1 that converged in two removals, after which
nvidia-smi inside the container reported the full MIG device.

Removing an entry the runtime has just refused cannot lose capability, because
the container was not going to start at all.

These tests extract the real shell function and drive it with a fake docker
whose refusals are read back from the spec, so the loop, the parsing, the stop
conditions and the YAML surgery are all exercised without a GPU.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"

SPEC = """\
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
        - hostPath: /run/nvidia-persistenced/socket
          containerPath: /run/nvidia-persistenced/socket
          options:
            - nosuid
            - rbind
        - hostPath: /lib/firmware/nvidia/580/gsp_ga10x.bin
          containerPath: /lib/firmware/nvidia/580/gsp_ga10x.bin
          options:
            - ro
            - rbind
        - hostPath: /usr/bin/nvidia-persistenced
          containerPath: /usr/bin/nvidia-persistenced
          options:
            - ro
"""


def _extract_function(name: str) -> str:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    start = text.index("\n%s() {\n" % name) + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _fake_docker(tmp_path: Path, failing: list) -> Path:
    """A docker that refuses each listed mount while it is still in the spec.

    It re-reads the spec on every call, so the loop has to actually remove an
    entry to make progress. That makes this a convergence test rather than a
    scripted sequence of canned replies.
    """
    script = tmp_path / "docker"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "spec = pathlib.Path(%r)\n"
        "failing = %r\n"
        "text = spec.read_text()\n"
        "for path in failing:\n"
        "    if ('hostPath: ' + path) in text:\n"
        "        sys.stderr.write('docker: Error response from daemon: failed to "
        "create shim task: error during container init: error mounting \"'\n"
        "                         + path + '\" to rootfs: no such file or directory\\n')\n"
        "        raise SystemExit(125)\n"
        "raise SystemExit(0)\n"
        % (str(tmp_path / "nvidia.yaml"), failing),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _harness(tmp_path: Path, docker: Path) -> str:
    body = _extract_function("prune_unmountable_cdi_entries")
    body = body.replace(
        "local spec=/etc/cdi/nvidia.yaml",
        'local spec="%s"' % (tmp_path / "nvidia.yaml"),
    )
    return (
        "log() { printf '%s\\n' \"$*\" >&2; }\n"
        + 'OSH_DOCKER_BIN="%s"\nOSH_IMAGE_TAG=probe:latest\nOSH_DIR="%s"\n'
        % (docker, tmp_path)
        + body
        + "\nprune_unmountable_cdi_entries\n"
    )


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-uo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        timeout=90,
        env=dict(os.environ),
    )


def _spec(tmp_path: Path) -> Path:
    path = tmp_path / "nvidia.yaml"
    path.write_text(SPEC, encoding="utf-8")
    return path


def test_it_converges_over_several_unmountable_entries(tmp_path):
    """The regression: worker1 needed two removals, not one."""
    failing = [
        "/run/nvidia-persistenced/socket",
        "/lib/firmware/nvidia/580/gsp_ga10x.bin",
    ]
    spec = _spec(tmp_path)

    result = _run(_harness(tmp_path, _fake_docker(tmp_path, failing)))

    text = spec.read_text(encoding="utf-8")
    for path in failing:
        assert "hostPath: %s" % path not in text, (
            "%s survived, so GPU containers still fail:\n%s" % (path, result.stderr)
        )
    assert "GPU containers start" in result.stderr


def test_mounts_the_runtime_accepts_are_kept(tmp_path):
    """Only what the runtime refused is removed."""
    spec = _spec(tmp_path)

    _run(_harness(tmp_path, _fake_docker(tmp_path, ["/run/nvidia-persistenced/socket"])))

    text = spec.read_text(encoding="utf-8")
    assert "hostPath: /usr/lib/libcuda.so.1" in text, "dropped a working driver mount"
    assert "hostPath: /usr/bin/nvidia-persistenced" in text, "dropped the binary mount"
    assert "hostPath: /lib/firmware/nvidia/580/gsp_ga10x.bin" in text, (
        "dropped a mount the runtime never complained about"
    )
    assert "cdiVersion: 0.5.0" in text
    assert "- ro\n" in text, "the surviving entries lost their options"


def test_a_healthy_node_is_untouched(tmp_path):
    """Nothing to repair when GPU containers already start."""
    spec = _spec(tmp_path)
    before = spec.read_text(encoding="utf-8")

    result = _run(_harness(tmp_path, _fake_docker(tmp_path, [])))

    assert spec.read_text(encoding="utf-8") == before
    assert "removing it from" not in result.stderr


def test_a_non_mount_failure_leaves_the_spec_alone(tmp_path):
    """A GPU broken for some other reason must not be 'repaired' by deletion."""
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\necho 'docker: Error response from daemon: no such image' >&2\nexit 125\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    spec = _spec(tmp_path)
    before = spec.read_text(encoding="utf-8")

    result = _run(_harness(tmp_path, docker))

    assert spec.read_text(encoding="utf-8") == before, (
        "removed CDI entries for a failure that was not a mount error"
    )
    assert "non-mount reason" in result.stderr


def test_it_refuses_to_remove_driver_libraries(tmp_path):
    """The guard that keeps a repair from becoming a silent GPU-less GPU.

    Dropping an auxiliary mount is safe: the container was not starting anyway.
    Dropping libcuda would make the container START and then have no working
    GPU -- converting a loud failure into a silent one, which is the exact
    pattern this whole line of work exists to remove. A node whose runtime
    cannot mount its driver libraries is broken and must say so.
    """
    spec = _spec(tmp_path)

    result = _run(_harness(tmp_path, _fake_docker(tmp_path, ["/usr/lib/libcuda.so.1"])))

    text = spec.read_text(encoding="utf-8")
    assert "hostPath: /usr/lib/libcuda.so.1" in text, (
        "removed a CUDA driver library; the container would start with no GPU"
    )
    assert "refusing to remove it" in result.stderr
    assert "needs operator attention" in result.stderr


def test_a_missing_spec_is_a_quiet_no_op(tmp_path):
    """Non-GPU nodes have no CDI spec."""
    result = _run(_harness(tmp_path, _fake_docker(tmp_path, [])))
    assert result.returncode == 0, result.stderr


def test_the_original_spec_is_backed_up_before_the_first_removal(tmp_path):
    spec = _spec(tmp_path)
    _run(_harness(tmp_path, _fake_docker(tmp_path, ["/run/nvidia-persistenced/socket"])))
    backup = Path(str(spec) + ".mac-bak")
    assert backup.is_file(), "no backup was taken before rewriting the CDI spec"
    assert "hostPath: /run/nvidia-persistenced/socket" in backup.read_text(
        encoding="utf-8"
    ), "the backup must hold the ORIGINAL spec, not the repaired one"


def test_the_repair_runs_before_the_gpu_smoke():
    """A repair that runs after the probe it fixes would prove nothing."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "prune_unmountable_cdi_entries\n    gpu_smoke_name" in text, (
        "the CDI repair must immediately precede the GPU smoke, or the smoke "
        "still fails and the node still advertises no GPU"
    )
