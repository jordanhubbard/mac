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
    task_metadata: Optional[dict] = None,
    seed_test_command: Optional[str] = "pytest",
    test_rc: int = 0,
    test_output: str = "1 passed in 0.01s\n",
    seed_lint: bool = False,
    lint_rc: int = 0,
) -> None:
    """Create fake opencode/git/curl/mac/python helpers on PATH.

    The fakes are intentionally dumb: ``opencode run`` prints the canned
    stdout/stderr and exits with ``opencode_rc``; ``git`` simulates a
    clone (creating a working tree + optional change) and a push; ``mac
    pull-request open`` returns canned PR JSON or fails.

    The mandatory pre-push gate runs lint + tests against the cloned
    working tree before the push. To keep the fakes hermetic, the fake
    ``opencode run`` seeds a ``pyproject.toml`` (so ``pytest`` is the
    detected test command) and a fake ``pytest`` binary is placed on PATH
    that exits with ``test_rc`` and prints ``test_output``. Set
    ``seed_test_command=None`` to simulate a repo where no test command can
    be detected (the gate must then block the push).
    """
    bindir.mkdir(parents=True, exist_ok=True)

    # The cloned working tree lives at /tmp/work-${MAC_TASK_ID}; the fake
    # opencode (which cd's into it) seeds the test/lint markers there so the
    # gate's detection + execution exercise the real code path.
    seed_lines = ["#!/usr/bin/env bash\n"]
    if seed_test_command == "pytest":
        seed_lines.append('printf "[project]\\nname = \\"x\\"\\n" > pyproject.toml\n')
    if seed_lint:
        # Add a ruff section so the lint detector picks ruff.
        seed_lines.append('printf "[tool.ruff]\\n" >> pyproject.toml\n')

    # opencode: only `run` matters; `--version` returns a banner.
    oc_out = bindir / "_opencode_stdout.txt"
    oc_out.write_text(opencode_stdout)
    oc_err = bindir / "_opencode_stderr.txt"
    oc_err.write_text(opencode_stderr)
    seed_block = "".join(seed_lines[1:])  # drop the shebang line for embedding
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        f"{seed_block}"
        f'  cat "{oc_out}"\n'
        f'  cat "{oc_err}" >&2\n'
        f'  exit {opencode_rc}\n'
        "fi\n"
        "exit 0\n",
    )

    # Fake pytest: emits canned output and exits with test_rc.
    _write_exec(
        bindir / "pytest",
        "#!/usr/bin/env bash\n"
        f"printf '%s' {json.dumps(test_output)}\n"
        f"exit {test_rc}\n",
    )
    # Fake ruff: lint detector picks this when seed_lint is set.
    _write_exec(
        bindir / "ruff",
        "#!/usr/bin/env bash\n"
        'echo "ruff check output"\n'
        f"exit {lint_rc}\n",
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
    metadata = {
        "origin": {
            "repository_url": "https://gitea.omv.example/org/repo.git",
            "default_branch": "main",
        }
    }
    if task_metadata:
        metadata.update(task_metadata)
    task_json = json.dumps(
        {
            "task": {
                "title": "Do a thing",
                "description": "edit files",
                "project": "demo",
                "metadata": metadata,
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
    # Build a JSON-lines stream using the REAL opencode event shapes:
    # `tool_use` events carry the tool name + status + error under
    # `part.state`; `step_finish` carries its reason under `part.reason`.
    # (See evidence ev_23c0286d: 35 tool_use events, reason under part.)
    def tool_use(name, status="completed", error=None):
        state = {"status": status, "input": {}}
        if error is not None:
            state["error"] = error
        return json.dumps(
            {"type": "tool_use", "part": {"type": "tool", "tool": name, "state": state}}
        )

    def step_finish(reason):
        return json.dumps({"type": "step_finish", "part": {"reason": reason}})

    lines: List[str] = []
    lines.append(json.dumps({"type": "step_start", "part": {}}))
    # one tool that errored (first error), then many ok tools, then a
    # final errored tool (last error).
    lines.append(tool_use("bash", status="error", error="first failure"))
    lines.append(step_finish("tool-calls"))
    for _ in range(50):
        lines.append(tool_use("edit"))
        lines.append(tool_use("read"))
    lines.append(tool_use("bash", status="error", error="last failure"))
    lines.append(step_finish("stop"))
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

    # event summary: must parse the REAL `tool_use` shape, not synthetic.
    summary = findings["opencode_event_summary"]
    assert summary["total_lines"] >= len(lines)
    assert summary["parseable_json_lines"] >= len(lines)
    counts = summary["event_counts"]
    assert counts.get("tool_use") == 102  # 50 edit + 50 read + 2 bash
    assert counts.get("step_finish") == 2
    assert summary["tool_call_count"] == 102
    assert summary["edit_tool_count"] == 50
    # errors are nested under part.state.error.
    assert "first failure" in json.dumps(summary["first_error"])
    assert "last failure" in json.dumps(summary["last_error"])
    # finish reason is nested under part.reason.
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


def _evidence_items(manifest: dict) -> Dict[int, dict]:
    """Index the required numbered evidence items (Lint/Format=1, Tests=2,
    Push/Test Failures=3, MR=4) by their `item` number."""
    out: Dict[int, dict] = {}
    for finding in manifest.get("findings", []):
        if finding.get("kind") == "evidence_item":
            out[int(finding["item"])] = finding
    return out


# --- Mandatory pre-push test gate --------------------------------------


def test_gate_blocks_push_and_mr_when_tests_fail(tmp_path: Path) -> None:
    """A deliberate test failure must STOP the run: no push, no MR, and the
    task is routed to needs_review (non-zero rc) with full test evidence."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command="pytest",
        test_rc=1,  # tests FAIL
        test_output="FAILED tests/test_x.py::test_one - assert 1 == 2\n1 failed\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    # Failing tests must fail the run so it is not submitted as complete.
    assert result.returncode != 0, (
        "test failure must block; stdout=%s" % result.stdout
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["returncode"] != 0
    assert manifest["gate_blocked"] is True
    assert manifest["gate_verdict"] == "fail"

    # No push, no MR.
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False
    assert repo.get("pr_opened") is False

    items = _evidence_items(manifest)
    # Tests item (2) records the failing command + output.
    assert items[2]["label"] == "Tests"
    assert items[2]["status"] == "fail"
    assert items[2]["command"] == "pytest"
    # Item 3 is "Test Failures" (NOT Push) and carries full output + a fix.
    assert items[3]["label"] == "Test Failures"
    assert "FAILED" in items[3]["output"]
    assert any("test_one" in f for f in items[3]["failing_tests"])
    assert items[3]["suggested_fix"]
    # Push (3 as Push) and MR (4) items must be absent on failure.
    assert "MR" not in {it.get("label") for it in items.values()}


def test_gate_blocks_when_no_test_command_detected(tmp_path: Path) -> None:
    """No detectable test command must NOT silently skip — the gate blocks
    and reports that manual verification is required."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command=None,  # repo has no detectable test command
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_blocked"] is True
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False
    assert repo.get("pr_opened") is False

    items = _evidence_items(manifest)
    assert items[2]["status"] == "not_detected"
    assert "could not detect test command" in items[3]["reason"]


def test_gate_evidence_items_always_present_on_success(tmp_path: Path) -> None:
    """A passing coding task must always carry Lint/Format + Tests + Push +
    MR evidence items, and lint auto-fix must be attempted/recorded."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command="pytest",
        test_rc=0,
        seed_lint=True,
        lint_rc=0,
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode == 0, "stdout=%s" % result.stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "pass"
    assert manifest["gate_blocked"] is False

    items = _evidence_items(manifest)
    # 1 Lint/Format, 2 Tests, 3 Push, 4 MR.
    assert items[1]["label"] == "Lint/Format"
    assert items[1]["command"] == "ruff check ."
    assert items[1]["status"] == "pass"
    assert items[2]["label"] == "Tests"
    assert items[2]["status"] == "pass"
    assert items[2]["command"] == "pytest"
    assert items[3]["label"] == "Push"
    assert items[3]["branch"]
    assert items[3]["commit_sha"]
    assert items[4]["label"] == "MR"
    assert items[4]["mr_url"] == "https://example.test/mr/1"

    # The gate check is recorded for reviewers.
    checks = {c["name"]: c for c in manifest["checks"]}
    assert checks["pre_push_test_gate"]["status"] == "pass"
    assert checks["lint_format"]["status"] == "pass"


def test_gate_lint_autofix_attempted_on_failure_but_does_not_block(
    tmp_path: Path,
) -> None:
    """Lint failures must trigger an auto-fix attempt and be recorded, but
    must NOT block the gate — tests are the hard gate."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command="pytest",
        test_rc=0,
        seed_lint=True,
        lint_rc=1,  # lint fails -> auto-fix attempted
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    # Lint failure does NOT block: tests pass, so push proceeds.
    assert result.returncode == 0, "stdout=%s" % result.stdout
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = _evidence_items(manifest)
    assert items[1]["label"] == "Lint/Format"
    assert items[1]["auto_fixed"] is True  # --fix was attempted
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is True


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


# --- Fix #1: capture work the agent committed to its own branch ---------


def _git(args: List[str], cwd: Path, env: Optional[Dict[str, str]] = None) -> str:
    full_env = dict(os.environ)
    full_env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for real-clone test"
)
def test_agent_committed_branch_is_pushed_to_task_branch(tmp_path: Path) -> None:
    """The real production failure: an agentic model uses its bash tool to
    `git checkout -b <its-own-branch>` + commit, leaving the working tree
    clean on a branch the script never inspects. The executor must detect
    that branch and push its work to the task branch on origin, instead of
    declaring 'no file changes'."""
    # Real bare remote + a seed commit on main.
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main", str(seed)], cwd=tmp_path)
    (seed / "a.txt").write_text("original\n")
    # A pyproject so the pre-push gate detects `pytest` as the test command.
    (seed / "pyproject.toml").write_text('[project]\nname = "x"\n')
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed"], cwd=seed)
    _git(["remote", "add", "origin", str(remote)], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)

    repo_url = "file://%s" % remote
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)

    # Fake pytest on PATH: the pre-push gate runs it before pushing; exit 0
    # so the gate passes and the agent-committed branch is pushed.
    _write_exec(
        bindir / "pytest",
        "#!/usr/bin/env bash\necho '1 passed'\nexit 0\n",
    )

    # fake opencode: act like the agent — checkout its OWN branch, edit a
    # file, commit. Leaves the script's checked-out branch + tree clean.
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode test"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        "  git checkout -b feature/agent-branch >/dev/null 2>&1\n"
        '  printf "agent change\\n" >> a.txt\n'
        "  git add -A >/dev/null 2>&1\n"
        '  git -c user.name=agent -c user.email=a@a commit -m "agent work" >/dev/null 2>&1\n'
        '  echo \'{"type":"step_finish","part":{"reason":"stop"}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    task_json = json.dumps(
        {
            "task": {
                "title": "Edit a.txt",
                "description": "append a line",
                "project": "demo",
                "metadata": {
                    "origin": {
                        "repository_url": repo_url,
                        "default_branch": "main",
                    }
                },
            }
        }
    )
    _write_exec(
        bindir / "curl",
        "#!/usr/bin/env bash\n"
        'url="${@: -1}"\n'
        'case "$url" in\n'
        f'  *"/tasks/"*) cat <<\'JSON\'\n{task_json}\nJSON\n;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )
    _write_exec(
        bindir / "mac",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "pull-request" ] && [ "$2" = "open" ]; then\n'
        '  echo \'{"url":"https://example.test/mr/1","number":1}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )

    task_id = "task_agentbranch"
    manifest_path = tmp_path / "mac-evidence.json"
    # NOTE: do NOT shadow the real `git` — the script must use real git so
    # the clone/checkout/branch-detection/push all run for real. Only
    # opencode/curl/mac are faked.
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id=task_id,
    )
    assert manifest_path.exists(), (
        "manifest missing; stdout=%s stderr=%s" % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings = _findings_by_kind(manifest)
    repo = findings["repo_change_summary"]

    expected_branch = "mac/mac-worker-python-coder-opencode/%s" % task_id
    assert repo["pushed"] is True, (
        "agent-committed branch must be pushed; repo=%s stdout=%s"
        % (repo, result.stdout)
    )
    assert manifest["returncode"] == 0, (
        "run with real agent work must succeed; stdout=%s" % result.stdout
    )
    # The work must actually exist on the task branch in the REMOTE.
    remote_branches = _git(["branch", "--format=%(refname:short)"], cwd=remote)
    assert expected_branch in remote_branches.split(), (
        "task branch missing on remote; have=%r" % remote_branches
    )
    # And it must contain the agent's change.
    show = _git(["show", "%s:a.txt" % expected_branch], cwd=remote)
    assert "agent change" in show, "agent's commit not on the pushed branch"


def test_review_feedback_is_included_in_prompt_with_shell_safety(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    captured = bindir / "_opencode_args.txt"
    _make_fake_bin(
        bindir,
        opencode_stdout=json.dumps({"type": "step_finish", "part": {"reason": "stop"}}) + "\n",
        make_change=False,
        task_metadata={
            "review_feedback": {
                "latest": {
                    "review_id": "rev_1",
                    "verdict_evidence_id": "ev_v",
                    "summary": "Needs fix",
                    "feedback": "Do not execute $(touch /tmp/pwned); fix tests",
                    "findings": [{"severity": "blocking", "message": "bad `command`; use quotes"}],
                }
            }
        },
    )
    # Override fake opencode to capture the prompt argument exactly.
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        f'printf "%s\\n" "$@" > {captured}\n'
        "printf '%s\\n' '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}'\n"
        "exit 0\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode != 127
    prompt = captured.read_text(encoding="utf-8")
    assert "Previous review feedback" in prompt
    assert "Do not execute $(touch /tmp/pwned); fix tests" in prompt
    assert not Path("/tmp/pwned").exists()


