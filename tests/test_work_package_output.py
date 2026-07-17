from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.models import ValidationError
from mac.work_package_output import GitAttemptOutputVerifier


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_AUTHOR_NAME": "MAC Test",
            "GIT_AUTHOR_EMAIL": "mac@example.invalid",
            "GIT_COMMITTER_NAME": "MAC Test",
            "GIT_COMMITTER_EMAIL": "mac@example.invalid",
        },
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[dict[str, str], Path, str, str]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git("init", "--bare", str(remote))
    _git("init", str(work))
    _git("-C", str(work), "checkout", "-b", "main")
    (work / "src").mkdir()
    (work / "src" / "base.py").write_text("BASE = True\n", encoding="utf-8")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-m", "base")
    base = _git("-C", str(work), "rev-parse", "HEAD")
    _git("-C", str(work), "remote", "add", "origin", str(remote))
    _git("-C", str(work), "push", "origin", "main")
    (work / "src" / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    _git("-C", str(work), "add", ".")
    _git("-C", str(work), "commit", "-m", "attempt")
    ref = "refs/mac/attempts/package/e1/node/a1-lease"
    _git("-C", str(work), "push", "origin", "HEAD:%s" % ref)
    return {"id": "repo-1", "source": str(remote)}, work, base, ref


def _namespace(*, case_sensitive: bool = True) -> dict[str, object]:
    return {
        "status": "resolved",
        "case_sensitive": case_sensitive,
        "unicode_normalization": "NFC",
        "symlink_resolution": "resolved",
    }


def test_observes_exact_remote_attempt_and_actual_scope(tmp_path: Path) -> None:
    repository, _work, base, ref = _repository(tmp_path)

    observed = GitAttemptOutputVerifier().observe(
        repository,
        attempt_ref=ref,
        base_sha=base,
        declared_effects={"writes": ["src"], "exclusive": []},
        resource_namespace=_namespace(),
    )

    assert observed.repository_id == "repo-1"
    assert observed.base_sha == base
    assert observed.changed_paths == ("src/feature.py",)
    assert observed.head_sha != base
    assert observed.tree_digest.startswith("sha256:")
    assert observed.observed_effects_digest.startswith("sha256:")
    assert observed.changes[0].status == "A"


def test_rejects_changed_path_outside_declared_scope(tmp_path: Path) -> None:
    repository, _work, base, ref = _repository(tmp_path)

    with pytest.raises(ValidationError, match="outside the declared effects"):
        GitAttemptOutputVerifier().observe(
            repository,
            attempt_ref=ref,
            base_sha=base,
            declared_effects={"writes": ["docs"], "exclusive": []},
            resource_namespace=_namespace(),
        )


def test_rename_checks_both_source_and_destination_scope(tmp_path: Path) -> None:
    repository, work, _base, _ref = _repository(tmp_path)
    base = _git("-C", str(work), "rev-parse", "HEAD")
    (work / "other").mkdir()
    _git("-C", str(work), "mv", "src/feature.py", "other/feature.py")
    _git("-C", str(work), "commit", "-m", "rename outside scope")
    ref = "refs/mac/attempts/package/e1/rename/a2-lease"
    _git("-C", str(work), "push", "origin", "HEAD:%s" % ref)

    with pytest.raises(ValidationError, match="other/feature.py"):
        GitAttemptOutputVerifier().observe(
            repository,
            attempt_ref=ref,
            base_sha=base,
            declared_effects={"writes": ["src"], "exclusive": []},
            resource_namespace=_namespace(),
        )


def test_rejects_attempt_ref_that_does_not_descend_from_assignment_base(
    tmp_path: Path,
) -> None:
    repository, work, _base, _ref = _repository(tmp_path)
    unrelated = tmp_path / "unrelated"
    _git("init", str(unrelated))
    _git("-C", str(unrelated), "checkout", "-b", "other")
    (unrelated / "src").mkdir()
    (unrelated / "src" / "other.py").write_text("OTHER = True\n", encoding="utf-8")
    _git("-C", str(unrelated), "add", ".")
    _git("-C", str(unrelated), "commit", "-m", "unrelated")
    unrelated_base = _git("-C", str(unrelated), "rev-parse", "HEAD")
    _git("-C", str(unrelated), "remote", "add", "origin", repository["source"])
    _git("-C", str(unrelated), "push", "origin", "HEAD:refs/heads/other")
    ref = "refs/mac/attempts/package/e1/node/a3-lease"
    _git("-C", str(work), "push", "origin", "HEAD:%s" % ref)

    with pytest.raises(ValidationError, match="does not descend"):
        GitAttemptOutputVerifier().observe(
            repository,
            attempt_ref=ref,
            base_sha=unrelated_base,
            attempt_base_ref="refs/heads/other",
            declared_effects={"writes": ["src"], "exclusive": []},
            resource_namespace=_namespace(),
        )


def test_case_insensitive_namespace_applies_to_plan_and_observed_paths(
    tmp_path: Path,
) -> None:
    repository, _work, base, ref = _repository(tmp_path)

    observed = GitAttemptOutputVerifier().observe(
        repository,
        attempt_ref=ref,
        base_sha=base,
        declared_effects={"writes": ["SRC"], "exclusive": []},
        resource_namespace=_namespace(case_sensitive=False),
    )

    assert observed.changed_paths == ("src/feature.py",)


def test_unresolved_namespace_requires_conservative_repository_scope(
    tmp_path: Path,
) -> None:
    repository, _work, base, ref = _repository(tmp_path)

    with pytest.raises(ValidationError, match="unresolved resource namespace"):
        GitAttemptOutputVerifier().observe(
            repository,
            attempt_ref=ref,
            base_sha=base,
            declared_effects={"writes": ["src"], "exclusive": []},
            resource_namespace={"status": "unresolved"},
        )

    observed = GitAttemptOutputVerifier().observe(
        repository,
        attempt_ref=ref,
        base_sha=base,
        declared_effects={"writes": ["src"], "exclusive": ["repo:*"]},
        resource_namespace={"status": "unresolved"},
    )
    assert observed.changed_paths == ("src/feature.py",)


def test_rejects_non_protected_ref_before_repository_access(tmp_path: Path) -> None:
    verifier = GitAttemptOutputVerifier()
    with pytest.raises(ValidationError, match="protected"):
        verifier.observe(
            {"id": "repo-1", "source": str(tmp_path / "missing.git")},
            attempt_ref="refs/heads/main",
            base_sha="a" * 40,
            declared_effects={"writes": ["src"], "exclusive": []},
            resource_namespace=_namespace(),
        )
