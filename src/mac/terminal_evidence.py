"""Terminal evidence detection: has this task's work already landed?

MAC could only ever ask this question at *completion* time, inside
``ControlPlane._require_canonical_integration_proof``, and only as a raising
gate. The claim and reopen paths could not ask it at all, so a row whose work
had already merged stayed claimable purely because its ``state`` said ``open``.
That is the duplicate-PR bug: on 2026-08-19 the open pull-request queue held 23
pull requests for 12 distinct pieces of work, and a fleet restart re-opened
``task_f33a2da7`` after its change had merged as PR #498, whereupon a worker
claimed it and re-implemented a merged module.

horde-claw-fleet ADR-0121 finding 4 states the rule this module enforces:

    Terminal evidence and queue status diverged. Rows with terminal evidence
    MUST NOT be claimable unless an explicit replacement or retry row is
    created.

Everything here is a pure function over already-loaded task metadata and
evidence mappings. The raising gate in ``services`` is now expressed in terms
of :func:`evidence_carries_canonical_integration` so the completion gate and
the claim gate cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Dict, List, NamedTuple, Optional


JsonDict = Dict[str, Any]

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

#: Metadata key under which a row records that an operator (or the reopen path)
#: has deliberately authorised further work despite terminal evidence.
LINEAGE_METADATA_KEY = "lineage"


class TerminalEvidenceKind(str, Enum):
    """Why a row is considered to have terminal evidence."""

    NONE = "none"
    #: The deterministic finalizer verified a guarded canonical push remotely.
    CANONICAL_INTEGRATION = "canonical_integration"
    #: A forge reported the task's pull request as merged.
    MERGED_PULL_REQUEST = "merged_pull_request"
    #: The row itself records a durable completion.
    RECORDED_COMPLETION = "recorded_completion"


#: Kinds that mean "the work exists"; ordered most to least authoritative.
TERMINAL_KINDS = (
    TerminalEvidenceKind.CANONICAL_INTEGRATION,
    TerminalEvidenceKind.MERGED_PULL_REQUEST,
    TerminalEvidenceKind.RECORDED_COMPLETION,
)


class TerminalEvidence(NamedTuple):
    """Verdict on whether a task's work already exists.

    ``present`` is the only field callers must branch on. ``kind`` and
    ``detail`` exist so a refusal can say *which* proof it found rather than
    emitting an unactionable "already done".
    """

    present: bool
    kind: str
    detail: JsonDict

    def to_dict(self) -> JsonDict:
        return {"present": self.present, "kind": self.kind, "detail": dict(self.detail)}


NO_TERMINAL_EVIDENCE = TerminalEvidence(False, TerminalEvidenceKind.NONE.value, {})


def _mapping(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _manifest(evidence_metadata: Mapping[str, Any]) -> JsonDict:
    """Unwrap the ``verification`` envelope, tolerating flat legacy metadata."""

    metadata = _mapping(evidence_metadata)
    return _mapping(metadata.get("verification")) or metadata


def evidence_carries_canonical_integration(
    evidence_metadata: Mapping[str, Any],
    canonical_ref: str,
) -> bool:
    """Does this one evidence record prove a durable canonical integration?

    Extracted verbatim from the completion gate so both gates share a single
    definition. An approved review proves an executor attempt was acceptable;
    it does not prove the reviewed ref survived parallel integration. Only the
    finalizer's remotely-verified guarded push does.

    Three shapes satisfy it, all requiring ``status=pass``,
    ``remote_verified=true`` and the exact canonical ref:

    * the recorded canonical tip *is* the evidence head (fast-forward);
    * the proof names this evidence's reviewed head and asserts the canonical
      tip contains it (merge commit);
    * the proof names this evidence's reviewed head and carries the explicit
      squash marker -- a squash merge lands the reviewed *content* under a new
      SHA, so neither equality nor ancestry can hold and the forge's report of
      the merge is itself the proof.
    """

    manifest = _manifest(evidence_metadata)
    repo = _mapping(manifest.get("repo"))
    integration = _mapping(manifest.get("canonical_integration"))
    head_sha = _text(repo.get("head_sha"))
    proof_sha = _text(
        integration.get("canonical_tip_sha") or integration.get("head_sha")
    )
    reviewed_sha = _text(integration.get("reviewed_head_sha"))
    reviewed_names_head = bool(
        _GIT_SHA_RE.match(reviewed_sha) and reviewed_sha == head_sha
    )
    proof_carries_reviewed_head = reviewed_names_head and (
        integration.get("contains_reviewed_head") is True
    )
    proof_squash_merged = reviewed_names_head and (
        integration.get("squash_merged") is True
    )
    return bool(
        _text(integration.get("status")).lower() in {"pass", "passed"}
        and integration.get("remote_verified") is True
        and _text(integration.get("canonical_ref")) == _text(canonical_ref)
        and _GIT_SHA_RE.match(head_sha)
        and _GIT_SHA_RE.match(proof_sha)
        and (
            head_sha == proof_sha
            or proof_carries_reviewed_head
            or proof_squash_merged
        )
    )


def _merged_pull_request(manifest: Mapping[str, Any]) -> JsonDict:
    """Return the merged pull request recorded in one evidence manifest.

    A pull request only counts once something asserted it merged. A URL alone
    means "a pull request was opened", which is exactly the pre-merge state a
    duplicate must still be allowed to supersede.
    """

    manifest = _mapping(manifest)
    candidates: List[JsonDict] = []
    repo = _mapping(manifest.get("repo"))
    for holder in (repo, manifest, _mapping(manifest.get("publication"))):
        pull_request = _mapping(holder.get("pull_request"))
        if pull_request:
            candidates.append(pull_request)
        merge = _mapping(holder.get("merge"))
        if merge:
            candidates.append(merge)
    for candidate in candidates:
        merged = (
            candidate.get("merged") is True
            or _text(candidate.get("state")).lower() == "merged"
            or bool(_text(candidate.get("merged_at")))
        )
        if not merged:
            continue
        url = _text(candidate.get("url") or candidate.get("html_url"))
        number = candidate.get("number")
        detail: JsonDict = {"merged": True}
        if url:
            detail["url"] = url
        if number not in (None, ""):
            detail["number"] = number
        sha = _text(candidate.get("merge_commit_sha") or candidate.get("sha"))
        if sha:
            detail["sha"] = sha
        return detail
    return {}


def detect_terminal_evidence(
    task: Mapping[str, Any],
    evidence: Iterable[Mapping[str, Any]] = (),
    *,
    canonical_ref: str = "",
) -> TerminalEvidence:
    """Decide whether *task*'s work already exists.

    ``task`` is a task dict (``Task.to_dict()`` shape). ``evidence`` is that
    task's evidence records, each a mapping with a ``metadata`` key -- oldest
    first, matching ``ControlPlane.list_evidence``; the newest proof wins.
    ``canonical_ref`` is the task's canonical ``refs/heads/<branch>``; when it
    is empty, canonical-integration proofs cannot be checked and only pull
    requests and recorded completions are considered.

    Deliberately non-raising: the claim path needs a verdict it can attach a
    lineage decision to, not an exception.
    """

    task = _mapping(task)
    records = [_mapping(record) for record in evidence or ()]
    for record in reversed(records):
        metadata = _mapping(record.get("metadata"))
        if canonical_ref and evidence_carries_canonical_integration(
            metadata, canonical_ref
        ):
            manifest = _manifest(metadata)
            return TerminalEvidence(
                True,
                TerminalEvidenceKind.CANONICAL_INTEGRATION.value,
                {
                    "evidence_id": _text(record.get("id")),
                    "canonical_ref": _text(canonical_ref),
                    "canonical_integration": _mapping(
                        manifest.get("canonical_integration")
                    ),
                },
            )
    for record in reversed(records):
        pull_request = _merged_pull_request(_manifest(_mapping(record.get("metadata"))))
        if pull_request:
            return TerminalEvidence(
                True,
                TerminalEvidenceKind.MERGED_PULL_REQUEST.value,
                {
                    "evidence_id": _text(record.get("id")),
                    "pull_request": pull_request,
                },
            )
    if _text(task.get("state")).lower() == "completed" and _text(
        task.get("completed_at")
    ):
        return TerminalEvidence(
            True,
            TerminalEvidenceKind.RECORDED_COMPLETION.value,
            {"completed_at": _text(task.get("completed_at"))},
        )
    return NO_TERMINAL_EVIDENCE


def lineage_authorization(task: Mapping[str, Any]) -> JsonDict:
    """Return the explicit replacement/retry authorisation carried by *task*.

    ADR-0121's exemption is narrow and deliberate: terminal evidence blocks a
    claim *unless an explicit replacement or retry row was created*. A row is
    itself that row when its lineage records what it retries or replaces, or
    when an operator acknowledged the terminal evidence through the reopen
    path. A bare ``open`` state is never authorisation.
    """

    lineage = _mapping(_mapping(task.get("metadata")).get(LINEAGE_METADATA_KEY))
    for key in ("retry_of", "replaces", "amends", "terminal_evidence_acknowledged"):
        value = lineage.get(key)
        if isinstance(value, Mapping):
            if value:
                return {"authorized_by": key, "detail": dict(value)}
        elif isinstance(value, (list, tuple)):
            if value:
                return {"authorized_by": key, "detail": {"entries": list(value)}}
        elif _text(value):
            return {"authorized_by": key, "detail": {"value": _text(value)}}
    return {}


def claim_refusal(
    task: Mapping[str, Any],
    evidence: Iterable[Mapping[str, Any]] = (),
    *,
    canonical_ref: str = "",
) -> Optional[JsonDict]:
    """Return why *task* must not be claimed, or ``None`` if it may be.

    The returned mapping is the refusal record: the terminal-evidence verdict
    plus a ``remediation`` naming the two legitimate next actions, so the
    caller's error message can tell an operator what to do instead of merely
    that the claim was denied.
    """

    verdict = detect_terminal_evidence(task, evidence, canonical_ref=canonical_ref)
    if not verdict.present:
        return None
    if lineage_authorization(task):
        return None
    return {
        "task_id": _text(task.get("id")),
        "terminal_evidence": verdict.to_dict(),
        "remediation": (
            "the work for this task already exists (%s); create an explicit "
            "replacement or retry row -- `mac task reopen --replace` or a new "
            "task with lineage.retry_of set -- instead of re-dispatching this "
            "row" % verdict.kind
        ),
    }


def describe_terminal_evidence(verdict: TerminalEvidence) -> str:
    """One-line operator-facing summary of a terminal-evidence verdict."""

    if not verdict.present:
        return "no terminal evidence"
    if verdict.kind == TerminalEvidenceKind.CANONICAL_INTEGRATION.value:
        integration = _mapping(verdict.detail.get("canonical_integration"))
        tip = _text(
            integration.get("canonical_tip_sha") or integration.get("head_sha")
        )
        return "canonical integration verified on %s%s" % (
            _text(verdict.detail.get("canonical_ref")) or "the canonical branch",
            " at %s" % tip[:12] if tip else "",
        )
    if verdict.kind == TerminalEvidenceKind.MERGED_PULL_REQUEST.value:
        pull_request = _mapping(verdict.detail.get("pull_request"))
        label = _text(pull_request.get("url"))
        if not label and pull_request.get("number") not in (None, ""):
            label = "#%s" % pull_request.get("number")
        return "pull request %s is merged" % (label or "(unnamed)")
    return "completion recorded at %s" % (
        _text(verdict.detail.get("completed_at")) or "an unrecorded time"
    )
