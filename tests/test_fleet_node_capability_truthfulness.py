"""An agent must advertise only what it can actually deliver.

Two measurements on the live fleet, 2026-08-05, motivated this module.

GPU. All five GKE workers reported MAC_OPENSHELL_GPU_AVAILABLE=0 -- the
bootstrap's GPU smoke test could not start a nested GPU container, because
/run/nvidia-persistenced/socket does not exist inside the pod -- and all five
still advertised "gpu" and "cuda" to the hub, because the capability probe
consulted nvidia-smi and nothing else. Tasks execute INSIDE OpenShell
sandboxes, so host-visible hardware is not the capability on offer.
src/mac/executor_sandbox.py already refuses such a task outright, so the
dispatcher matched work to hosts guaranteed to reject it rather than routing it
to bullwinkle or natasha, which can run it.

Health. Three of those five finished their deploy with the startup probe
reporting ready=False and their Hermes memory topology missing, and then
registered "healthy" anyway: the verdict was printed for humans and discarded.

Both are the same defect in different clothes -- a correct measurement that no
consumer acts on -- so both are tested here.

These tests EXTRACT THE REAL SHELL from deploy/fleet-node-install.sh and
execute it. Asserting on substrings of the script would pass just as happily
against a comment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"


def _script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
    """Pull one top-level `name() { ... }` definition out of the installer."""
    text = _script()
    start = text.index("\n%s() {\n" % name) + 1
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _extract_capability_block() -> str:
    """The capability probe: from the hardware-probe comment to the cpu append."""
    text = _script()
    start = text.index("# Hardware capability probes:")
    end = text.index('capabilities="$capabilities,cpu"', start) + len(
        'capabilities="$capabilities,cpu"'
    )
    return text[start:end]


def _run_bash(script: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.update(env)
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        env=full,
        cwd=str(cwd),
        timeout=60,
    )


# --------------------------------------------------------------------------
# GPU capability advertisement
# --------------------------------------------------------------------------


def _capability_harness(tmp_path: Path, *, has_gpu: bool) -> str:
    """Run the real capability block with a fake nvidia-smi."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    if has_gpu:
        nvidia = fake_bin / "nvidia-smi"
        nvidia.write_text(
            '#!/bin/sh\necho "GPU 0: NVIDIA RTX PRO 6000 Blackwell Server Edition (UUID: GPU-x)"\n',
            encoding="utf-8",
        )
        nvidia.chmod(0o755)
    return (
        'export PATH="%s:$PATH"\n'
        'capabilities="ops,python"\n'
        "%s\n"
        'printf "%%s\\n" "$capabilities"\n' % (fake_bin, _extract_capability_block())
    )


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_gpu_is_advertised_when_the_sandbox_smoke_proved_it(tmp_path, flag):
    """natasha and bullwinkle must keep their GPU capability."""
    result = _run_bash(
        _capability_harness(tmp_path, has_gpu=True),
        {"MAC_OPENSHELL_GPU_AVAILABLE": flag},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    caps = result.stdout.strip().split(",")
    assert "gpu" in caps and "cuda" in caps, (
        "a host whose OpenShell GPU smoke passed must still advertise gpu: %s" % result.stdout
    )
    assert "cpu" in caps


@pytest.mark.parametrize("flag", ["0", "false", "no", "off", ""])
def test_gpu_is_withheld_when_the_sandbox_cannot_use_it(tmp_path, flag):
    """The regression. Host GPU present, sandbox GPU proven unusable."""
    result = _run_bash(
        _capability_harness(tmp_path, has_gpu=True),
        {"MAC_OPENSHELL_GPU_AVAILABLE": flag},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    caps = result.stdout.strip().split(",")
    assert "gpu" not in caps and "cuda" not in caps, (
        "advertised GPU capability the executor will refuse "
        "(MAC_OPENSHELL_GPU_AVAILABLE=%r): %s" % (flag, result.stdout)
    )
    assert "cpu" in caps, "the host is still a perfectly good CPU worker"
    assert "not advertising gpu/cuda" in result.stderr, (
        "withholding a capability must say so; a silent downgrade is how this "
        "went unnoticed in the first place"
    )


def test_an_unset_flag_is_treated_as_unproved(tmp_path):
    """Unset must match env_bool's default=False, which is what the executor uses."""
    result = _run_bash(_capability_harness(tmp_path, has_gpu=True), {}, tmp_path)
    assert result.returncode == 0, result.stderr
    caps = result.stdout.strip().split(",")
    assert "gpu" not in caps, (
        "unset is not proof of GPU access; executor_sandbox.py would refuse the "
        "task, so advertising it guarantees a failure"
    )


def test_a_host_with_no_gpu_is_unaffected(tmp_path):
    """rocky has no nvidia-smi at all; the flag must not invent a GPU."""
    result = _run_bash(
        _capability_harness(tmp_path, has_gpu=False),
        {"MAC_OPENSHELL_GPU_AVAILABLE": "1"},
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    caps = result.stdout.strip().split(",")
    assert "gpu" not in caps and "cuda" not in caps
    assert "cpu" in caps


# --------------------------------------------------------------------------
# Health must reflect the startup probe
# --------------------------------------------------------------------------


def _health_harness(tmp_path: Path, report: object | None) -> str:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    if report is not None:
        (log_dir / "startup-hermes.json").write_text(
            report if isinstance(report, str) else json.dumps(report),
            encoding="utf-8",
        )
    return 'LOG_DIR="%s"\nPY="%s"\n%s\nstartup_probe_health_status\n' % (
        log_dir,
        shutil.which("python3") or "python3",
        _extract_function("startup_probe_health_status"),
    )


def test_a_not_ready_node_reports_degraded(tmp_path):
    """The regression: ready=False must not register as healthy."""
    report = {
        "ready": False,
        "warnings": ["Hermes memory topology file is missing"],
        "qdrant_level2": {"status": "missing_topology", "ready": False},
    }
    result = _run_bash(_health_harness(tmp_path, report), {}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "degraded", (
        "a node that failed its own readiness probe reported healthy: %s" % result.stdout
    )


def test_a_ready_node_reports_healthy(tmp_path):
    result = _run_bash(_health_harness(tmp_path, {"ready": True, "warnings": []}), {}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "healthy"


def test_a_missing_report_is_not_treated_as_degraded(tmp_path):
    """A node that runs no Hermes startup probe proves nothing either way.

    Inventing a degradation there would mark healthy hosts unhealthy, which is
    the same class of untruth in the opposite direction.
    """
    result = _run_bash(_health_harness(tmp_path, None), {}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "healthy"


def test_an_unreadable_report_is_not_treated_as_degraded(tmp_path):
    result = _run_bash(_health_harness(tmp_path, "{not json"), {}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "healthy"


def test_the_drain_clear_sends_the_measured_health(tmp_path):
    """The verdict must reach the heartbeat, not just be computed."""
    body = _extract_function("clear_mac_agent_drain_after_deploy")
    assert "startup_probe_health_status" in body, (
        "clear_mac_agent_drain_after_deploy no longer consults the startup "
        "probe, so a not-ready node would register healthy again"
    )
    assert '"health_status\\":\\"$health' in body or "$health" in body, (
        "the measured health must be interpolated into the heartbeat payload"
    )
    assert '"health_status":"healthy"' not in body, "the heartbeat still hardcodes healthy"


# --------------------------------------------------------------------------
# Typed phase 2 must repair absent gateway state under OpenClaw home
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "writer,filename",
    [
        ("write_hermes_memory_topology", "mac-memory-topology.json"),
        ("write_hermes_runtime_context", "mac-runtime-context.json"),
    ],
)
def test_typed_phase_two_repairs_absent_gateway_state(writer, filename):
    """A recreated fungible node must be able to get these files back.

    Both writers ran only on the legacy-one-shot path, so a typed phase-2
    deploy could never restore them: three of five GKE workers were still
    missing both on 2026-08-05. Phase 2 must keep refusing to MUTATE an
    existing file while still repairing an absent one. After Hermes retirement
    the live files belong under $MAC_HOME/openclaw, not ~/.hermes.
    """
    text = _script()
    guarded = re.search(
        r'if \[ ! -f "\$\(mac_gateway_home\)/%s" \]; then\n(?:.*\n)*?\s*%s\n'
        % (re.escape(filename), re.escape(writer)),
        text,
    )
    assert guarded, (
        "typed phase 2 has no absence-repair for %s, so a recreated node can "
        "never regain it" % filename
    )
    # The repair must be conditional: an existing file is still left alone.
    assert '! -f "$(mac_gateway_home)/%s"' % filename in text
    # Recreating ~/.hermes is the opposite of retiring Hermes.
    assert "$HOME/.hermes/%s" % filename not in text


def test_installer_defaults_gateway_home_to_openclaw_not_hermes():
    """Deploy must not recreate a vacated ~/.hermes tree.

    Python already resolves gateway_home() to $MAC_HOME/openclaw. The installer
    and mac.env rewrite were still pinning ~/.hermes, which is how the last
    fleet deploy put that directory back after GC.
    """
    text = _script()
    assert "mac_gateway_home()" in text
    assert "${HERMES_HOME:-$HOME/.hermes}" not in text
    assert "$HOME/.hermes/mac-memory-topology.json" not in text
    assert "$HOME/.hermes/mac-runtime-context.json" not in text
    assert 'local skills_dir="$HOME/.hermes/skills"' not in text
    assert 'local skills_dir="$MAC_HOME/openclaw/workspace/skills"' in text
    # Continuity may still READ a leftover tree; it must not mkdir one.
    assert '[ ! -d "$HOME/.hermes" ]' in text
