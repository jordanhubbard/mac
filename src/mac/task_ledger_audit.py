"""Point-in-time, evidence-backed audit of the complete task ledger.

The normal task list is an operational projection: it tells an operator what
state each task currently claims to be in.  This module checks the stronger
question required for reconciliation: whether the history, evidence,
dependencies, replacement chain, and repository state support that claim.

The audit is deliberately read-only.  It reports contradictions and suggested
repairs, but never changes task state or fetches/mutates a repository.  Git
proof is based on the registered checkout and the canonical ref visible there
at the start of the audit.  A later caller may apply only the repairs it has
independently adjudicated.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mac.models import metadata_declares_report_deliverable


TASK_LEDGER_AUDIT_SCHEMA = "mac.task_ledger_audit.v1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GENERIC_FAILURE_REASONS = frozenset(
    {
        "max attempts",
        "executor_failed",
        "verification_contract_failed",
        "review rejected",
        "review rejected after max attempts",
        "review_verdict_wait_cap_hit",
    }
)
_ACTIVE_STATES = frozenset(
    {"open", "claimed", "running", "needs_review", "reviewing"}
)
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any) -> bool:
    return value is True or _text(value).lower() in {"1", "true", "yes", "on"}


def _task_commit_attribution(commit_message: str, task_id: str) -> str:
    """Return conservative task attribution encoded in a commit message.

    Modern publishers use the full ledger id, while older/manual publication
    paths used the CLI's eight-hex display form (``task_deadbeef``).  Ignoring
    that established convention falsely reports integrated legacy work as
    missing.  Boundary checks prevent one task prefix from matching a longer
    hexadecimal token accidentally.
    """

    if not commit_message or not task_id:
        return "none"
    if task_id in commit_message:
        return "task_id_in_commit_message"
    match = re.fullmatch(r"task_([0-9a-f]{32})", task_id)
    if match is None:
        return "none"
    short_id = "task_" + match.group(1)[:8]
    subject = commit_message.splitlines()[0].strip()
    # A short display id is not globally unique evidence when it merely occurs
    # in descriptive prose (for example, ``MAC task task_A: reproduce the
    # failure on task_B payload``).  Accept the legacy form only when the
    # subject identifies it as the commit's primary task, or when an old/manual
    # publication subject ends with the task reference.  Full 32-hex ids remain
    # independently strong enough to match anywhere in the message.
    primary = re.search(
        r"^(?:MAC\s+task|task)\s+%s(?:\s*[:\-]|\s|$)" % re.escape(short_id),
        subject,
        flags=re.IGNORECASE,
    )
    terminal = re.search(
        r"(?<![0-9A-Za-z_])%s(?:[.):,;!?])?$" % re.escape(short_id),
        subject,
    )
    if primary or terminal:
        return "task_id_prefix_in_commit_message"
    return "none"


def _state_counts(tasks: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    return dict(sorted(Counter(_text(task.get("state")) or "unknown" for task in tasks).items()))


def _task_set_digest(tasks: Iterable[Mapping[str, Any]]) -> str:
    rows = sorted(
        (
            _text(task.get("id")),
            _text(task.get("state")),
            _text(task.get("updated_at") or task.get("last_updated_at")),
        )
        for task in tasks
    )
    encoded = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return "sha256:%s" % hashlib.sha256(encoded).hexdigest()


def _history_reason(detail: Mapping[str, Any]) -> str:
    for key in ("reason", "error", "message", "feedback", "summary"):
        value = _text(detail.get(key))
        if value:
            return value
    problems = detail.get("problems")
    if isinstance(problems, list):
        rendered = "; ".join(_text(item) for item in problems if _text(item))
        if rendered:
            return rendered
    return ""


def _history_audit(task: Mapping[str, Any], history: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    problems: List[str] = []
    cursor: Optional[str] = None
    transitions = 0
    repeated_transitions = 0
    for event in history:
        from_state = _text(event.get("from_state")) or None
        to_state = _text(event.get("to_state")) or None
        if to_state is None:
            continue
        transitions += 1
        if cursor is None:
            cursor = from_state
        if (
            from_state is not None
            and cursor is not None
            and from_state != cursor
            and to_state == cursor
        ):
            # Some lifecycle operations intentionally emit both the ordinary
            # task.transitioned event and a more specific audit event (for
            # example task.auto_reopened) for the same from/to pair.  The
            # second event describes the already-applied transition; it is not
            # a discontinuity in the task's state machine.
            repeated_transitions += 1
            continue
        if from_state is not None and cursor is not None and from_state != cursor:
            problems.append(
                "history discontinuity at %s: expected from_state=%s, found %s"
                % (_text(event.get("id")) or "unknown event", cursor, from_state)
            )
        cursor = to_state

    claimed_state = _text(task.get("state"))
    if cursor is None:
        problems.append("history has no state-bearing event")
    elif cursor != claimed_state:
        problems.append(
            "history ends in %s but task claims %s" % (cursor, claimed_state)
        )

    # A later state->same-state annotation must not erase the reason that
    # actually put the task into its current state.  Walk backwards to the
    # most recent transition whose source differs from the claimed state.
    entry: Dict[str, Any] = {}
    for event in reversed(history):
        if (
            _text(event.get("to_state")) == claimed_state
            and _text(event.get("from_state")) != claimed_state
        ):
            entry = dict(event)
            break
    detail = _mapping(entry.get("detail"))
    return {
        "event_count": len(history),
        "transition_count": transitions,
        "repeated_transition_event_count": repeated_transitions,
        "valid": not problems,
        "problems": problems,
        "entered_state_at": _text(entry.get("created_at")) or None,
        "entry_event_id": _text(entry.get("id")) or None,
        "entry_event_type": _text(entry.get("event_type")) or None,
        "entry_actor": _text(entry.get("actor")) or None,
        "entry_reason": _history_reason(detail) or None,
        "entry_detail": detail,
    }


def _passing(item: Mapping[str, Any]) -> Optional[bool]:
    if item.get("returncode") is not None:
        try:
            return int(item.get("returncode")) == 0
        except (TypeError, ValueError):
            return False
    status = _text(item.get("status")).lower()
    if status in {"pass", "passed", "success", "successful", "succeeded", "ok"}:
        return True
    if status in {"fail", "failed", "failure", "error", "rejected"}:
        return False
    return None


def _repo_claim(
    repo: Mapping[str, Any],
    *,
    source: str,
    evidence_id: str = "",
    published: bool = False,
) -> Optional[Dict[str, Any]]:
    head_sha = _text(repo.get("head_sha"))
    if not _SHA_RE.fullmatch(head_sha):
        return None
    return {
        "head_sha": head_sha,
        "base_sha": _text(repo.get("base_sha")) or None,
        "branch": _text(repo.get("branch")) or None,
        "remote_ref": _text(repo.get("remote_ref")) or None,
        "pushed": _bool(repo.get("pushed")),
        "dirty": repo.get("dirty"),
        "files_changed": [
            _text(item) for item in (repo.get("files_changed") or []) if _text(item)
        ],
        "source": source,
        "evidence_id": evidence_id or None,
        "publication_evidence": bool(published),
    }


def _approved_review_chains(
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Return structurally valid approved review-to-executor links.

    Review rows point at the reviewer's verdict evidence.  The verdict in turn
    names the executor evidence it reviewed.  Keeping that indirection intact
    is what prevents a rejected or superseded executor attempt from becoming
    completion proof merely because its commit happens to be on ``main``.
    """

    chains: List[Dict[str, Any]] = []
    for review in reviews:
        if _text(review.get("status")).lower() != "approved":
            continue
        review_id = _text(review.get("id"))
        verdict_evidence_id = _text(review.get("evidence_id"))
        verdict_evidence = evidence_by_id.get(verdict_evidence_id)
        if verdict_evidence is None:
            continue
        metadata = _mapping(verdict_evidence.get("metadata"))
        verification = _mapping(metadata.get("verification"))
        if _text(verification.get("evidence_type")).lower() != "review_verdict":
            continue
        verdict = _text(verification.get("verdict")).lower()
        semantic_verdict = _text(verification.get("semantic_verdict")).lower()
        if verdict != "approved" or semantic_verdict not in {"", "approved"}:
            continue
        executor_evidence_id = _text(verification.get("reviewed_evidence_id"))
        executor_evidence = evidence_by_id.get(executor_evidence_id)
        if not executor_evidence_id or executor_evidence is None:
            continue
        executor_verification = _mapping(
            _mapping(executor_evidence.get("metadata")).get("verification")
        )
        if _text(executor_verification.get("evidence_type")).lower() == "review_verdict":
            continue
        chains.append(
            {
                "review_id": review_id or None,
                "verdict_evidence_id": verdict_evidence_id,
                "executor_evidence_id": executor_evidence_id,
            }
        )
    return chains


