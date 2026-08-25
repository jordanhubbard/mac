from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "codex-runner" / "mac-task-executor-opencode-review"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_review(
    tmp_path: Path,
    event_text: str,
    *,
    blind: bool = False,
    discovery_event_text: str = "",
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    opencode = fake_bin / "opencode"
    _write_exec(
        opencode,
        "#!/usr/bin/env sh\n"
        'if [ "$1" = "--version" ]; then echo opencode-test; exit 0; fi\n'
        "if printf '%s' \"$*\" | grep -q 'evidence-withheld discovery'; then\n"
        '  if [ -e "$MAC_TASK_WORKSPACE/executor-evidence.json" ]; then exit 9; fi\n'
        "  cat <<'DISCOVERY_EOF'\n" + discovery_event_text + "\nDISCOVERY_EOF\n"
        "  exit 0\n"
        "fi\n"
        "cat <<'EOF'\n" + event_text + "\nEOF\n"
        "exit 0\n",
    )
    cfg = tmp_path / "opencode.json"
    cfg.write_text(
        json.dumps(
            {
                "model": "inference-hub/default-review-model",
                "agent": {"review": {"model": "inference-hub/reviewer-model"}},
            }
        ),
        encoding="utf-8",
    )
    evidence = tmp_path / "mac-evidence.json"
    (tmp_path / "executor-evidence.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "verification": {
                        "schema": "mac.worker_evidence.v1",
                        "status": "complete",
                        "evidence_type": "operator_result",
                        "summary": "executor result",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    metadata = {
        "review_context": {
            "task_id": "task_test",
            "review_id": "rev_test",
            "executor_evidence_id": "ev_executor",
        }
    }
    if blind:
        metadata["review_experiment"] = {
            "schema": "mac.review_experiment.v1",
            "experiment_id": "exp-opencode-blind",
            "arm": "blind",
            "blind": True,
        }
    original_task = {
        "id": "task_test",
        "title": "test task",
        "description": "exercise review",
    }
    (tmp_path / "executor-task.json").write_text(json.dumps(original_task), encoding="utf-8")
    (tmp_path / "task.json").write_text(
        json.dumps(
            {
                "task": {
                    "id": "review_rev_test",
                    "owner_agent_id": "reviewer",
                    "metadata": metadata,
                }
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    # Ensure the subprocess's python3 can import `mac`.  In production the
    # container installs mac system-wide; in the dev / sandbox environment
    # mac is installed into a project-local .venv.  Prepend both the venv
    # bin directory (so `python3` resolves to the venv interpreter) and the
    # package source root to PYTHONPATH so either path works.
    venv_bin = REPO_ROOT / ".venv" / "bin"
    src_root = REPO_ROOT / "src"
    existing_pythonpath = env.get("PYTHONPATH", "")
    extra_pythonpath = str(src_root)
    env["PYTHONPATH"] = (
        extra_pythonpath + os.pathsep + existing_pythonpath
        if existing_pythonpath
        else extra_pythonpath
    )
    extra_path = str(fake_bin)
    if venv_bin.is_dir():
        extra_path = extra_path + os.pathsep + str(venv_bin)
    env.update(
        {
            "PATH": extra_path + os.pathsep + env.get("PATH", ""),
            "MAC_TASK_ID": "task_test",
            "MAC_REVIEW_ID": "rev_test",
            "MAC_REVIEW_TARGET_EVIDENCE_ID": "ev_executor",
            "MAC_AGENT_ID": "reviewer",
            "MAC_OPENCODE_CONFIG_PATH": str(cfg),
            "MAC_TASK_EVIDENCE_MANIFEST_PATH": str(evidence),
            "MAC_TASK_WORKSPACE": str(tmp_path),
            "MAC_TASK_FILE": str(tmp_path / "task.json"),
            "MAC_AGENT_ATTESTATION_KEY": "reviewer-secret",
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=30
    )
    return result, json.loads(evidence.read_text(encoding="utf-8"))


def test_opencode_review_rejected_event_stream(tmp_path: Path) -> None:
    events = "\n".join(
        [
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Reviewing"}],
                }
            ),
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": '```json\n{"verdict":"rejected","summary":"Tests fail","feedback":"Fix the contract test","findings":[{"severity":"blocking","message":"Test fails"}]}\n```',
                        }
                    ],
                }
            ),
        ]
    )
    result, manifest = _run_review(tmp_path, events)
    assert result.returncode == 0, result.stderr + result.stdout
    assert manifest["verdict"] == "rejected"
    assert manifest["status"] == "complete"
    assert manifest["returncode"] == 0
    assert manifest["feedback"] == "Fix the contract test"
    assert manifest["llm_model"] == "inference-hub/reviewer-model"
    assert manifest["llm"]["model"] == "inference-hub/reviewer-model"
    assert manifest["worktree_digest"].startswith("sha256:")
    assert manifest["signed_by"] == "reviewer"


def test_opencode_review_approved_event_stream(tmp_path: Path) -> None:
    events = "\n".join(
        [
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Reviewing"}],
                }
            ),
            json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": '```json\n{"verdict":"approved","summary":"Looks good","feedback":""}\n```',
                        }
                    ],
                }
            ),
        ]
    )
    result, manifest = _run_review(tmp_path, events)
    assert result.returncode == 0, result.stderr + result.stdout
    assert manifest["verdict"] == "approved"
    assert manifest["status"] == "complete"
    assert manifest["returncode"] == 0
    assert manifest["result"] == "review_completed"
    assert manifest["llm_model"] == "inference-hub/reviewer-model"
    assert manifest["worktree_digest"].startswith("sha256:")


def test_opencode_review_blind_discovery_withholds_then_restores_evidence(
    tmp_path: Path,
) -> None:
    discovery_events = json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": '```json\n{"findings":[{"severity":"medium","summary":"Independent concern"}],"no_findings_reason":""}\n```',
                }
            ],
        }
    )
    final_events = json.dumps(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": '```json\n{"verdict":"approved","summary":"Concern resolved","feedback":"","findings":[]}\n```',
                }
            ],
        }
    )

    result, manifest = _run_review(
        tmp_path,
        final_events,
        blind=True,
        discovery_event_text=discovery_events,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (tmp_path / "executor-evidence.json").is_file()
    assert manifest["review_experiment"]["protocol"]["protocol_compliant"] is True
    assert manifest["independent_findings"] == [
        {"severity": "medium", "summary": "Independent concern"}
    ]
    protocol = json.loads((tmp_path / "review-protocol.json").read_text(encoding="utf-8"))
    assert protocol["executor_evidence_hidden"] is True
    assert protocol["independent_findings_count"] == 1
