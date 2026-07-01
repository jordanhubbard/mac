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


def _run_review(tmp_path: Path, event_text: str):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    opencode = fake_bin / "opencode"
    _write_exec(
        opencode,
        "#!/usr/bin/env sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo opencode-test; exit 0; fi\n"
        "cat <<'EOF'\n" + event_text + "\nEOF\n"
        "exit 0\n",
    )
    cfg = tmp_path / "opencode.json"
    cfg.write_text(
        json.dumps(
            {
                "model": "inference-hub/default-review-model",
                "agent": {
                    "review": {"model": "inference-hub/reviewer-model"}
                },
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
    (tmp_path / "task.json").write_text(
        json.dumps(
            {
                "task": {
                    "id": "review_rev_test",
                    "owner_agent_id": "reviewer",
                    "metadata": {
                        "review_context": {
                            "task_id": "task_test",
                            "review_id": "rev_test",
                            "executor_evidence_id": "ev_executor",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
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
    result = subprocess.run(["bash", str(SCRIPT)], env=env, text=True, capture_output=True, timeout=30)
    return result, json.loads(evidence.read_text(encoding="utf-8"))


def test_opencode_review_rejected_event_stream(tmp_path: Path) -> None:
    events = "\n".join(
        [
            json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Reviewing"}]}),
            json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "```json\n{\"verdict\":\"rejected\",\"summary\":\"Tests fail\",\"feedback\":\"Fix the contract test\",\"findings\":[{\"severity\":\"blocking\",\"message\":\"Test fails\"}]}\n```"}]}),
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
            json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Reviewing"}]}),
            json.dumps({"type": "message", "role": "assistant", "content": [{"type": "text", "text": "```json\n{\"verdict\":\"approved\",\"summary\":\"Looks good\",\"feedback\":\"\"}\n```"}]}),
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
