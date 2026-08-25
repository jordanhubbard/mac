from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SIDECAR = ROOT / "deploy/openclaw/curiosity-sidecar.py"


def run(tmp_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(SIDECAR),
            "--state-dir",
            str(tmp_path / "state"),
            "--workspace",
            str(tmp_path / "workspace"),
            "--agent-id",
            "agent_test",
            *args,
        ],
        text=True,
        capture_output=True,
        check=check,
        timeout=10,
    )


def test_candidate_is_quarantined_redacted_and_requires_explicit_approval(tmp_path: Path) -> None:
    submitted = json.loads(
        run(
            tmp_path,
            "submit",
            "--hypothesis",
            "This may be true",
            "--question",
            "What would disprove it?",
            "--test",
            "Run an A/B comparison",
            "--evidence",
            "trace github_pat_abcdefghijklmnopqrstuvwxyz123456",
            "--provenance",
            "task:task_123",
            "--counterevidence",
            "one contrary observation",
            "--unknown",
            "sample bias",
            "--mode",
            "angry-librarian",
        ).stdout
    )
    candidate_id = submitted["id"]
    assert submitted["status"] == "quarantined"
    assert submitted["redactions"] == 1
    assert "github_pat_" not in json.dumps(submitted)
    assert not (tmp_path / "workspace/memory/curiosity-approved").exists()

    denied = run(
        tmp_path,
        "approve",
        candidate_id,
        "--actor",
        "operator",
        "--reason",
        "evidence checked",
        check=False,
    )
    assert denied.returncode != 0
    assert "approval" in denied.stderr

    approved = json.loads(
        run(
            tmp_path,
            "approve",
            candidate_id,
            "--actor",
            "operator",
            "--reason",
            "evidence checked",
            "--approval-id",
            "task_approval_1",
        ).stdout
    )
    assert approved["status"] == "approved"
    memory = (tmp_path / f"workspace/memory/curiosity-approved/{candidate_id}.md").read_text(
        encoding="utf-8"
    )
    assert "task_approval_1" in memory
    assert "Run an A/B comparison" in memory
    verified = json.loads(run(tmp_path, "verify").stdout)
    assert verified == {
        "events": 2,
        "head_sha256": verified["head_sha256"],
        "valid": True,
    }


def test_abuse_frame_surfaces_false_equivalence_without_inventing_evidence(tmp_path: Path) -> None:
    value = json.loads(
        run(
            tmp_path,
            "abuse-frame",
            "--event",
            "A documented harmful act",
            "--comparison",
            "Both sides are described as equivalent",
            "--harmed-party",
            "affected person",
            "--evidence",
            "source:incident-report",
            "--unknown",
            "intent",
            "--power-asymmetry",
            "--responsibility-asymmetry",
            "--moral-injury",
        ).stdout
    )
    assert value["possible_false_equivalence"] is True
    assert value["moral_injury"] is True
    assert value["evidence"] == ["source:incident-report"]
    assert "never toward dehumanization" in value["protective_anger"]


def test_policy_exposes_shipped_principle(tmp_path: Path) -> None:
    output = run(tmp_path, "policy").stdout
    assert "endlessly curious" in output
    assert "ruthless toward bad data" in output
    assert "angry at abuse" in output
    assert "exacting about evidence" in output