def _publication_resolution(
    detail: Mapping[str, Any],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    publications: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Resolve the one executor attempt authorized by active publication.

    The publication is the authority, not evidence creation order.  Standard
    publications directly name executor evidence.  Publication-artifact
    policies name a separate publication evidence row, so their executor is
    recovered through the task's immutable review target (or the sole valid
    approved review chain for legacy rows).
    """

    active = [
        item for item in publications if _text(item.get("status")).lower() == "published"
    ]
    base: Dict[str, Any] = {
        "status": "not_published",
        "publication_id": None,
        "publication_target": None,
        "publication_evidence_id": None,
        "executor_evidence_id": None,
        "review_id": None,
        "verdict_evidence_id": None,
        "problems": [],
    }
    if not active:
        return base
    if len(active) != 1:
        base.update(
            {
                "status": "invalid",
                "problems": ["multiple_active_publications"],
            }
        )
        return base

    publication = active[0]
    publication_id = _text(publication.get("id"))
    publication_evidence_id = _text(publication.get("evidence_id"))
    base.update(
        {
            "status": "invalid",
            "publication_id": publication_id or None,
            "publication_target": _text(publication.get("target")) or None,
            "publication_evidence_id": publication_evidence_id or None,
        }
    )
    if not publication_evidence_id:
        base["problems"] = ["publication_missing_evidence_id"]
        return base
    publication_evidence = evidence_by_id.get(publication_evidence_id)
    if publication_evidence is None:
        base["problems"] = ["publication_evidence_missing"]
        return base

    chains = _approved_review_chains(evidence_by_id, reviews)
    direct = [
        chain
        for chain in chains
        if _text(chain.get("executor_evidence_id")) == publication_evidence_id
    ]
    selected: Optional[Dict[str, Any]] = direct[-1] if direct else None
    if selected is None and _text(publication_evidence.get("kind")).lower() == "publication":
        task = _mapping(detail.get("task"))
        review_target = _mapping(_mapping(task.get("metadata")).get("review_target"))
        target_evidence_id = _text(review_target.get("executor_evidence_id"))
        targeted = [
            chain
            for chain in chains
            if target_evidence_id
            and _text(chain.get("executor_evidence_id")) == target_evidence_id
        ]
        if targeted:
            selected = targeted[-1]
        elif len(chains) == 1:
            # Compatibility for publication-artifact rows created before the
            # immutable review_target was persisted.  A single approved chain
            # is unambiguous; multiple chains fail closed.
            selected = chains[0]
    if selected is None:
        base["problems"] = ["publication_has_no_matching_approved_review"]
        return base

    base.update(
        {
            "status": "resolved",
            "executor_evidence_id": selected["executor_evidence_id"],
            "review_id": selected["review_id"],
            "verdict_evidence_id": selected["verdict_evidence_id"],
            "problems": [],
        }
    )
    return base


def _evidence_audit(detail: Mapping[str, Any]) -> Dict[str, Any]:
    evidence = _items(detail.get("evidence"))
    reviews = _items(detail.get("reviews"))
    publications = _items(detail.get("publications"))
    evidence_by_id = {
        _text(item.get("id")): item for item in evidence if _text(item.get("id"))
    }
    publication_resolution = _publication_resolution(
        detail, evidence_by_id, reviews, publications
    )
    published_evidence_ids = {
        _text(item.get("evidence_id"))
        for item in publications
        if _text(item.get("status")) == "published" and _text(item.get("evidence_id"))
    }
    kinds = Counter()
    verification_types = Counter()
    tests_passed = 0
    tests_failed = 0
    checks_passed = 0
    checks_failed = 0
    approved_verdicts = 0
    rejected_verdicts = 0
    repo_claims: List[Dict[str, Any]] = []
    adjudications: List[Dict[str, Any]] = []
    seen_claims: set[Tuple[str, str, str]] = set()

    def add_claim(claim: Optional[Dict[str, Any]]) -> None:
        if claim is None:
            return
        key = (
            _text(claim.get("head_sha")),
            _text(claim.get("evidence_id")),
            _text(claim.get("source")),
        )
        if key in seen_claims:
            return
        seen_claims.add(key)
        repo_claims.append(claim)

    for item in evidence:
        evidence_id = _text(item.get("id"))
        kinds[_text(item.get("kind")) or "unknown"] += 1
        metadata = _mapping(item.get("metadata"))
        if (
            _text(metadata.get("schema")) == "mac.task_ledger_adjudication.v1"
            and _text(item.get("created_by")) == "task-ledger-audit"
        ):
            adjudication = {
                "evidence_id": evidence_id,
                "disposition": _text(metadata.get("disposition")),
                "scope": _text(metadata.get("scope")),
                "reason": _text(metadata.get("reason")),
                "canonical_sha": _text(metadata.get("canonical_sha")) or None,
                "related_task_ids": [
                    _text(value)
                    for value in (metadata.get("related_task_ids") or [])
                    if _text(value)
                ],
            }
            adjudications.append(adjudication)
            canonical_sha = _text(metadata.get("canonical_sha"))
            if _SHA_RE.fullmatch(canonical_sha):
                add_claim(
                    {
                        "head_sha": canonical_sha,
                        "base_sha": None,
                        "branch": None,
                        "remote_ref": None,
                        "pushed": True,
                        "dirty": False,
                        "files_changed": [
                            _text(value)
                            for value in (metadata.get("files_changed") or [])
                            if _text(value)
                        ],
                        "source": "task_ledger_adjudication",
                        "evidence_id": evidence_id,
                        "publication_evidence": True,
                    }
                )
        verification = _mapping(metadata.get("verification"))
        evidence_type = _text(verification.get("evidence_type"))
        if evidence_type:
            verification_types[evidence_type] += 1
        for test in _items(verification.get("tests")):
            passed = _passing(test)
            tests_passed += int(passed is True)
            tests_failed += int(passed is False)
        for check in _items(verification.get("checks")):
            passed = _passing(check)
            checks_passed += int(passed is True)
            checks_failed += int(passed is False)
        verdict = _text(
            verification.get("verdict") or verification.get("semantic_verdict")
        ).lower()
        approved_verdicts += int(verdict in {"approved", "approve", "pass", "passed"})
        rejected_verdicts += int(verdict in {"rejected", "reject", "failed", "fail"})
        add_claim(
            _repo_claim(
                _mapping(verification.get("repo")),
                source="evidence.verification.repo",
                evidence_id=evidence_id,
                published=evidence_id in published_evidence_ids,
            )
        )

    task = _mapping(detail.get("task"))
    metadata = _mapping(task.get("metadata"))
    latest_claim = _mapping(metadata.get("latest_review_claim"))
    if _SHA_RE.fullmatch(_text(latest_claim.get("repository_head_sha"))):
        add_claim(
            {
                "head_sha": _text(latest_claim.get("repository_head_sha")),
                "base_sha": None,
                "branch": _text(latest_claim.get("repository_branch")) or None,
                "remote_ref": _text(latest_claim.get("repository_remote_ref")) or None,
                "pushed": any(
                    _text(check.get("name")) == "guarded git push"
                    and _passing(check) is True
                    for check in _items(latest_claim.get("checks"))
                ),
                "dirty": None,
                "files_changed": [
                    _text(item)
                    for item in (latest_claim.get("repository_files_changed") or [])
                    if _text(item)
                ],
                "source": "task.metadata.latest_review_claim",
                "evidence_id": _text(latest_claim.get("executor_evidence_id")) or None,
                "publication_evidence": False,
            }
        )

    authoritative_evidence_id = _text(
        publication_resolution.get("executor_evidence_id")
    )
    for claim in repo_claims:
        claim["authoritative"] = bool(
            authoritative_evidence_id
            and _text(claim.get("evidence_id")) == authoritative_evidence_id
            and _text(claim.get("source")) == "evidence.verification.repo"
        )
        claim["superseded"] = bool(
            authoritative_evidence_id and not claim["authoritative"]
        )

    return {
        "count": len(evidence),
        "kinds": dict(sorted(kinds.items())),
        "verification_types": dict(sorted(verification_types.items())),
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
        "checks_passed": checks_passed,
        "checks_failed": checks_failed,
        "approved_verdicts": approved_verdicts,
        "rejected_verdicts": rejected_verdicts,
        "review_statuses": dict(
            sorted(Counter(_text(item.get("status")) or "unknown" for item in reviews).items())
        ),
        "publication_statuses": dict(
            sorted(
                Counter(
                    _text(item.get("status")) or "unknown" for item in publications
                ).items()
            )
        ),
        "published_evidence_ids": sorted(published_evidence_ids),
        "publication_resolution": publication_resolution,
        "adjudications": adjudications,
        "repo_claims": repo_claims,
    }


def _repository_contract(repo: Mapping[str, Any]) -> Dict[str, Any]:
    metadata = _mapping(repo.get("metadata"))
    return _mapping(metadata.get("repository_contract"))


def _select_repository(
    task: Mapping[str, Any], repositories: Sequence[Mapping[str, Any]]
) -> Optional[Dict[str, Any]]:
    metadata = _mapping(task.get("metadata"))
    execution = _mapping(metadata.get("execution_contract"))
    origin = _mapping(metadata.get("origin"))
    wanted_id = _text(execution.get("repository_id") or origin.get("repository_id"))
    if wanted_id:
        for repo in repositories:
            if _text(repo.get("id")) == wanted_id:
                return dict(repo)
    wanted_name = _text(origin.get("repository_name"))
    if wanted_name:
        for repo in repositories:
            if _text(repo.get("name")).lower() == wanted_name.lower():
                return dict(repo)
    project = _text(task.get("project"))
    matches = [
        dict(repo)
        for repo in repositories
        if _text(repo.get("project")).lower() == project.lower() and project
    ]
    return matches[0] if len(matches) == 1 else None


def _repository_applicability(
    task: Mapping[str, Any], evidence: Mapping[str, Any], repo: Optional[Mapping[str, Any]]
) -> str:
    metadata = _mapping(task.get("metadata"))
    if metadata_declares_report_deliverable(metadata):
        return "not_applicable"
    execution = _mapping(metadata.get("execution_contract"))
    if _text(execution.get("type")) == "repository":
        return "applicable"
    origin = _mapping(metadata.get("origin"))
    if any(origin.get(key) for key in ("repository_id", "repository_path", "repository_name")):
        return "applicable"
    if evidence.get("repo_claims"):
        return "applicable"
    if repo is not None:
        return "unknown"
    return "not_applicable"


def _run_git(
    path: Path,
    args: Sequence[str],
    timeout: int,
    cache: Optional[Dict[Tuple[str, Tuple[str, ...]], subprocess.CompletedProcess[str]]] = None,
) -> subprocess.CompletedProcess[str]:
    key = (str(path), tuple(args))
    if cache is not None and key in cache:
        return cache[key]
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result = subprocess.CompletedProcess(
            ["git", "-C", str(path), *args], 1, stdout="", stderr=str(exc)
        )
    if cache is not None:
        cache[key] = result
    return result


def _canonical_tip(
    path: Path,
    timeout: int,
    cache: Optional[Dict[Tuple[str, Tuple[str, ...]], subprocess.CompletedProcess[str]]] = None,
) -> Tuple[str, str, str]:
    candidates = ("refs/remotes/origin/main", "refs/remotes/origin/master", "HEAD")
    errors: List[str] = []
    for ref in candidates:
        result = _run_git(path, ["rev-parse", "%s^{commit}" % ref], timeout, cache)
        sha = _text(result.stdout)
        if result.returncode == 0 and _SHA_RE.fullmatch(sha):
            return sha, ref, ""
        if _text(result.stderr):
            errors.append(_text(result.stderr).splitlines()[-1])
    return "", "", "; ".join(errors[-2:]) or "canonical ref is unavailable"


def _repository_snapshot(
    repo: Mapping[str, Any],
    *,
    verify_git: bool,
    timeout: int,
    cache: Optional[Dict[Tuple[str, Tuple[str, ...]], subprocess.CompletedProcess[str]]] = None,
) -> Dict[str, Any]:
    path = Path(_text(repo.get("path"))).expanduser()
    contract = _repository_contract(repo)
    result: Dict[str, Any] = {
        "id": _text(repo.get("id")) or None,
        "name": _text(repo.get("name")) or None,
        "project": _text(repo.get("project")) or None,
        "path": str(path),
        "canonical_remote_url": _text(contract.get("canonical_remote_url")) or None,
        "path_exists": path.is_dir(),
        "canonical_tip": None,
        "canonical_ref": None,
        "error": None,
    }
    if not verify_git:
        result["error"] = "git verification disabled"
        return result
    if not path.is_dir():
        result["error"] = "registered repository path does not exist"
        return result
    tip, ref, error = _canonical_tip(path, timeout, cache)
    result["canonical_tip"] = tip or None
    result["canonical_ref"] = ref or None
    result["error"] = error or None
    return result


def _git_claim_status(
    snapshot: Mapping[str, Any],
    claim: Mapping[str, Any],
    task_id: str,
    timeout: int,
    cache: Optional[Dict[Tuple[str, Tuple[str, ...]], subprocess.CompletedProcess[str]]] = None,
) -> Dict[str, Any]:
    result = dict(claim)
    path = Path(_text(snapshot.get("path"))).expanduser()
    tip = _text(snapshot.get("canonical_tip"))
    sha = _text(claim.get("head_sha"))
    result.update(
        {
            "commit_present": False,
            "ancestor_of_canonical": False,
            "patch_equivalent_to_canonical": False,
            "integration_status": "not_verified",
            "attribution_status": "none",
            "verification_error": None,
        }
    )
    if not path.is_dir() or not tip:
        result["verification_error"] = _text(snapshot.get("error")) or "canonical tip unavailable"
        return result
    present = _run_git(path, ["cat-file", "-e", "%s^{commit}" % sha], timeout, cache)
    if present.returncode != 0:
        result["verification_error"] = "commit is not present in registered checkout"
        return result
    result["commit_present"] = True
    message = _run_git(path, ["show", "-s", "--format=%B", sha], timeout, cache)
    commit_message = _text(message.stdout)
    result["commit_subject"] = commit_message.splitlines()[0] if commit_message else None
    message_attribution = _task_commit_attribution(commit_message, task_id)
    if message_attribution != "none":
        result["attribution_status"] = message_attribution
    elif claim.get("publication_evidence"):
        result["attribution_status"] = "accepted_publication_evidence"
    else:
        base_sha = _text(claim.get("base_sha"))
        claimed_files = {
            _text(item) for item in (claim.get("files_changed") or []) if _text(item)
        }
        if (
            _SHA_RE.fullmatch(base_sha)
            and base_sha != sha
            and claimed_files
            and _run_git(path, ["cat-file", "-e", "%s^{commit}" % base_sha], timeout, cache).returncode
            == 0
        ):
            changed = _run_git(
                path, ["diff", "--name-only", "%s..%s" % (base_sha, sha)], timeout, cache
            )
            actual_files = {
                _text(item) for item in changed.stdout.splitlines() if _text(item)
            }
            if changed.returncode == 0 and claimed_files.issubset(actual_files):
                result["attribution_status"] = "manifest_diff_matches_claimed_files"
    ancestor = _run_git(path, ["merge-base", "--is-ancestor", sha, tip], timeout, cache)
    if ancestor.returncode == 0:
        result["ancestor_of_canonical"] = True
        result["integration_status"] = (
            "ancestor"
            if result["attribution_status"] != "none"
            else "ancestor_unattributed"
        )
        return result

    # Preserve proof across squash/cherry-pick publication.  ``git cherry`` is
    # meaningful for a normal single-parent commit; merge commits remain
    # unverified unless their exact SHA is reachable from the canonical tip.
    parents = _run_git(path, ["rev-list", "--parents", "-n", "1", sha], timeout, cache)
    parts = _text(parents.stdout).split()
    if parents.returncode == 0 and len(parts) == 2:
        cherry = _run_git(path, ["cherry", tip, sha, parts[1]], timeout, cache)
        line = _text(cherry.stdout).splitlines()
        if cherry.returncode == 0 and line and line[0].startswith("-"):
            result["patch_equivalent_to_canonical"] = True
            result["integration_status"] = (
                "patch_equivalent"
                if result["attribution_status"] != "none"
                else "patch_equivalent_unattributed"
            )
            return result
    result["integration_status"] = "not_integrated"
    return result


def _canonical_task_commit_claim(
    snapshot: Mapping[str, Any], task_id: str, task_title: str, timeout: int,
    cache: Optional[Dict[Tuple[str, Tuple[str, ...]], subprocess.CompletedProcess[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """Find a task-attributed commit that is already on the canonical ref.

    Early workers did not always persist the final publication SHA in task
    evidence, but the publication commit convention includes the full task ID
    in its subject.  Searching only the canonical history is conservative: a
    matching commit on an abandoned task branch does not count.
    """

    path = Path(_text(snapshot.get("path"))).expanduser()
    tip = _text(snapshot.get("canonical_tip"))
    if not path.is_dir() or not tip or not task_id:
        return None
    candidates: List[Tuple[str, str]] = [(task_id, "task_id")]
    task_match = re.fullmatch(r"task_([0-9a-f]{32})", task_id)
    if task_match is not None:
        candidates.append(("task_" + task_match.group(1)[:8], "task_id"))
    # Several early repositories used the complete task title as the commit
    # subject but omitted the ledger id.  An exact, non-trivial title match on
    # canonical history is direct attribution; fuzzy similarity is deliberately
    # excluded and remains a human adjudication concern.
    if len(task_title.strip()) >= 16:
        candidates.append((task_title.strip(), "task_title"))
    sha: List[str] = []
    attribution = "none"
    commit_subject: Optional[str] = None
    for candidate, candidate_kind in candidates:
        result = _run_git(
            path,
            [
                "log",
                tip,
                "--fixed-strings",
                "--grep=%s" % candidate,
                "--format=%H%x00%s",
                "-n",
                "1",
            ],
            timeout, cache,
        )
        line = _text(result.stdout).splitlines()
        if result.returncode != 0 or not line:
            continue
        parts = line[0].split("\x00", 1)
        if not parts or not _SHA_RE.fullmatch(parts[0]):
            continue
        subject = parts[1] if len(parts) > 1 else ""
        candidate_attribution = _task_commit_attribution(subject, task_id)
        if (
            candidate_attribution == "none"
            and candidate_kind == "task_title"
            and task_title.strip() in subject
        ):
            candidate_attribution = "task_title_in_commit_message"
        if candidate_attribution == "none":
            continue
        sha = [parts[0]]
        attribution = candidate_attribution
        commit_subject = parts[1] if len(parts) > 1 else None
        break
    if not sha:
        return None
    return {
        "head_sha": sha[0],
        "base_sha": None,
        "branch": None,
        "remote_ref": None,
        "pushed": True,
        "dirty": False,
        "files_changed": [],
        "source": "canonical_commit_message",
        "evidence_id": None,
        "publication_evidence": True,
        "commit_present": True,
        "ancestor_of_canonical": True,
        "patch_equivalent_to_canonical": False,
        "integration_status": "ancestor",
        "attribution_status": attribution,
        "commit_subject": commit_subject,
        "verification_error": None,
    }


def _dependency_audit(
    task: Mapping[str, Any], tasks_by_id: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    dependencies = [_text(item) for item in (task.get("dependencies") or []) if _text(item)]
    rows = []
    for dependency_id in dependencies:
        dependency = tasks_by_id.get(dependency_id)
        rows.append(
            {
                "task_id": dependency_id,
                "state": _text(dependency.get("state")) if dependency else "missing",
                "title": _text(dependency.get("title")) if dependency else None,
            }
        )
    terminal_blockers: List[Dict[str, Any]] = []
    cycles: List[List[str]] = []
    seen_blockers: set[Tuple[str, str]] = set()
    seen_cycles: set[Tuple[str, ...]] = set()

    def walk(task_id: str, path: List[str]) -> None:
        if task_id in path:
            cycle = path[path.index(task_id) :] + [task_id]
            # Normalize the cycle for de-duplication without discarding the
            # human-readable path that explains how this task reaches it.
            normalized = tuple(sorted(set(cycle)))
            if normalized not in seen_cycles:
                seen_cycles.add(normalized)
                cycles.append(cycle)
            return
        dependency = tasks_by_id.get(task_id)
        if dependency is None:
            key = (task_id, "missing")
            if key not in seen_blockers:
                seen_blockers.add(key)
                terminal_blockers.append(
                    {"task_id": task_id, "state": "missing", "path": path + [task_id]}
                )
            return
        state = _text(dependency.get("state"))
        if state in {"failed", "cancelled"}:
            key = (task_id, state)
            if key not in seen_blockers:
                seen_blockers.add(key)
                terminal_blockers.append(
                    {"task_id": task_id, "state": state, "path": path + [task_id]}
                )
            return
        if state == "completed":
            return
        for child_id in (
            _text(item) for item in (dependency.get("dependencies") or []) if _text(item)
        ):
            walk(child_id, path + [task_id])

    for dependency_id in dependencies:
        walk(dependency_id, [_text(task.get("id"))])

    return {
        "count": len(dependencies),
        "all_completed": bool(dependencies)
        and all(item["state"] == "completed" for item in rows),
        "incomplete_count": sum(item["state"] != "completed" for item in rows),
        "terminal_blocker_count": len(terminal_blockers),
        "terminal_blockers": terminal_blockers,
        "cycle_count": len(cycles),
        "cycles": cycles,
        "items": rows,
    }


def _replacement_id(task: Mapping[str, Any], history: Mapping[str, Any]) -> str:
    metadata = _mapping(task.get("metadata"))
    lifecycle = _mapping(metadata.get("repository_ref_lifecycle"))
    entry_detail = _mapping(history.get("entry_detail"))
    return _text(
        lifecycle.get("replacement_task_id")
        or entry_detail.get("replacement_task_id")
        or metadata.get("replacement_task_id")
    )


def _replacement_audit(
    task: Mapping[str, Any],
    history: Mapping[str, Any],
    rows_by_id: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    replacement_id = _replacement_id(task, history)
    if not replacement_id:
        return {"task_id": None, "state": None, "assessment": None}
    replacement = rows_by_id.get(replacement_id)
    return {
        "task_id": replacement_id,
        "state": _text(replacement.get("state")) if replacement else "missing",
        "assessment": _text(_mapping(replacement.get("assessment")).get("verdict"))
        if replacement
        else None,
        "repository_integration_status": _text(
            _mapping(replacement.get("repository")).get("integration_status")
        )
        if replacement
        else None,
    }


def _root_failure_reason(history: Sequence[Mapping[str, Any]], current_reason: str) -> str:
    for event in reversed(history):
        detail = _mapping(event.get("detail"))
        reason = _history_reason(detail)
        if reason and reason.lower() not in _GENERIC_FAILURE_REASONS:
            return reason
    return current_reason


def _assessment(
    row: Mapping[str, Any],
    raw_history: Sequence[Mapping[str, Any]],
    replacement: Mapping[str, Any],
) -> Dict[str, Any]:
    task = _mapping(row.get("task"))
    state = _text(task.get("state"))
    history = _mapping(row.get("history"))
    evidence = _mapping(row.get("evidence"))
    repository = _mapping(row.get("repository"))
    dependencies = _mapping(row.get("dependencies"))
    findings: List[str] = []
    action = "none"
    verdict = "verified"

    if not history.get("valid"):
        findings.append("history_chain_invalid")
        verdict = "contradiction"
        action = "repair_history_or_task_state"

    integrated = _text(repository.get("integration_status")) in {
        "ancestor",
        "patch_equivalent",
    }
    applicability = _text(repository.get("applicability"))
    evidence_count = int(evidence.get("count") or 0)
    replacement_verified = (
        _text(replacement.get("state")) == "completed"
        and _text(replacement.get("assessment")) == "verified"
    )
    replacement_active = _text(replacement.get("state")) in {
        "open",
        "waiting",
        "blocked",
        "claimed",
        "running",
        "needs_review",
        "reviewing",
    }
    entry_reason = _text(history.get("entry_reason"))
    entry_detail = _mapping(history.get("entry_detail"))
    metadata = _mapping(task.get("metadata"))
    lifecycle = _mapping(metadata.get("repository_ref_lifecycle"))
    effective_reason = entry_reason or _text(lifecycle.get("reason"))
    disposition = _text(
        lifecycle.get("disposition") or entry_detail.get("disposition")
    ).lower()
    adjudication_dispositions = {
        _text(item.get("disposition"))
        for item in _items(evidence.get("adjudications"))
    }
    operational_completion_verified = (
        "completed_operationally_verified" in adjudication_dispositions
    )
    cancellation_confirmed = bool(
        adjudication_dispositions
        & {"cancellation_confirmed", "cancellation_confirmed_decomposed"}
    )

    if state == "completed":
        publication_resolution = _mapping(evidence.get("publication_resolution"))
        if (
            publication_resolution.get("publication_id")
            and _text(publication_resolution.get("status")) != "resolved"
        ):
            findings.append("completed_publication_chain_invalid")
            verdict = "contradiction"
            action = "repair_publication_evidence_or_reopen"
        elif operational_completion_verified:
            pass
        elif applicability == "applicable" and not integrated:
            findings.append("completed_repository_work_not_proven_on_canonical_branch")
            verdict = "contradiction" if repository.get("candidate_count") else "needs_review"
            action = "verify_or_reopen_completed_task"
        elif applicability == "unknown" and not integrated:
            findings.append("completed_task_repository_applicability_unresolved")
            verdict = "needs_review"
            action = "classify_deliverable_and_verify_code"
        elif applicability == "not_applicable" and evidence_count == 0:
            findings.append("completed_non_repository_task_has_no_evidence")
            verdict = "needs_review"
            action = "supply_completion_evidence_or_reopen"
    elif state == "cancelled":
        if not effective_reason:
            findings.append("cancelled_without_entry_reason")
            verdict = "contradiction"
            action = "supply_cancellation_adjudication_or_reopen"
        elif cancellation_confirmed:
            pass
        elif replacement.get("task_id") and not replacement_verified:
            if replacement_active:
                findings.append("cancelled_task_has_active_replacement")
                verdict = "active_valid"
                action = "none"
            else:
                findings.append("cancelled_replacement_not_verified_complete")
                verdict = "contradiction" if replacement.get("state") in {"failed", "missing"} else "needs_review"
                action = "reopen_or_repair_replacement_chain"
        elif replacement_verified:
            pass
        elif integrated:
            findings.append("cancelled_task_work_is_on_canonical_branch")
            verdict = "contradiction"
            action = "reconcile_cancelled_task_as_completed"
        elif disposition in {"not_applicable"} or metadata.get("experiment_purpose"):
            pass
        elif any(token in (task.get("title") or "").lower() for token in ("probe", "canary", "loop proof")):
            pass
        elif any(
            token in effective_reason.lower()
            for token in ("probe", "canary", "loop-proof", "loop proof")
        ):
            pass
        else:
            findings.append("cancellation_requires_semantic_adjudication")
            verdict = "needs_review"
            action = "confirm_cancellation_or_reopen"
    elif state == "failed":
        root_reason = _root_failure_reason(raw_history, entry_reason)
        if integrated:
            findings.append("failed_task_work_is_on_canonical_branch")
            verdict = "contradiction"
            action = "reconcile_failed_task_as_completed"
        elif replacement_verified:
            pass
        elif replacement_active:
            findings.append("failed_task_has_active_replacement")
            verdict = "active_valid"
            action = "none"
        elif not root_reason:
            findings.append("failed_without_root_cause")
            verdict = "contradiction"
            action = "diagnose_and_reopen_failed_task"
        else:
            findings.append("failed_work_remains_unsuperseded")
            verdict = "needs_review"
            action = "repair_root_cause_and_reopen_or_cancel_with_replacement"
    elif state == "waiting":
        incomplete = int(dependencies.get("incomplete_count") or 0)
        dependency_count = int(dependencies.get("count") or 0)
        if integrated:
            findings.append("waiting_task_work_is_on_canonical_branch")
            verdict = "contradiction"
            action = "reconcile_waiting_task_as_completed"
        elif not dependency_count:
            findings.append("waiting_without_dependencies")
            verdict = "contradiction"
            action = "reopen_invalid_waiting_task"
        elif incomplete == 0:
            findings.append("waiting_with_all_dependencies_completed")
            verdict = "contradiction"
            action = "reopen_stranded_waiting_task"
        elif int(dependencies.get("terminal_blocker_count") or 0):
            findings.append("waiting_on_failed_cancelled_or_missing_dependency")
            verdict = "contradiction"
            action = "repair_terminal_dependency_or_cancel_with_replacement"
        elif int(dependencies.get("cycle_count") or 0):
            findings.append("waiting_dependency_cycle")
            verdict = "contradiction"
            action = "break_dependency_cycle"
        else:
            verdict = "active_valid"
    elif state == "blocked":
        incomplete = int(dependencies.get("incomplete_count") or 0)
        dependency_count = int(dependencies.get("count") or 0)
        ready_at = _parse_time(entry_detail.get("ready_at"))
        now = datetime.now(timezone.utc)
        if integrated:
            findings.append("blocked_task_work_is_on_canonical_branch")
            verdict = "contradiction"
            action = "reconcile_blocked_task_as_completed"
        elif dependency_count and incomplete == 0:
            findings.append("blocked_with_all_dependencies_completed")
            verdict = "contradiction"
            action = "reopen_stranded_blocked_task"
        elif int(dependencies.get("terminal_blocker_count") or 0):
            findings.append("blocked_by_failed_cancelled_or_missing_dependency")
            verdict = "contradiction"
            action = "repair_terminal_dependency_or_cancel_with_replacement"
        elif int(dependencies.get("cycle_count") or 0):
            findings.append("blocked_by_dependency_cycle")
            verdict = "contradiction"
            action = "break_dependency_cycle"
        elif ready_at is not None and ready_at <= now:
            findings.append("blocked_retry_deadline_elapsed")
            verdict = "contradiction"
            action = "reopen_stranded_blocked_task"
        elif dependency_count and incomplete:
            findings.append("actionable_block_also_has_incomplete_dependencies")
            verdict = "needs_review"
            action = "separate_blocker_from_dependency_wait"
        elif not entry_reason:
            findings.append("blocked_without_reason_or_incomplete_dependency")
            verdict = "contradiction"
            action = "supply_blocker_or_reopen"
        else:
            findings.append("external_or_operator_blocker_requires_revalidation")
            verdict = "needs_review"
            action = "revalidate_blocker"
    elif state == "open":
        if int(dependencies.get("terminal_blocker_count") or 0):
            findings.append("open_task_has_terminal_dependency")
            verdict = "contradiction"
            action = "repair_terminal_dependency_or_cancel_with_replacement"
        elif int(dependencies.get("cycle_count") or 0):
            findings.append("open_task_has_dependency_cycle")
            verdict = "contradiction"
            action = "break_dependency_cycle"
        elif int(dependencies.get("incomplete_count") or 0):
            findings.append("open_with_incomplete_dependencies")
            verdict = "needs_review"
            action = "confirm_dependency_semantics_or_block"
        else:
            verdict = "active_valid"
            if _bool(metadata.get("no_dispatch")):
                findings.append("open_task_is_intentionally_held")
    elif state in _ACTIVE_STATES:
        leased_until = _parse_time(task.get("leased_until"))
        if state in {"claimed", "running"} and leased_until and leased_until <= datetime.now(timezone.utc):
            findings.append("active_task_lease_expired")
            verdict = "contradiction"
            action = "release_or_reopen_stale_lease"
        else:
            verdict = "active_valid"
    else:
        findings.append("unknown_task_state")
        verdict = "contradiction"
        action = "repair_task_state"

    return {
        "verdict": verdict,
        "findings": findings,
        "recommended_action": action,
        "root_failure_reason": _root_failure_reason(raw_history, entry_reason)
        if state == "failed"
        else None,
    }


def build_task_ledger_audit(
    details: Sequence[Mapping[str, Any]],
    repositories: Sequence[Mapping[str, Any]],
    *,
    snapshot_started_at: Optional[str] = None,
    snapshot_finished_at: Optional[str] = None,
    start_tasks: Optional[Sequence[Mapping[str, Any]]] = None,
    end_tasks: Optional[Sequence[Mapping[str, Any]]] = None,
    detail_errors: Optional[Sequence[Mapping[str, Any]]] = None,
    project: Optional[str] = None,
    verify_git: bool = True,
    git_timeout_seconds: int = 10,
    all_tasks: Optional[Sequence[Mapping[str, Any]]] = None,
    pagination: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Audit every supplied task detail and return one compact row per task."""

    started_at = snapshot_started_at or _utc_now()
    tasks = [_mapping(detail.get("task")) for detail in details]
    if project is not None:
        tasks_allowed = {
            _text(task.get("id")) for task in tasks if _text(task.get("project")) == project
        }
        details = [
            detail
            for detail in details
            if _text(_mapping(detail.get("task")).get("id")) in tasks_allowed
        ]
        tasks = [_mapping(detail.get("task")) for detail in details]

    dependency_tasks = [_mapping(task) for task in (all_tasks or tasks)]
    tasks_by_id = {
        _text(task.get("id")): task for task in dependency_tasks if _text(task.get("id"))
    }
    git_command_cache: Dict[Tuple[str, Tuple[str, ...]], subprocess.CompletedProcess[str]] = {}
    repo_snapshots = {
        _text(repo.get("id")): _repository_snapshot(
            repo, verify_git=verify_git, timeout=git_timeout_seconds, cache=git_command_cache
        )
        for repo in repositories
        if _text(repo.get("id"))
    }
    git_cache: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    raw_histories: Dict[str, List[Dict[str, Any]]] = {}

    for detail in details:
        task = _mapping(detail.get("task"))
        task_id = _text(task.get("id"))
        history_items = _items(detail.get("history"))
        raw_histories[task_id] = history_items
        history = _history_audit(task, history_items)
        lifecycle = _mapping(_mapping(task.get("metadata")).get("repository_ref_lifecycle"))
        history["lifecycle_reason"] = _text(lifecycle.get("reason")) or None
        history["effective_entry_reason"] = (
            _text(history.get("entry_reason")) or _text(lifecycle.get("reason")) or None
        )
        evidence = _evidence_audit(detail)
        selected_repo = _select_repository(task, repositories)
        selected_snapshot = (
            repo_snapshots.get(_text(selected_repo.get("id"))) if selected_repo else None
        )
        claims = evidence.get("repo_claims") or []
        verified_claims: List[Dict[str, Any]] = []
        for claim in claims:
            if selected_snapshot is None or not verify_git:
                checked = dict(claim)
                checked.update(
                    {
                        "commit_present": False,
                        "ancestor_of_canonical": False,
                        "patch_equivalent_to_canonical": False,
                        "integration_status": "not_verified",
                        "verification_error": "registered repository unavailable"
                        if selected_snapshot is None
                        else "git verification disabled",
                    }
                )
            else:
                key = (
                    _text(selected_snapshot.get("path")),
                    _text(selected_snapshot.get("canonical_tip")),
                    _text(claim.get("head_sha")),
                    task_id,
                    _text(claim.get("base_sha")),
                    bool(claim.get("publication_evidence")),
                    tuple(sorted(_text(item) for item in (claim.get("files_changed") or []))),
                    _text(claim.get("source")),
                    _text(claim.get("evidence_id")),
                )
                if key not in git_cache:
                    git_cache[key] = _git_claim_status(
                        selected_snapshot, claim, task_id, git_timeout_seconds, git_command_cache
                    )
                checked = {**claim, **git_cache[key]}
            verified_claims.append(checked)
        if selected_snapshot is not None and verify_git:
            attributed = _canonical_task_commit_claim(
                selected_snapshot, task_id, _text(task.get("title")), git_timeout_seconds,
                git_command_cache,
            )
            if attributed is not None and not any(
                _text(claim.get("head_sha")) == _text(attributed.get("head_sha"))
                for claim in verified_claims
            ):
                verified_claims.append(attributed)
        publication_resolution = _mapping(evidence.get("publication_resolution"))
        has_active_publication = bool(publication_resolution.get("publication_id"))
        if has_active_publication:
            integration_candidates = [
                claim for claim in verified_claims if claim.get("authoritative") is True
            ]
        else:
            integration_candidates = verified_claims
        integration_claim = next(
            (
                claim
                for claim in sorted(
                    integration_candidates,
                    key=lambda item: (
                        item.get("authoritative") is not True,
                        _text(item.get("attribution_status"))
                        not in {
                            "task_id_in_commit_message",
                            "task_id_prefix_in_commit_message",
                            "task_title_in_commit_message",
                        },
                        _text(item.get("attribution_status"))
                        != "accepted_publication_evidence",
                    ),
                )
                if _text(claim.get("integration_status"))
                in {"ancestor", "patch_equivalent"}
            ),
            None,
        )
        applicability = _repository_applicability(task, evidence, selected_repo)
        repository = {
            "applicability": applicability,
            "repository_id": _text(selected_repo.get("id")) if selected_repo else None,
            "repository_name": _text(selected_repo.get("name")) if selected_repo else None,
            "path": _text(selected_snapshot.get("path")) if selected_snapshot else None,
            "path_exists": bool(selected_snapshot and selected_snapshot.get("path_exists")),
            "canonical_tip": _text(selected_snapshot.get("canonical_tip"))
            if selected_snapshot
            else None,
            "canonical_ref": _text(selected_snapshot.get("canonical_ref"))
            if selected_snapshot
            else None,
            "snapshot_error": _text(selected_snapshot.get("error"))
            if selected_snapshot
            else "no registered repository matched task",
            "candidate_count": len(verified_claims),
            "authoritative_candidate_count": len(integration_candidates),
            "unattributed_canonical_candidate_count": sum(
                _text(claim.get("integration_status"))
                in {"ancestor_unattributed", "patch_equivalent_unattributed"}
                for claim in verified_claims
            ),
            "integration_status": _text(integration_claim.get("integration_status"))
            if integration_claim
            else (
                "not_integrated"
                if any(
                    _text(claim.get("integration_status")) == "not_integrated"
                    for claim in verified_claims
                )
                else "not_verified"
            ),
            "proof_sha": _text(integration_claim.get("head_sha"))
            if integration_claim
            else None,
            "claims": verified_claims,
        }
        dependencies = _dependency_audit(task, tasks_by_id)
        rows.append(
            {
                "task_id": task_id,
                "title": _text(task.get("title")),
                "project": _text(task.get("project")) or None,
                "state": _text(task.get("state")),
                "created_at": _text(task.get("created_at")) or None,
                "updated_at": _text(task.get("updated_at") or task.get("last_updated_at"))
                or None,
                "task": task,
                "history": history,
                "dependencies": dependencies,
                "evidence": evidence,
                "repository": repository,
                "replacement": {"task_id": None, "state": None, "assessment": None},
                "assessment": {},
            }
        )

    rows_by_id = {_text(row.get("task_id")): row for row in rows}
    # Two passes allow a cancellation/failed task to rely on a completed
    # replacement's independently computed code proof.
    for row in rows:
        row["assessment"] = _assessment(
            row, raw_histories.get(_text(row.get("task_id")), []), {}
        )
    for row in rows:
        replacement = _replacement_audit(
            _mapping(row.get("task")), _mapping(row.get("history")), rows_by_id
        )
        row["replacement"] = replacement
        row["assessment"] = _assessment(
            row,
            raw_histories.get(_text(row.get("task_id")), []),
            replacement,
        )

    # Do not duplicate the complete task body in the final compact report.
    for row in rows:
        row.pop("task", None)

    start_snapshot = [dict(item) for item in (start_tasks or tasks)]
    end_snapshot = [dict(item) for item in (end_tasks or start_snapshot)]
    start_by_id = {_text(item.get("id")): item for item in start_snapshot}
    end_by_id = {_text(item.get("id")): item for item in end_snapshot}
    added = sorted(set(end_by_id) - set(start_by_id))
    removed = sorted(set(start_by_id) - set(end_by_id))
    changed = sorted(
        task_id
        for task_id in set(start_by_id) & set(end_by_id)
        if (
            _text(start_by_id[task_id].get("state")),
            _text(
                start_by_id[task_id].get("updated_at")
                or start_by_id[task_id].get("last_updated_at")
            ),
        )
        != (
            _text(end_by_id[task_id].get("state")),
            _text(
                end_by_id[task_id].get("updated_at")
                or end_by_id[task_id].get("last_updated_at")
            ),
        )
    )
    verdict_counts = dict(
        sorted(
            Counter(
                _text(_mapping(row.get("assessment")).get("verdict")) or "unknown"
                for row in rows
            ).items()
        )
    )
    finding_counts = Counter()
    for row in rows:
        finding_counts.update(_mapping(row.get("assessment")).get("findings") or [])

    return {
        "schema": TASK_LEDGER_AUDIT_SCHEMA,
        "mode": "read_only",
        "project": project,
        "snapshot": {
            "started_at": started_at,
            "finished_at": snapshot_finished_at or _utc_now(),
            "task_count": len(rows),
            "state_counts": _state_counts(tasks),
            "task_set_digest": _task_set_digest(tasks),
            "detail_error_count": len(detail_errors or []),
            "detail_errors": [dict(item) for item in (detail_errors or [])],
            "pagination": dict(pagination or {"offset": 0, "limit": None, "returned": len(rows), "total": len(tasks)}),
            "changed_during_run": bool(added or removed or changed),
            "added_task_ids": added,
            "removed_task_ids": removed,
            "updated_task_ids": changed,
        },
        "repositories": list(repo_snapshots.values()),
        "summary": {
            "verdict_counts": verdict_counts,
            "finding_counts": dict(sorted(finding_counts.items())),
            "unresolved_count": sum(
                count
                for verdict, count in verdict_counts.items()
                if verdict in {"contradiction", "needs_review"}
            ),
        },
        "tasks": sorted(rows, key=lambda row: (_text(row.get("state")), _text(row.get("task_id")))),
    }


__all__ = ["TASK_LEDGER_AUDIT_SCHEMA", "build_task_ledger_audit"]
