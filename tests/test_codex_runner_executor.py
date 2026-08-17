"""PR3 integration test: the codex-runner role executor scripts.

These tests run the bash scripts in
``deploy/codex-runner/mac-task-executor-codex(-review)`` directly (the
same scripts the K8s Job pod invokes via MAC_TASK_EXECUTOR_COMMAND) and
verify the resulting verification manifest:

1. Has the shape ``_assess_default_review_evidence`` accepts when fed
   into a real ControlPlane via ``add_evidence`` →
   ``_require_review_ready``.
2. Verifies under the agent's attestation key (mac-ng2).
3. Passes the review-readiness gate so ``submit_for_review`` succeeds.

Without these tests, drift between the bash canonical-JSON encoding
and ``mac.models.json_dumps`` would manifest as a "signature_invalid"
rejection in production — which is exactly what blocked PR2c. The
tests are the contract.

The tests skip when ``bash`` is not available on PATH (e.g. Windows CI
without WSL); on macOS / Linux they run by default.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from mac.models import TaskState
from mac.services import ControlPlane


REPO_ROOT = Path(__file__).resolve().parent.parent
CODER_SCRIPT = REPO_ROOT / "deploy" / "codex-runner" / "mac-task-executor-codex"
REVIEWER_SCRIPT = (
    REPO_ROOT / "deploy" / "codex-runner" / "mac-task-executor-codex-review"
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not available on PATH; PR3 executor scripts are bash",
)


@pytest.fixture()
def cp() -> ControlPlane:
    # There is no bd CLI to stand in for: the beads bridge is removed and
    # MAC_BEADS_CLI has no readers anywhere in the tree.
    return ControlPlane.in_memory()


def _register_worker(cp: ControlPlane, name: str, capabilities: list) -> Any:
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=capabilities)


def _run_executor(
    script: Path,
    *,
    task_id: str,
    lease_id: str,
    agent_id: str,
    agent_role: str,
    task_title: str,
    attestation_key: str | None,
    manifest_path: Path,
    extra_env: Dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        {
            "MAC_TASK_ID": task_id,
            "MAC_LEASE_ID": lease_id,
            "MAC_AGENT_ID": agent_id,
            "MAC_AGENT_ROLE": agent_role,
            "MAC_TASK_TITLE": task_title,
            "MAC_URL": "http://mac-api.svc:80",
            "MAC_TASK_EVIDENCE_MANIFEST_PATH": str(manifest_path),
            # Bash stubs delegate signing to `mac-evidence` (the new
            # console-script installed by the wheel). When tests run
            # against a src checkout without the venv on PATH, point the
            # script at the running interpreter so it can do
            # `python -m mac.evidence_cli`.
            "MAC_EVIDENCE_PYTHON": sys.executable,
        }
    )
    if attestation_key is not None:
        env["MAC_AGENT_ATTESTATION_KEY"] = attestation_key
    else:
        env.pop("MAC_AGENT_ATTESTATION_KEY", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCoderExecutorProducesSignedManifest:
    def test_signed_manifest_passes_review_readiness_gate(
        self, cp: ControlPlane, tmp_path: Path
    ) -> None:
        """End-to-end: the bash executor produces a manifest that the
        Python verifier accepts under the same attestation key.

        This is the regression guard for canonical-JSON drift between
        the bash python heredoc and mac.models.json_dumps.
        """
        worker = _register_worker(cp, "mac-worker-python-coder", ["python", "ops"])
        # `register_agent` returns the cleartext key on FIRST registration
        # only — and the fixture's `register_agent` is the first call.
        attestation_key = getattr(worker, "attestation_key", None)
        assert attestation_key, (
            "fixture agent must surface its attestation key on first "
            "registration; otherwise the signing test is meaningless"
        )

        task = cp.create_task(
            "Build a widget",
            required_capabilities=["ops"],
            metadata={},
        )
        cp.claim_task(task.id, worker.id)
        cp.start_task(task.id, worker.id)

        manifest_path = tmp_path / "mac-evidence.json"
        result = _run_executor(
            CODER_SCRIPT,
            task_id=task.id,
            lease_id="lease-1",
            agent_id=worker.id,
            agent_role="python-coder",
            task_title=task.title,
            attestation_key=attestation_key,
            manifest_path=manifest_path,
        )
        assert result.returncode == 0, (
            "executor failed: stdout=%s stderr=%s" % (result.stdout, result.stderr)
        )
        assert manifest_path.exists(), "executor must write the manifest file"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Shape contract: matches _assess_default_review_evidence at
        # services.py:11204+ AND OperatorResultValidator at
        # evidence_validators.py:222+.
        assert manifest["schema"] == "mac.worker_evidence.v1"
        assert manifest["status"] == "complete"
        assert manifest["evidence_type"] == "operator_result"
        assert manifest["signed_by"] == worker.id
        assert manifest["signature"].startswith("v1:")
        assert manifest["summary"]  # operator_result requires summary OR result

        # Now wire it through add_evidence + submit_for_review. If the
        # bash canonical form drifts from json_dumps, submit_for_review
        # raises ValidationError("signature_invalid"). If we get to
        # NEEDS_REVIEW the executor's signature verified.
        cp.add_evidence(
            task.id,
            "log",
            "artifact://operator-result",
            "executor produced operator_result manifest",
            worker.id,
            metadata={"returncode": 0, "verification": manifest},
        )
        cp.submit_for_review(task.id, worker.id)
        assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value

    def test_unsigned_manifest_is_rejected_with_clear_diagnostic(
        self, cp: ControlPlane, tmp_path: Path
    ) -> None:
        """Without an attestation key, the bash executor still writes a
        structurally-coherent manifest but leaves it unsigned. mac then
        rejects with manifest_not_signed — the documented fail-loud
        signal for an operator who didn't seed the per-role key."""
        worker = _register_worker(cp, "mac-worker-python-coder", ["python", "ops"])
        task = cp.create_task(
            "Build a widget",
            required_capabilities=["ops"],
            metadata={},
        )
        cp.claim_task(task.id, worker.id)
        cp.start_task(task.id, worker.id)

        manifest_path = tmp_path / "mac-evidence.json"
        result = _run_executor(
            CODER_SCRIPT,
            task_id=task.id,
            lease_id="lease-1",
            agent_id=worker.id,
            agent_role="python-coder",
            task_title=task.title,
            attestation_key=None,  # <-- no key
            manifest_path=manifest_path,
        )
        assert result.returncode == 0
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Manifest is structurally well-formed but explicitly marked
        # incomplete + unsigned.
        assert manifest["status"] == "incomplete"
        assert "signed_by" not in manifest
        assert "signature" not in manifest

        # Hub rejection — same path as today's PR2c failure but with
        # the diagnostic "manifest_not_signed" instead of
        # "missing_verification_manifest".
        cp.add_evidence(
            task.id,
            "log",
            "artifact://operator-result",
            "executor produced unsigned manifest",
            worker.id,
            metadata={"returncode": 0, "verification": manifest},
        )
        # status=incomplete fails BEFORE the signed_by check, so the
        # operator sees "status must be complete" — also a clear signal.
        from mac.models import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            cp.submit_for_review(task.id, worker.id)
        msg = str(exc_info.value)
        # Either the status check OR the signed_by check is hit;
        # both are fine as fail-loud signals. The point is we don't
        # silently accept the unsigned manifest.
        assert any(
            phrase in msg
            for phrase in (
                "status",
                "signed_by",
                "signature",
                "manifest",
            )
        )


class TestDeprecatedReviewerExecutorFailsClosed:
    def test_deprecated_reviewer_cannot_emit_approval(
        self, cp: ControlPlane, tmp_path: Path
    ) -> None:
        """The removed always-approve path must fail without a manifest."""
        reviewer = _register_worker(
            cp, "mac-worker-python-reviewer", ["review", "python"]
        )
        attestation_key = getattr(reviewer, "attestation_key", None)
        assert attestation_key

        manifest_path = tmp_path / "mac-evidence.json"
        result = _run_executor(
            REVIEWER_SCRIPT,
            task_id="task-under-review",
            lease_id="lease-r1",
            agent_id=reviewer.id,
            agent_role="python-reviewer",
            task_title="Review widget",
            attestation_key=attestation_key,
            manifest_path=manifest_path,
            extra_env={"MAC_REVIEW_TARGET_EVIDENCE_ID": "ev-target-123"},
        )
        assert result.returncode == 64
        assert "disabled" in result.stderr
        assert not manifest_path.exists()
