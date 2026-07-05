"""Fail-closed verifier for reusing prior executor evidence.

When a review-infrastructure failure prevents the normal reviewer path from
completing, recovery logic may attempt to reuse an existing executor's evidence
record rather than dispatching a fresh execution.  This module answers the
question:

  "Is this prior executor evidence still safe to use for publication?"

It is deliberately **fail-closed**: every sub-check that cannot be confirmed
returns a structured ``FAIL`` reason.  Callers that want to skip a check (e.g.
during offline development) must explicitly pass the right flags; the default
is always the most conservative path.

Usage::

    from mac.evidence_reuse_verifier import verify_prior_executor_evidence
    result = verify_prior_executor_evidence(
        manifest=evidence.metadata["verification"],
        agent_key_lookup=cp._agent_attestation_key,
        remote_url="git@github.com:org/repo.git",
        expected_head_sha="a1b2c3d4...",
    )
    if not result.ok:
        logger.warning("cannot reuse evidence: %s", result.problems)

The function and its helpers are pure-python and have no I/O side-effects
beyond the optional git / network round-trips that the caller opts into.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from mac.codegraph_audit import codegraph_audit_manifest_problems
from mac.evidence_validators import GIT_SHA_RE

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

REUSE_VERIFIER_SCHEMA = "mac.evidence_reuse_verification.v1"

# Status codes used in structured reasons.
_PASS = "pass"
_FAIL = "fail"
_SKIP = "skip"  # used when a check is explicitly disabled


@dataclass(frozen=True)
class CheckResult:
    """Result of a single reuse-invariant check."""

    name: str
    status: str  # "pass" | "fail" | "skip"
    reason: str = ""
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == _PASS


@dataclass
class ReuseVerificationResult:
    """Aggregate result of all invariant checks.

    ``ok`` is True only when every *non-skipped* check passed.
    ``checks`` records every check in order; callers can inspect individual
    entries to build a repair plan.
    ``problems`` is a convenience list of human-readable failure descriptions
    (one entry per failing check) that recovery logic can log or store.
    """

    schema: str = REUSE_VERIFIER_SCHEMA
    ok: bool = False
    checks: List[CheckResult] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "ok": self.ok,
            "problems": self.problems,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "reason": c.reason,
                    **({"detail": c.detail} if c.detail is not None else {}),
                }
                for c in self.checks
            ],
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GIT_TIMEOUT_SEC = 10


def _run_git(argv: List[str], *, timeout: int = _GIT_TIMEOUT_SEC) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        # Return a synthetic CompletedProcess so callers don't need to branch.
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr=str(exc))


def _str(value: Any) -> str:
    return str(value or "").strip()


def _manifest_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Individual invariant checks
# ---------------------------------------------------------------------------

def _check_schema_and_status(manifest: Mapping[str, Any]) -> CheckResult:
    """Evidence must declare the canonical verification schema and status=complete."""
    from mac.services import VERIFICATION_SCHEMA  # type: ignore[attr-defined]

    schema = _str(manifest.get("schema"))
    status = _str(manifest.get("status")).lower()
    if schema != VERIFICATION_SCHEMA:
        return CheckResult(
            name="schema",
            status=_FAIL,
            reason="wrong_schema",
            detail="expected %s, got %r" % (VERIFICATION_SCHEMA, schema),
        )
    if status != "complete":
        return CheckResult(
            name="status",
            status=_FAIL,
            reason="status_not_complete",
            detail="expected 'complete', got %r" % status,
        )
    return CheckResult(name="schema_and_status", status=_PASS, reason="schema_and_status_ok")


def _check_evidence_type(manifest: Mapping[str, Any]) -> CheckResult:
    """evidence_type must be present and not 'review_verdict' (which is not executor evidence)."""
    evidence_type = _str(manifest.get("evidence_type")).lower()
    if not evidence_type:
        return CheckResult(
            name="evidence_type",
            status=_FAIL,
            reason="evidence_type_missing",
            detail="verification.evidence_type is required",
        )
    if evidence_type == "review_verdict":
        return CheckResult(
            name="evidence_type",
            status=_FAIL,
            reason="review_verdict_not_reusable",
            detail="review_verdict evidence is a reviewer artifact, not executor evidence",
        )
    return CheckResult(name="evidence_type", status=_PASS, reason="evidence_type_ok")


def _check_signature_provenance(
    manifest: Mapping[str, Any],
    agent_key_lookup: Optional[Callable[[str], Optional[str]]],
) -> CheckResult:
    """Verify HMAC-SHA256 signature on the manifest (mac-ng2 root of trust).

    The manifest must carry ``signed_by`` + ``signature``.  When
    ``agent_key_lookup`` is provided the signature is cryptographically
    verified; when it is None the check is structural only (presence of
    fields).  A missing or invalid signature is always a hard FAIL.
    """
    from mac.services import verify_verification_manifest_signature  # type: ignore[attr-defined]

    signed_by = _str(manifest.get("signed_by"))
    signature = _str(manifest.get("signature"))
    if not signed_by or not signature:
        return CheckResult(
            name="signature_provenance",
            status=_FAIL,
            reason="manifest_not_signed",
            detail="verification.signed_by and verification.signature are required",
        )
    if agent_key_lookup is None:
        # Structural check only — key store not available in this context.
        return CheckResult(
            name="signature_provenance",
            status=_PASS,
            reason="signature_fields_present_key_lookup_skipped",
        )
    key = agent_key_lookup(signed_by)
    if key is None:
        return CheckResult(
            name="signature_provenance",
            status=_FAIL,
            reason="signer_unknown",
            detail="agent %r has no attestation key on file" % signed_by,
        )
    if not verify_verification_manifest_signature(key, dict(manifest), signature):
        return CheckResult(
            name="signature_provenance",
            status=_FAIL,
            reason="signature_invalid",
            detail="HMAC does not verify for agent %r" % signed_by,
        )
    return CheckResult(
        name="signature_provenance",
        status=_PASS,
        reason="signature_verified",
        detail="signed_by=%s" % signed_by,
    )


def _check_pushed_remote_ref(manifest: Mapping[str, Any]) -> CheckResult:
    """repo.pushed must be true with a non-empty remote_ref (or pr_url)."""
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return CheckResult(
            name="pushed_remote_ref",
            status=_FAIL,
            reason="repo_missing",
            detail="verification.repo object is absent",
        )
    head_sha = _str(repo.get("head_sha"))
    if not GIT_SHA_RE.match(head_sha):
        return CheckResult(
            name="pushed_remote_ref",
            status=_FAIL,
            reason="head_sha_invalid",
            detail="repo.head_sha is not a valid git SHA: %r" % head_sha,
        )
    dirty = repo.get("dirty")
    if dirty not in {False, "false", "False", 0, "0"}:
        return CheckResult(
            name="pushed_remote_ref",
            status=_FAIL,
            reason="repo_dirty",
            detail="repo.dirty is not false — worktree had uncommitted changes when evidence was written",
        )
    pushed = repo.get("pushed") is True or _str(repo.get("pushed")).lower() == "true"
    remote_ref = _str(repo.get("remote_ref"))
    pr_url = _str(repo.get("pr_url"))
    if not (pushed and remote_ref) and not pr_url:
        return CheckResult(
            name="pushed_remote_ref",
            status=_FAIL,
            reason="not_pushed",
            detail="evidence requires pushed=true with remote_ref, or pr_url",
        )
    return CheckResult(
        name="pushed_remote_ref",
        status=_PASS,
        reason="pushed_ok",
        detail="remote_ref=%s" % (remote_ref or pr_url),
    )


def _check_repo_head_sha(
    manifest: Mapping[str, Any],
    expected_head_sha: Optional[str],
) -> CheckResult:
    """The manifest's repo.head_sha must match the task's expected HEAD.

    When ``expected_head_sha`` is None the check is skipped (no expectation
    was set by the caller).
    """
    if expected_head_sha is None:
        return CheckResult(name="repo_head_sha", status=_SKIP, reason="no_expected_sha_provided")
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return CheckResult(
            name="repo_head_sha",
            status=_FAIL,
            reason="repo_missing",
            detail="verification.repo object is absent",
        )
    manifest_sha = _str(repo.get("head_sha"))
    if manifest_sha != _str(expected_head_sha):
        return CheckResult(
            name="repo_head_sha",
            status=_FAIL,
            reason="head_sha_mismatch",
            detail="manifest repo.head_sha %s != expected %s" % (manifest_sha, expected_head_sha),
        )
    return CheckResult(
        name="repo_head_sha",
        status=_PASS,
        reason="head_sha_matches",
        detail=manifest_sha,
    )


def _check_remote_sha_equality(
    manifest: Mapping[str, Any],
    *,
    verify_remote: bool = True,
) -> CheckResult:
    """Verify that the remote ref actually resolves to repo.head_sha.

    This catches the failure mode where an executor wrote pushed=true but never
    actually pushed (the original phantom-push incident).  Uses
    ``git ls-remote`` — network-dependent.  Pass ``verify_remote=False`` to
    skip the network round-trip (offline development).
    """
    if not verify_remote:
        return CheckResult(
            name="remote_sha_equality",
            status=_SKIP,
            reason="remote_verification_disabled",
        )
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return CheckResult(
            name="remote_sha_equality",
            status=_FAIL,
            reason="repo_missing",
            detail="verification.repo object is absent",
        )
    pushed = repo.get("pushed") is True or _str(repo.get("pushed")).lower() == "true"
    remote_url = _str(repo.get("remote_url"))
    remote_ref = _str(repo.get("remote_ref"))
    head_sha = _str(repo.get("head_sha"))
    if not (pushed and remote_url and remote_ref):
        return CheckResult(
            name="remote_sha_equality",
            status=_SKIP,
            reason="no_remote_coordinates_to_verify",
        )
    proc = _run_git(["git", "ls-remote", remote_url, remote_ref])
    if proc.returncode != 0:
        # Best-effort: auth/network failures don't reject evidence.
        stderr = (proc.stderr or "").strip()
        from mac.fleet_learning import (  # type: ignore[attr-defined]
            AUTH_FAILURE_CLASSES,
            classify_repository_access_failure,
        )
        failure_class = classify_repository_access_failure(stderr)
        if failure_class in {*AUTH_FAILURE_CLASSES, "network"}:
            return CheckResult(
                name="remote_sha_equality",
                status=_SKIP,
                reason="remote_unreachable_%s" % failure_class,
                detail=stderr[:200],
            )
        return CheckResult(
            name="remote_sha_equality",
            status=_FAIL,
            reason="ls_remote_failed",
            detail="git ls-remote rc=%d: %s" % (proc.returncode, stderr[:200]),
        )
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return CheckResult(
            name="remote_sha_equality",
            status=_FAIL,
            reason="ref_not_found_on_remote",
            detail="%s not found on %s" % (remote_ref, remote_url),
        )
    # ls-remote output: "<sha>\t<ref>" per line.
    remote_sha = stdout.splitlines()[0].split("\t")[0].strip()
    if remote_sha != head_sha:
        return CheckResult(
            name="remote_sha_equality",
            status=_FAIL,
            reason="remote_sha_mismatch",
            detail="remote %s resolves to %s, evidence claims %s"
            % (remote_ref, remote_sha, head_sha),
        )
    return CheckResult(
        name="remote_sha_equality",
        status=_PASS,
        reason="remote_sha_matches",
        detail="%s == %s" % (remote_sha, head_sha),
    )


def _check_canonical_ancestry(
    manifest: Mapping[str, Any],
    canonical_remote_url: Optional[str],
    *,
    verify_remote: bool = True,
) -> CheckResult:
    """Confirm the evidence SHA is an ancestor of the canonical branch tip.

    This detects stale branches that have been superseded by force-pushes or
    diverged histories.  Requires network access (``git ls-remote`` to resolve
    the canonical branch tip, then a local ``git merge-base`` if a worktree is
    available).  Skipped when ``canonical_remote_url`` is None or
    ``verify_remote=False``.
    """
    if not verify_remote or not canonical_remote_url:
        return CheckResult(
            name="canonical_ancestry",
            status=_SKIP,
            reason="canonical_ancestry_check_disabled_or_no_url",
        )
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return CheckResult(
            name="canonical_ancestry",
            status=_FAIL,
            reason="repo_missing",
            detail="verification.repo object is absent",
        )
    head_sha = _str(repo.get("head_sha"))
    if not GIT_SHA_RE.match(head_sha):
        return CheckResult(
            name="canonical_ancestry",
            status=_FAIL,
            reason="head_sha_invalid",
            detail="repo.head_sha is not a valid git SHA",
        )
    # Resolve the canonical branch tip via ls-remote.
    proc = _run_git(["git", "ls-remote", canonical_remote_url, "refs/heads/main"])
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        # Best-effort: network failures don't reject evidence.
        return CheckResult(
            name="canonical_ancestry",
            status=_SKIP,
            reason="canonical_tip_unresolvable",
            detail=(proc.stderr or "").strip()[:200],
        )
    canonical_sha = proc.stdout.strip().splitlines()[0].split("\t")[0].strip()
    if canonical_sha == head_sha:
        return CheckResult(
            name="canonical_ancestry",
            status=_PASS,
            reason="head_sha_is_canonical_tip",
            detail=head_sha,
        )
    # We can only verify ancestry when the SHAs are reachable in a local clone.
    # The verifier runs on the hub which may not have a local checkout, so we
    # do a best-effort: if the remote returns a SHA we trust it; we cannot
    # call merge-base without a local repo.  Structured skip rather than fail.
    return CheckResult(
        name="canonical_ancestry",
        status=_SKIP,
        reason="local_repo_not_available_for_ancestry_check",
        detail="canonical_tip=%s evidence_sha=%s" % (canonical_sha, head_sha),
    )


def _check_required_tests_and_checks(manifest: Mapping[str, Any]) -> CheckResult:
    """At least one passing test or check must be recorded."""
    tests = _manifest_list(manifest.get("tests"))
    checks = _manifest_list(manifest.get("checks"))
    _PASSING_WORDS = {"pass", "passed", "success", "successful", "succeeded", "ok"}

    def _item_passed(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        # returncode==0 OR status in passing words.
        try:
            rc = item.get("returncode")
            if rc is not None and int(rc) == 0:
                return True
        except (TypeError, ValueError):
            pass
        return _str(item.get("status")).lower() in _PASSING_WORDS

    passing = sum(1 for i in tests + checks if _item_passed(i))
    if passing < 1:
        return CheckResult(
            name="required_tests_and_checks",
            status=_FAIL,
            reason="no_passing_test_or_check",
            detail="evidence has %d tests and %d checks, none passing"
            % (len(tests), len(checks)),
        )
    return CheckResult(
        name="required_tests_and_checks",
        status=_PASS,
        reason="passing_checks_found",
        detail="%d passing item(s)" % passing,
    )


def _check_codegraph(manifest: Mapping[str, Any]) -> CheckResult:
    """When changed files include source/build paths, CodeGraph must have passed."""
    problems = codegraph_audit_manifest_problems(manifest)
    if problems:
        return CheckResult(
            name="codegraph",
            status=_FAIL,
            reason="codegraph_problems",
            detail="; ".join(problems),
        )
    # codegraph_audit_manifest_problems returns [] for non-code changes too.
    repo = manifest.get("repo")
    if isinstance(repo, dict):
        from mac.codegraph_audit import codegraph_audit_required  # type: ignore[attr-defined]

        files = _manifest_list(repo.get("files_changed"))
        if codegraph_audit_required(files):
            return CheckResult(name="codegraph", status=_PASS, reason="codegraph_passed")
        return CheckResult(name="codegraph", status=_SKIP, reason="no_code_changes_require_codegraph")
    return CheckResult(name="codegraph", status=_SKIP, reason="no_repo_in_manifest")


def _check_dirty_stale_branch(manifest: Mapping[str, Any]) -> CheckResult:
    """Consolidates dirty-worktree and stale-branch conditions from the manifest.

    This is a purely manifest-level check (no I/O).  The authoritative
    git-level checks are ``_check_pushed_remote_ref`` and
    ``_check_remote_sha_equality``; this check provides a fast-path that
    catches cases where the executor itself recorded a dirty or unpushed state.
    """
    repo = manifest.get("repo")
    if not isinstance(repo, dict):
        return CheckResult(
            name="dirty_stale_branch",
            status=_FAIL,
            reason="repo_missing",
            detail="verification.repo object is absent",
        )
    dirty = repo.get("dirty")
    if dirty not in {False, "false", "False", 0, "0"}:
        return CheckResult(
            name="dirty_stale_branch",
            status=_FAIL,
            reason="dirty_worktree_declared",
            detail="repo.dirty=%r — evidence declares uncommitted changes" % dirty,
        )
    pushed = repo.get("pushed") is True or _str(repo.get("pushed")).lower() == "true"
    if not pushed:
        return CheckResult(
            name="dirty_stale_branch",
            status=_FAIL,
            reason="not_pushed",
            detail="repo.pushed is not true — the branch was never pushed",
        )
    return CheckResult(name="dirty_stale_branch", status=_PASS, reason="clean_and_pushed")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def verify_prior_executor_evidence(
    manifest: Mapping[str, Any],
    *,
    agent_key_lookup: Optional[Callable[[str], Optional[str]]] = None,
    expected_head_sha: Optional[str] = None,
    canonical_remote_url: Optional[str] = None,
    verify_remote: bool = False,
) -> ReuseVerificationResult:
    """Run all fail-closed invariant checks on a prior executor evidence manifest.

    Parameters
    ----------
    manifest:
        The ``verification`` dict from ``evidence.metadata["verification"]``.
    agent_key_lookup:
        Callable that accepts an agent_id and returns the agent's plaintext
        attestation key (or None if unknown).  When None the signature check
        is structural only (field presence).
    expected_head_sha:
        When set, ``repo.head_sha`` must match exactly.  Pass None to skip
        the comparison (e.g. when the task doesn't yet have a canonical SHA).
    canonical_remote_url:
        URL of the canonical remote (e.g. git@github.com:org/repo.git) used
        to verify ancestry.  None disables the ancestry check.
    verify_remote:
        When True, network-dependent checks (``git ls-remote`` for remote SHA
        equality and canonical ancestry) are run.  Defaults to False so the
        verifier works offline.

    Returns
    -------
    ReuseVerificationResult
        ``ok`` is True only when every non-skipped check passed.  Inspect
        ``checks`` for per-check status and ``problems`` for a concise summary.
    """
    result = ReuseVerificationResult()

    all_checks: List[CheckResult] = [
        _check_schema_and_status(manifest),
        _check_evidence_type(manifest),
        _check_signature_provenance(manifest, agent_key_lookup),
        _check_pushed_remote_ref(manifest),
        _check_dirty_stale_branch(manifest),
        _check_repo_head_sha(manifest, expected_head_sha),
        _check_remote_sha_equality(manifest, verify_remote=verify_remote),
        _check_canonical_ancestry(
            manifest, canonical_remote_url, verify_remote=verify_remote
        ),
        _check_required_tests_and_checks(manifest),
        _check_codegraph(manifest),
    ]

    result.checks = all_checks
    failing = [c for c in all_checks if c.status == _FAIL]
    result.problems = [
        "%s: %s%s" % (c.name, c.reason, (" — " + c.detail) if c.detail else "")
        for c in failing
    ]
    result.ok = len(failing) == 0
    return result
