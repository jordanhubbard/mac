"""Integration tests for the opencode-build role executor script.

Runs ``deploy/codex-runner/mac-task-executor-opencode-build`` (the same
bash script the K8s Job pod invokes via MAC_TASK_EXECUTOR_COMMAND) with
fake ``opencode``, ``git``, ``curl`` and ``mac`` binaries on PATH and
asserts on the resulting ``mac.worker_evidence.v1`` manifest.

These guard the observability contract for the build executor:

* opencode stdout AND stderr are captured separately and exposed in the
  manifest as bounded head + tail slices (provider errors often appear at
  the tail), with byte counts + sha256 so a postmortem isn't limited to a
  single truncated preview.
* a parsed opencode event summary (event counts, first/last error,
  tool/edit-call counts, final step_finish reason) is recorded.
* the original opencode return code is preserved separately from the
  effective/guarded return code.
* the selected opencode model/provider is recorded.
* a no-change run records git diagnostics (status, cwd, branch) and the
  full files_changed list.
* a successful push followed by a failed PR/MR open fails the run.

The tests skip when ``bash`` is not available on PATH.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest


from mac.services import ControlPlane


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = (
    REPO_ROOT / "deploy" / "codex-runner" / "mac-task-executor-opencode-build"
)


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None,
    reason="bash is not available on PATH; the executor scripts are bash",
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _make_fake_bin(
    bindir: Path,
    *,
    opencode_stdout: str,
    opencode_stderr: str = "",
    opencode_rc: int = 0,
    make_change: bool = True,
    push_rc: int = 0,
    pr_rc: int = 0,
    pr_json: str = '{"url": "https://example.test/mr/1", "number": 1}',
    opencode_config: Optional[str] = None,
) -> None:
    """Create fake opencode/git/curl/mac/python helpers on PATH.

    The fakes are intentionally dumb: ``opencode run`` prints the canned
    stdout/stderr and exits with ``opencode_rc``; ``git`` simulates a
    clone (creating a working tree + optional change) and a push; ``mac
    pull-request open`` returns canned PR JSON or fails.
    """
    bindir.mkdir(parents=True, exist_ok=True)

    # opencode: only `run` matters; `--version` returns a banner.
    oc_out = bindir / "_opencode_stdout.txt"
    oc_out.write_text(opencode_stdout)
    oc_err = bindir / "_opencode_stderr.txt"
    oc_err.write_text(opencode_stderr)
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        f'  cat "{oc_out}"\n'
        f'  cat "{oc_err}" >&2\n'
        f'  exit {opencode_rc}\n'
        "fi\n"
        "exit 0\n",
    )

    # git: clone makes the workdir a git repo; checkout/config/add/commit
    # are no-ops; status reports a change iff make_change; push exits with
    # push_rc; rev-parse/diff return canned values.
    change_flag = "yes" if make_change else "no"
    _write_exec(
        bindir / "git",
        "#!/usr/bin/env bash\n"
        'cmd="$1"; shift\n'
        'case "$cmd" in\n'
        "  clone)\n"
        '    dest="${@: -1}"\n'
        '    mkdir -p "$dest/.git"\n'
        "    ;;\n"
        "  config|checkout|add|commit) : ;;\n"
        "  status)\n"
        f'    if [ "{change_flag}" = "yes" ]; then echo " M a.txt"; fi\n'
        "    ;;\n"
        "  diff)\n"
        f'    if [ "{change_flag}" = "yes" ]; then printf "a.txt\\nb.txt\\n"; fi\n'
        "    ;;\n"
        '  rev-parse) echo "deadbeefcafef00d" ;;\n'
        f"  push) exit {push_rc} ;;\n"
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )

    # curl: the script fetches task + project JSON. Return a task carrying
    # an origin repository_url so the workdir branch is exercised.
    task_json = json.dumps(
        {
            "task": {
                "title": "Do a thing",
                "description": "edit files",
                "project": "demo",
                "metadata": {
                    "origin": {
                        "repository_url": "https://gitea.omv.example/org/repo.git",
                        "default_branch": "main",
                    }
                },
            }
        }
    )
    _write_exec(
        bindir / "curl",
        "#!/usr/bin/env bash\n"
        # Last arg is the URL.
        'url="${@: -1}"\n'
        'case "$url" in\n'
        f'  *"/tasks/"*) cat <<\'JSON\'\n{task_json}\nJSON\n;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )

    # mac: only `pull-request open` is used.
    _write_exec(
        bindir / "mac",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "pull-request" ] && [ "$2" = "open" ]; then\n'
        f'  if [ "{pr_rc}" -ne 0 ]; then echo "boom" >&2; exit {pr_rc}; fi\n'
        f"  cat <<'JSON'\n{pr_json}\nJSON\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    # opencode config consumed by the script via /etc/opencode mount. We
    # cannot write /etc here, so the script reads OPENCODE_CONFIG_PATH if
    # set (added by the executor for testability).
    if opencode_config is not None:
        cfg = bindir.parent / "opencode.json"
        cfg.write_text(opencode_config)


def _run_build(
    *,
    bindir: Path,
    manifest_path: Path,
    task_id: str = "task_demo",
    agent_id: str = "mac-worker-python-coder-opencode",
    opencode_config_path: Optional[Path] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Hermetic: drop inherited fleet/runtime env.
    for key in list(env):
        if key.startswith(("MAC_", "INFERENCE_HUB_", "GITEA_", "GH_")):
            env.pop(key, None)
    env["PATH"] = "%s%s%s" % (bindir, os.pathsep, env.get("PATH", ""))
    env.update(
        {
            "MAC_TASK_ID": task_id,
            "MAC_LEASE_ID": "lease-1",
            "MAC_AGENT_ID": agent_id,
            "MAC_AGENT_ROLE": "python-coder-opencode",
            "MAC_URL": "http://mac-api.svc:80",
            "MAC_WORKER_TOKEN": "tok",
            "MAC_TASK_EVIDENCE_MANIFEST_PATH": str(manifest_path),
            "MAC_EVIDENCE_PYTHON": sys.executable,
        }
    )
    if opencode_config_path is not None:
        env["MAC_OPENCODE_CONFIG_PATH"] = str(opencode_config_path)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _findings_by_kind(manifest: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for finding in manifest.get("findings", []):
        kind = finding.get("kind")
        if kind:
            out[kind] = finding
    return out


def test_captures_stdout_stderr_head_tail_and_event_summary(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    # Build a long JSON-lines opencode stream: a head error, many tool
    # events, and a final step_finish; plus a distinctive tail error.
    lines: List[str] = []
    lines.append(json.dumps({"type": "step_start"}))
    lines.append(json.dumps({"type": "error", "message": "first failure"}))
    for _ in range(50):
        lines.append(json.dumps({"type": "tool", "name": "edit"}))
        lines.append(json.dumps({"type": "tool", "name": "read"}))
    lines.append(json.dumps({"type": "error", "message": "last failure"}))
    lines.append(json.dumps({"type": "step_finish", "reason": "stop"}))
    stdout = "\n".join(lines) + "\n"
    stderr = "provider error: model rejected request\n" * 200

    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_stderr=stderr,
        opencode_rc=0,
        make_change=True,
    )
    cfg = tmp_path / "opencode.json"
    cfg.write_text(
        json.dumps(
            {
                "model": "inference-hub/aws/anthropic/bedrock-claude-opus-4-8",
                "agent": {
                    "build": {
                        "model": "inference-hub/aws/anthropic/bedrock-claude-opus-4-8"
                    }
                },
            }
        )
    )

    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        opencode_config_path=cfg,
    )
    assert manifest_path.exists(), (
        "executor must write a manifest; stdout=%s stderr=%s"
        % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings = _findings_by_kind(manifest)

    # stdout finding: bounded head + tail, byte count + sha256.
    out = findings["opencode_stdout"]
    assert "head" in out and "tail" in out
    assert out["bytes"] == len(stdout.encode("utf-8"))
    assert out["sha256"] == hashlib.sha256(stdout.encode("utf-8")).hexdigest()
    # tail must include the final events, not just the head.
    assert "last failure" in out["tail"] or "step_finish" in out["tail"]

    # stderr captured separately with head/tail + bytes + sha256.
    err = findings["opencode_stderr"]
    assert err["bytes"] == len(stderr.encode("utf-8"))
    assert err["sha256"] == hashlib.sha256(stderr.encode("utf-8")).hexdigest()
    assert "provider error" in err["tail"]

    # event summary.
    summary = findings["opencode_event_summary"]
    assert summary["total_lines"] >= len(lines)
    assert summary["parseable_json_lines"] >= len(lines)
    counts = summary["event_counts"]
    assert counts.get("tool") == 100
    assert counts.get("error") == 2
    assert counts.get("step_finish") == 1
    assert summary["tool_call_count"] == 100
    assert summary["edit_tool_count"] == 50
    assert summary["first_error"]["message"] == "first failure"
    assert summary["last_error"]["message"] == "last failure"
    assert summary["final_step_finish_reason"] == "stop"

    # selected model recorded.
    assert (
        manifest["opencode_model"]
        == "inference-hub/aws/anthropic/bedrock-claude-opus-4-8"
    )


def test_records_original_and_effective_returncode_on_no_change(
    tmp_path: Path,
) -> None:
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=False,  # clean tree -> guard forces rc=1
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    # Guard fires: effective rc is non-zero so the task is retried.
    assert result.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["returncode"] == 1
    assert manifest["original_opencode_rc"] == 0
    assert manifest["status"] == "incomplete"

    findings = _findings_by_kind(manifest)
    repo = findings["repo_change_summary"]
    assert repo["pushed"] is False
    assert repo["files_changed"] == []
    # No-change diagnostics present.
    assert "git_status" in repo
    assert "cwd" in repo
    assert repo["branch"]


def test_push_succeeds_but_pr_open_fails_marks_run_failed(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=2,  # PR open fails
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode != 0, (
        "a successful push with a failed PR open must fail the run; "
        "stdout=%s stderr=%s" % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["returncode"] != 0
    findings = _findings_by_kind(manifest)
    repo = findings["repo_change_summary"]
    assert repo["pushed"] is True
    assert repo.get("pr_opened") is False


def test_successful_push_and_pr_records_pr_and_full_files(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode == 0, (
        "stdout=%s stderr=%s" % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["returncode"] == 0
    findings = _findings_by_kind(manifest)
    repo = findings["repo_change_summary"]
    assert repo["pushed"] is True
    assert repo.get("pr_opened") is True
    # fake git diff returns two files; full list preserved.
    assert repo["files_changed"] == ["a.txt", "b.txt"]


def test_signed_manifest_with_rich_findings_passes_review_gate(
    tmp_path: Path,
) -> None:
    """The enriched manifest (head/tail slices, event summary, etc.) must
    still sign + verify under the attestation key and pass the review
    readiness gate. Guards canonical-JSON drift between the bash
    json.dump heredoc and mac.models.json_dumps for the larger payload."""
    cp = ControlPlane.in_memory()
    machine = cp.register_machine(
        "opencode-host", resources={"cpu": 4, "memory_gb": 8}
    )
    worker = cp.register_agent(
        machine.id, "mac-worker-python-coder-opencode", capabilities=["python", "ops"]
    )
    attestation_key = getattr(worker, "attestation_key", None)
    assert attestation_key

    task = cp.create_task(
        "Build a widget", required_capabilities=["ops"], metadata={}
    )
    cp.claim_task(task.id, worker.id)
    cp.start_task(task.id, worker.id)

    bindir = tmp_path / "bin"
    lines = [json.dumps({"type": "tool", "name": "edit"}) for _ in range(10)]
    lines.append(json.dumps({"type": "step_finish", "reason": "stop"}))
    stdout = "\n".join(lines) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_stderr="some stderr noise\n",
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id=task.id,
        agent_id=worker.id,
        extra_env={"MAC_AGENT_ATTESTATION_KEY": attestation_key},
    )
    assert result.returncode == 0, (
        "stdout=%s stderr=%s" % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["signed_by"] == worker.id
    assert manifest["signature"].startswith("v1:")

    from mac.services import verify_verification_manifest_signature

    assert verify_verification_manifest_signature(
        attestation_key, manifest, manifest["signature"]
    ), "bash canonical form must agree with json_dumps for rich manifest"

    from mac.models import TaskState

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

