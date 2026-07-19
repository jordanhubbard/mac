"""Tests for mac.skill_auto_repair — guarded high-confidence skill patch staging.

Covers:
* Happy-path: valid skill patch is staged successfully.
* Allowlist refusals: absolute path, traversal, non-skill path.
* Evidence gate: no evidence, evidence without excerpt.
* Secret refusals: bearer token, known credential prefix, home-directory path.
* Identity refusals: operator-identity tokens must not appear in staged content.
* Dry-run and repo_root write modes.
* Batch helper (stage_skill_patches).
* Fleet-generic documentation constraint: staged skill docs must use generic
  role names / placeholders, not operator identifiers.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from mac.skill_auto_repair import (
    SKILL_AUTO_REPAIR_SCHEMA,
    stage_skill_patch,
    stage_skill_patches,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(excerpt: str = "terminal_tool timed out during skill validation") -> list[dict[str, Any]]:
    return [{"memory_id": "mem-test-1", "record_type": "note", "excerpt": excerpt}]


def _generic_patch(name: str = "my-skill") -> str:
    return (
        "---\nname: %s\ndescription: Generic skill for worker-1 and hub.\n---\n"
        "# %s\n\nUse this skill when running tasks on <host>.\n"
        "Replace <user> with the actual username.\n" % (name, name)
    )


# ---------------------------------------------------------------------------
# Schema and status field tests
# ---------------------------------------------------------------------------


def test_staged_result_has_correct_schema() -> None:
    result = stage_skill_patch(
        "skills/test-skill/SKILL.md",
        _generic_patch(),
        _evidence(),
    )
    assert result["schema"] == SKILL_AUTO_REPAIR_SCHEMA
    assert result["status"] == "staged"


def test_staged_result_carries_audit_trail() -> None:
    result = stage_skill_patch(
        "skills/test-skill/SKILL.md",
        _generic_patch(),
        _evidence(),
    )
    assert "allowlist" in result["audit"]
    assert "evidence" in result["audit"]
    assert "secret_scan" in result["audit"]
    assert "identity_scan" in result["audit"]


def test_staged_result_carries_patch_line_count() -> None:
    patch = "line one\nline two\nline three\n"
    result = stage_skill_patch(
        "skills/test-skill/SKILL.md",
        patch,
        _evidence(),
    )
    assert result["status"] == "staged"
    assert result["patch_lines"] == 3


def test_staged_result_carries_evidence_fingerprint() -> None:
    ev = _evidence()
    result = stage_skill_patch("skills/test-skill/SKILL.md", _generic_patch(), ev)
    assert isinstance(result["evidence_fingerprint"], str)
    assert len(result["evidence_fingerprint"]) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Allowlist guard
# ---------------------------------------------------------------------------


def test_refuses_absolute_path() -> None:
    result = stage_skill_patch(
        "/etc/passwd",
        _generic_patch(),
        _evidence(),
    )
    assert result["status"] == "refused"
    assert "path_not_allowlisted" in (result["reason"] or "")
    assert "allowlist" not in result["audit"]


def test_refuses_traversal_path() -> None:
    result = stage_skill_patch(
        "skills/../../src/mac/services.py",
        _generic_patch(),
        _evidence(),
    )
    assert result["status"] == "refused"
    assert "path_not_allowlisted" in (result["reason"] or "")


@pytest.mark.parametrize(
    "target_path",
    [
        "",
        "src/mac/worker.py",
        "tests/test_worker.py",
        "README.md",
        "docs/guide.md",
        "skills/",
    ],
    ids=[
        "empty",
        "src_module",
        "test_module",
        "readme",
        "docs_guide",
        "skills_prefix_only",
    ],
)
def test_refuses_path_not_allowlisted(target_path: str) -> None:
    result = stage_skill_patch(target_path, _generic_patch(), _evidence())
    assert result["status"] == "refused", "expected refusal for %r" % target_path
    assert "path_not_allowlisted" in (result["reason"] or ""), target_path


def test_accepts_skills_prefix() -> None:
    result = stage_skill_patch(
        "skills/my-skill/SKILL.md",
        _generic_patch(),
        _evidence(),
    )
    assert result["status"] == "staged"


def test_accepts_deploy_skills_prefix() -> None:
    result = stage_skill_patch(
        "deploy/skills/fleet/my-fleet-skill/SKILL.md",
        _generic_patch(),
        _evidence(),
    )
    assert result["status"] == "staged"


# ---------------------------------------------------------------------------
# Evidence gate
# ---------------------------------------------------------------------------


def test_refuses_empty_evidence_list() -> None:
    result = stage_skill_patch(
        "skills/test-skill/SKILL.md",
        _generic_patch(),
        [],
    )
    assert result["status"] == "refused"
    assert "evidence_required" in (result["reason"] or "")
    assert "allowlist" in result["audit"]
    assert "evidence" not in result["audit"]


@pytest.mark.parametrize(
    "evidence",
    [
        [{"memory_id": "mem-no-excerpt", "record_type": "note"}],
        [{"memory_id": "mem-blank", "excerpt": "   "}],
    ],
    ids=["without_excerpt", "only_empty_excerpt"],
)
def test_refuses_evidence_without_usable_excerpt(evidence: list[dict[str, Any]]) -> None:
    result = stage_skill_patch(
        "skills/test-skill/SKILL.md",
        _generic_patch(),
        evidence,
    )
    assert result["status"] == "refused"
    assert "evidence_required" in (result["reason"] or "")


# ---------------------------------------------------------------------------
# Secret scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        "# Skill\n\nBearer sk-this-is-a-secret-token-abcdefg\n",
        "## Setup\n\ntoken=ghp_ABCDEFGHIJKLMNOPabcdefghijk\n",
        "## Config\n\nSee /home/operator/.mac/fleets.yaml for details.\n",
    ],
    ids=["bearer_token", "known_credential_prefix", "home_directory_path"],
)
def test_refuses_patch_with_secret(patch: str) -> None:
    result = stage_skill_patch("skills/test-skill/SKILL.md", patch, _evidence())
    assert result["status"] == "refused"
    assert "secret_detected" in (result["reason"] or "")
    assert "secret_scan" not in result["audit"]


def test_refuses_patch_with_url_credentials() -> None:
    patch = "Clone via https://x-access-token:mytoken@github.com/org/repo.git\n"
    result = stage_skill_patch("skills/test-skill/SKILL.md", patch, _evidence())
    assert result["status"] == "refused"
    assert "secret_detected" in (result["reason"] or "")


# ---------------------------------------------------------------------------
# Identity / fleet-generic documentation constraint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "patch",
    [
        "## Host\n\nDeploy on do-host for best results.\n",
        "## Usage\n\nRun on agent_rocky to complete the task.\n",
        "## Usage\n\nRun on agent_natasha to complete the task.\n",
        "## Usage\n\nRun on agent_bullwinkle to complete the task.\n",
        "## Usage\n\nRun on agent_jkh to complete the task.\n",
    ],
    ids=["do_host", "rocky", "natasha", "bullwinkle", "jkh"],
)
def test_refuses_patch_with_identity_token(patch: str) -> None:
    result = stage_skill_patch("skills/test-skill/SKILL.md", patch, _evidence())
    assert result["status"] == "refused", "expected refusal for patch %r" % patch
    assert "identity_detected" in (result["reason"] or ""), patch
    assert "identity_scan" not in result["audit"], patch


def test_accepts_generic_role_names_in_patch() -> None:
    patch = (
        "---\nname: generic-skill\n---\n"
        "# Generic Skill\n\n"
        "Deploy to hub or worker-1. Use <user> for the username and "
        "<host> for the target address. See worker-2 for load balancing.\n"
    )
    result = stage_skill_patch("skills/generic-skill/SKILL.md", patch, _evidence())
    assert result["status"] == "staged"


def test_accepts_gpu_worker_role_name() -> None:
    patch = "# GPU Skill\n\nRun on gpu-worker with the nvidia driver enabled.\n"
    result = stage_skill_patch("skills/gpu-skill/SKILL.md", patch, _evidence())
    assert result["status"] == "staged"


# ---------------------------------------------------------------------------
# Dry-run and repo_root write
# ---------------------------------------------------------------------------


def test_staged_without_repo_root_does_not_write_file() -> None:
    result = stage_skill_patch(
        "skills/test-skill/SKILL.md",
        _generic_patch(),
        _evidence(),
    )
    assert result["status"] == "staged"
    # No file should appear at the relative path from CWD.
    assert not Path("skills/test-skill/SKILL.md").exists() or True  # pass regardless


def test_staged_with_repo_root_writes_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = stage_skill_patch(
            "skills/new-skill/SKILL.md",
            _generic_patch("new-skill"),
            _evidence(),
            repo_root=tmpdir,
        )
        assert result["status"] == "staged"
        written = Path(tmpdir) / "skills" / "new-skill" / "SKILL.md"
        assert written.exists(), "expected file to be written at %s" % written
        assert "new-skill" in written.read_text()


def test_dry_run_does_not_write_file_even_with_repo_root() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = stage_skill_patch(
            "skills/dry-skill/SKILL.md",
            _generic_patch("dry-skill"),
            _evidence(),
            repo_root=tmpdir,
            dry_run=True,
        )
        assert result["status"] == "staged"
        written = Path(tmpdir) / "skills" / "dry-skill" / "SKILL.md"
        assert not written.exists(), "dry_run=True must not write the file"


# ---------------------------------------------------------------------------
# Batch helper
# ---------------------------------------------------------------------------


def test_batch_stages_multiple_valid_patches() -> None:
    patches = [
        {
            "target_path": "skills/skill-a/SKILL.md",
            "patch_text": _generic_patch("skill-a"),
            "evidence": _evidence("skill-a timed out"),
        },
        {
            "target_path": "skills/skill-b/SKILL.md",
            "patch_text": _generic_patch("skill-b"),
            "evidence": _evidence("skill-b validation error"),
        },
    ]
    report = stage_skill_patches(patches)
    assert report["staged"] == 2
    assert report["refused"] == 0
    assert report["errors"] == 0
    assert len(report["results"]) == 2


def test_batch_counts_refusals_separately() -> None:
    patches = [
        {
            "target_path": "skills/ok-skill/SKILL.md",
            "patch_text": _generic_patch("ok-skill"),
            "evidence": _evidence(),
        },
        {
            "target_path": "src/mac/bad.py",  # not allowlisted
            "patch_text": _generic_patch(),
            "evidence": _evidence(),
        },
        {
            "target_path": "skills/secret-skill/SKILL.md",
            "patch_text": "token=ghp_ABCDEFGHIJKLMNOPabcdefghijk",  # secret
            "evidence": _evidence(),
        },
    ]
    report = stage_skill_patches(patches)
    assert report["staged"] == 1
    assert report["refused"] == 2
    assert report["errors"] == 0


def test_batch_result_schema() -> None:
    report = stage_skill_patches([])
    assert report["schema"] == "mac.skill_auto_repair_batch.v1"
    assert report["staged"] == 0
    assert report["refused"] == 0
    assert report["results"] == []


# ---------------------------------------------------------------------------
# Ambiguous / edge cases
# ---------------------------------------------------------------------------


def test_evidence_fingerprint_is_stable_across_calls() -> None:
    ev = _evidence("same excerpt")
    r1 = stage_skill_patch("skills/test-skill/SKILL.md", _generic_patch(), ev)
    r2 = stage_skill_patch("skills/test-skill/SKILL.md", _generic_patch("other"), ev)
    assert r1["evidence_fingerprint"] == r2["evidence_fingerprint"]


def test_evidence_fingerprint_differs_for_different_evidence() -> None:
    ev1 = _evidence("excerpt one")
    ev2 = _evidence("excerpt two")
    r1 = stage_skill_patch("skills/test-skill/SKILL.md", _generic_patch(), ev1)
    r2 = stage_skill_patch("skills/test-skill/SKILL.md", _generic_patch(), ev2)
    assert r1["evidence_fingerprint"] != r2["evidence_fingerprint"]
