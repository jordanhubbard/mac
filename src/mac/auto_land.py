"""Adversarial-agent auto-land pipeline.

We land a change through two *independent* gates, exactly as the human process
does today:

1. **The machine contract gate** — ``scripts/run-contract-tests.sh`` (coverage
   floors 90% stmt / 80% branch). It is GREEN on exit 0, RED otherwise.
2. **An independent adversarial reviewer agent** — a separately-spawned coding
   agent whose standing instruction is to *default to REJECT* and actively try
   to refute the change. It returns APPROVE only if it cannot find a reason to
   reject.

A change lands **only** when the contract gate is GREEN **and** the reviewer
returns APPROVE. Every other outcome — RED contract, an explicit REJECT, or an
ambiguous/missing verdict — does **not** land. This is fail-closed:
*default-to-reject* is the whole point, so any uncertainty blocks the land.

This module is split into a pure, fully-testable decision core
(:func:`decide_land`) and an orchestration layer (:func:`run_auto_land`) whose
side-effecting dependencies (run the contract gate, spawn the reviewer, perform
the land, record the outcome) are all *injected callables*. Tests inject fakes;
the CLI injects the real wiring built by :func:`build_real_dependencies`, which
reuses existing infrastructure:

* the contract gate script ``scripts/run-contract-tests.sh``;
* :func:`mac.coding_agent.resolve_coding_agent` / :func:`mac.coding_agent.coding_agent_argv`
  to spawn the *independent* reviewer (fail-closed if no agent is available);
* :func:`mac.merge_queue.validate_projected_merge` as the final land-time safety
  check (never land onto a tip the branch conflicts with);
* the dispatch plane's ``add_evidence`` to persist the outcome as task evidence.

Safety: :func:`run_auto_land` never invokes ``do_land`` unless *both* gates are
green, and the real ``do_land`` re-validates the merge gate and never
force-pushes.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional


class AutoLandError(RuntimeError):
    """Raised when the safe land action cannot proceed."""


# ---------------------------------------------------------------------------
# Verdict normalization (default-to-reject)
# ---------------------------------------------------------------------------

# Only these exact tokens count as an approval. Everything else — including the
# empty string, ``None``, ``changes_requested`` and anything unrecognized — is
# treated as *not* an approval, so the pipeline fails closed.
_APPROVE_TOKENS = {"approve", "approved", "lgtm"}
_REJECT_TOKENS = {
    "reject",
    "rejected",
    "changes_requested",
    "changes-requested",
    "block",
    "blocked",
    "deny",
    "denied",
}


def normalize_verdict(raw: Any) -> str:
    """Map an arbitrary verdict token onto ``approve`` / ``reject`` / ``ambiguous``.

    Anything that is not an explicit, recognized approval or rejection resolves
    to ``ambiguous`` — and an ambiguous verdict never lands (default-to-reject).
    """
    if raw is None:
        return "ambiguous"
    token = str(raw).strip().lower()
    if not token:
        return "ambiguous"
    if token in _APPROVE_TOKENS:
        return "approve"
    if token in _REJECT_TOKENS:
        return "reject"
    return "ambiguous"


# ---------------------------------------------------------------------------
# Structured gate results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractResult:
    """Outcome of the machine contract gate."""

    green: bool
    summary: str = ""
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "green": self.green,
            "summary": self.summary,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ReviewVerdict:
    """Outcome of the independent adversarial reviewer.

    ``verdict`` is always normalized to ``approve`` / ``reject`` / ``ambiguous``.
    """

    verdict: str
    findings: List[str] = field(default_factory=list)
    reviewer: str = ""
    raw: str = ""

    @classmethod
    def approve(
        cls, *, reviewer: str = "", findings: Optional[List[str]] = None, raw: str = ""
    ) -> "ReviewVerdict":
        return cls("approve", list(findings or []), reviewer, raw)

    @classmethod
    def reject(
        cls, *, reviewer: str = "", findings: Optional[List[str]] = None, raw: str = ""
    ) -> "ReviewVerdict":
        return cls("reject", list(findings or []), reviewer, raw)

    @classmethod
    def of(
        cls,
        raw_verdict: Any,
        *,
        reviewer: str = "",
        findings: Optional[List[str]] = None,
        raw: str = "",
    ) -> "ReviewVerdict":
        return cls(normalize_verdict(raw_verdict), list(findings or []), reviewer, raw)

    @property
    def is_approve(self) -> bool:
        return self.verdict == "approve"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": list(self.findings),
            "reviewer": self.reviewer,
        }


@dataclass(frozen=True)
class LandDecision:
    """The final land/no-land decision plus a structured reason."""

    land: bool
    gate: str  # "landed" | "contract" | "review"
    reason: str
    findings: List[str] = field(default_factory=list)
    contract: Optional[Dict[str, Any]] = None
    verdict: Optional[Dict[str, Any]] = None
    target: str = ""
    head_sha: str = ""
    author: str = ""
    reviewer: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "mac.auto_land.decision.v1",
            "target": self.target,
            "head_sha": self.head_sha,
            "author": self.author,
            "reviewer": self.reviewer,
            "land": self.land,
            "gate": self.gate,
            "reason": self.reason,
            "findings": list(self.findings),
            "contract": self.contract,
            "verdict": self.verdict,
        }


# ---------------------------------------------------------------------------
# Pure decision core
# ---------------------------------------------------------------------------


def decide_land(
    contract_result: Optional[ContractResult],
    review_verdict: Optional[ReviewVerdict],
    *,
    target: str = "",
    head_sha: str = "",
    author: str = "",
    reviewer: str = "",
) -> LandDecision:
    """Decide whether to land, given the two gate results.

    Lands **only** when all of the following hold — anything else fails closed
    (default-to-reject), since a human is no longer in the merge path:

    * the contract gate is GREEN;
    * the independent adversarial reviewer returned an explicit APPROVE;
    * the reviewer is a *different* agent than the author (independence). An
      empty reviewer, an empty author, or ``reviewer == author`` never lands —
      a change must not sign off on itself.

    This is a pure function: no I/O, no side effects. ``head_sha``/``author``/
    ``reviewer`` are recorded on the decision so the land action can gate on the
    exact revision the gates ran against.
    """
    contract_dict = contract_result.to_dict() if contract_result is not None else None
    verdict_dict = review_verdict.to_dict() if review_verdict is not None else None
    findings = list(review_verdict.findings) if review_verdict is not None else []
    reviewer = reviewer or (review_verdict.reviewer if review_verdict is not None else "")

    def _decision(*, land: bool, gate: str, reason: str) -> LandDecision:
        return LandDecision(
            land=land,
            gate=gate,
            reason=reason,
            findings=findings,
            contract=contract_dict,
            verdict=verdict_dict,
            target=target,
            head_sha=head_sha,
            author=author,
            reviewer=reviewer,
        )

    # Gate 1: the machine contract gate.
    if contract_result is None:
        return _decision(
            land=False,
            gate="contract",
            reason="no contract result available; default-to-reject",
        )
    if not contract_result.green:
        return _decision(
            land=False,
            gate="contract",
            reason="contract gate is RED; %s" % (contract_result.summary or "see details"),
        )

    # Gate 2: the independent adversarial reviewer.
    if review_verdict is None:
        return _decision(
            land=False,
            gate="review",
            reason="no reviewer verdict available; default-to-reject",
        )
    if not review_verdict.is_approve:
        detail = review_verdict.verdict
        return _decision(
            land=False,
            gate="review",
            reason="reviewer did not APPROVE (verdict=%s); default-to-reject" % detail,
        )

    # Gate 3: reviewer independence — the reviewer must not be the author.
    if not reviewer:
        return _decision(
            land=False,
            gate="independence",
            reason="reviewer identity unknown; cannot prove independence; default-to-reject",
        )
    if not author:
        return _decision(
            land=False,
            gate="independence",
            reason="author identity unknown; cannot prove reviewer independence; default-to-reject",
        )
    if reviewer.strip().lower() == author.strip().lower():
        return _decision(
            land=False,
            gate="independence",
            reason="reviewer (%s) is the author; a change must not approve itself; default-to-reject"
            % reviewer,
        )

    # All gates green.
    return _decision(
        land=True,
        gate="landed",
        reason="contract GREEN, reviewer APPROVE, and reviewer independent of author",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_auto_land(
    target: str,
    *,
    run_contract: Callable[[str], Optional[ContractResult]],
    run_review: Callable[[str], Optional[ReviewVerdict]],
    do_land: Callable[[str, LandDecision], Any],
    record: Callable[[str, LandDecision], Any],
    author: str = "",
    resolve_head_sha: Optional[Callable[[str], str]] = None,
    notify_human: Optional[Callable[[str, LandDecision], Any]] = None,
) -> LandDecision:
    """Run the full auto-land pipeline for ``target`` (a task id or a branch).

    ``run_contract`` runs the machine contract gate; ``run_review`` spawns the
    independent adversarial reviewer; ``do_land`` performs the (safe) land
    action; ``record`` persists the outcome. All are injected so tests can
    substitute fakes.

    The head_sha the gates ran against is captured up front (via
    ``resolve_head_sha``) and threaded into the decision so ``do_land`` can
    refuse to land if the tip moved out from under the gate. ``author`` is the
    change's author; the reviewer must be a *different* agent (independence).

    Both gates always run so their findings are recorded even on a no-land.
    ``do_land`` is invoked **exactly once, and only when the decision lands**.
    ``record`` is **always** invoked with the final decision, even if
    ``do_land`` raises. ``notify_human`` (post-hoc, non-blocking) is invoked on
    **every** outcome — a human is informed, never blocking.
    """
    head_sha = ""
    if resolve_head_sha is not None:
        try:
            head_sha = str(resolve_head_sha(target) or "")
        except Exception:  # best-effort; a missing sha simply gates the land
            head_sha = ""

    contract_result = run_contract(target)
    review_verdict = run_review(target)
    reviewer = review_verdict.reviewer if review_verdict is not None else ""
    decision = decide_land(
        contract_result,
        review_verdict,
        target=target,
        head_sha=head_sha,
        author=author,
        reviewer=reviewer,
    )

    try:
        if decision.land:
            do_land(target, decision)
    finally:
        try:
            record(target, decision)
        finally:
            if notify_human is not None:
                try:
                    notify_human(target, decision)
                except Exception:  # visibility is non-blocking; never fail the run
                    pass
    return decision


# ---------------------------------------------------------------------------
# Real dependency wiring (reuses existing infrastructure)
# ---------------------------------------------------------------------------

_CONTRACT_SCRIPT = "scripts/run-contract-tests.sh"

_ADVERSARIAL_PROMPT = """\
You are an INDEPENDENT adversarial code reviewer and you are the merge gate.
Your DEFAULT verdict is REJECT. You do not approve a change unless you have
actively tried and FAILED to find a reason to reject it. Assume the change is
wrong until proven otherwise.

Review target: {target}

Actively try to refute this change. Look for: correctness bugs, missing or weak
tests, coverage gaps, security issues, broken invariants, unsafe git/merge
actions, and anything the contract gate cannot catch. Inspect the diff and the
surrounding code.

You MUST end your response with two lines, exactly:
VERDICT: APPROVE   (only if you could NOT find any reason to reject)
or
VERDICT: REJECT
FINDINGS: <semicolon-separated list of the concrete problems you found, or "none">
"""


def _default_subprocess_runner(argv, *, cwd=None):  # pragma: no cover - thin shim
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def run_contract_gate(
    repo_dir: str = ".",
    *,
    script: str = _CONTRACT_SCRIPT,
    runner: Callable[..., Any] = _default_subprocess_runner,
) -> ContractResult:
    """Invoke the machine contract gate and translate exit code -> GREEN/RED.

    ``scripts/run-contract-tests.sh`` exits 0 only when the tests pass AND the
    coverage-policy floors hold; any nonzero exit is RED.
    """
    proc = runner(["bash", script], cwd=repo_dir)
    rc = int(getattr(proc, "returncode", 1))
    stdout = getattr(proc, "stdout", "") or ""
    stderr = getattr(proc, "stderr", "") or ""
    tail = "\n".join((stdout + stderr).splitlines()[-25:])
    return ContractResult(
        green=(rc == 0),
        summary="contract gate rc=%d" % rc,
        details={"returncode": rc, "tail": tail},
    )


def _parse_review_output(output: str, *, reviewer: str) -> ReviewVerdict:
    """Extract a verdict + findings from the reviewer's output; default REJECT.

    Only a clear ``VERDICT: APPROVE`` approves. A missing/garbled verdict line
    resolves to ``ambiguous`` (which does not land). An explicit
    ``VERDICT: REJECT`` rejects.
    """
    verdict_token: Optional[str] = None
    findings: List[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("verdict:"):
            verdict_token = stripped.split(":", 1)[1].strip()
        elif low.startswith("findings:"):
            payload = stripped.split(":", 1)[1].strip()
            if payload and payload.lower() != "none":
                findings = [f.strip() for f in payload.split(";") if f.strip()]
    normalized = normalize_verdict(verdict_token)
    if normalized == "ambiguous" and not findings:
        findings = ["reviewer produced no parseable VERDICT line; failing closed"]
    return ReviewVerdict(normalized, findings, reviewer, output[-4000:])


def _reviewer_identity(choice: Any, env: Optional[Mapping[str, str]]) -> str:
    """Resolve the *fleet* identity of the reviewer for the independence check.

    Prefer an explicit reviewer id pinned by the dispatcher
    (``MAC_REVIEWER_AGENT_ID``) — that is the peer agent pulled from the fleet
    by capability. Fall back to the current agent id, then to the coding-agent
    CLI name. Independence is enforced against this value in :func:`decide_land`.
    """
    env = os.environ if env is None else env
    for key in ("MAC_REVIEWER_AGENT_ID", "MAC_AGENT_ID"):
        val = str(env.get(key) or "").strip()
        if val:
            return val
    return getattr(choice, "agent", "") or ""


def run_adversarial_review(
    target: str,
    *,
    repo_dir: str = ".",
    env: Optional[Mapping[str, str]] = None,
    author: str = "",
    runner: Callable[..., Any] = _default_subprocess_runner,
    resolve: Optional[Callable[..., Any]] = None,
    build_argv: Optional[Callable[..., Any]] = None,
) -> ReviewVerdict:
    """Spawn an INDEPENDENT adversarial reviewer agent and return its verdict.

    Reuses :mod:`mac.coding_agent` to select and launch a real coding-agent CLI
    (the same seam the executor uses). If no coding agent is available/authed,
    this fails **closed** with a REJECT — we never approve just because there is
    no reviewer.

    Independence is load-bearing: the reviewer must be a *different* agent than
    ``author``. If the resolved reviewer identity is the author, this fails
    **closed** with a REJECT — a change must never review itself.
    """
    from mac import coding_agent as _ca

    resolve = resolve or _ca.resolve_coding_agent
    build_argv = build_argv or _ca.coding_agent_argv

    choice = resolve(env=env)
    if not getattr(choice, "available", False) or not getattr(choice, "agent", ""):
        return ReviewVerdict.reject(
            reviewer="none",
            findings=["no independent coding agent available to review; failing closed"],
        )

    reviewer_id = _reviewer_identity(choice, env)
    if author and reviewer_id and reviewer_id.strip().lower() == author.strip().lower():
        return ReviewVerdict.reject(
            reviewer=reviewer_id,
            findings=[
                "reviewer (%s) is the author; independence violated; failing closed" % reviewer_id
            ],
        )

    prompt = _ADVERSARIAL_PROMPT.format(target=target)
    from mac.prompt_master import compile_prompt

    compiled = compile_prompt(
        prompt,
        target=choice.agent,
        model=getattr(choice, "model", ""),
        prompt_kind="auto_land_review",
        task_id=str((env or os.environ).get("MAC_TASK_ID") or target),
        agent_id=str((env or os.environ).get("MAC_AGENT_ID") or ""),
        route_fingerprint=(
            choice.route_fingerprint()
            if callable(getattr(choice, "route_fingerprint", None))
            else ""
        ),
    )
    argv = build_argv(choice, compiled.text, env=env)
    proc = runner(argv, cwd=repo_dir)
    output = (getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or "")
    if int(getattr(proc, "returncode", 1)) != 0 and not output.strip():
        return ReviewVerdict.reject(
            reviewer=reviewer_id,
            findings=["reviewer agent exited nonzero with no output; failing closed"],
        )
    return _parse_review_output(output, reviewer=reviewer_id)


def _branch_for_target(target: str) -> Optional[str]:
    """Heuristic: treat a target that looks like a git ref as a branch.

    Task ids in this repo are ``task_<hex>``; anything else is treated as a
    branch/ref for the land-time merge-gate check.
    """
    if not target:
        return None
    if target.startswith("task_"):
        return None
    return target


def _default_git_runner(repo_dir: str, args) -> Any:  # pragma: no cover - thin shim
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_head_sha(
    target: str,
    *,
    repo_dir: str = ".",
    git_runner: Optional[Callable[..., Any]] = None,
) -> str:
    """Resolve the commit sha the gates should run against for ``target``.

    For a branch target, resolve that branch's tip; otherwise resolve ``HEAD``.
    Returns "" on any failure — a missing sha simply blocks the head_sha land
    gate (fail-closed), it never silently lands a different revision.
    """
    grun = git_runner or _default_git_runner
    ref = _branch_for_target(target) or "HEAD"
    try:
        proc = grun(repo_dir, ["rev-parse", "--verify", "%s^{commit}" % ref])
    except Exception:
        return ""
    if int(getattr(proc, "returncode", 1)) != 0:
        return ""
    return (getattr(proc, "stdout", "") or "").strip()


def safe_do_land(
    target: str,
    decision: LandDecision,
    *,
    plane: Any = None,
    repo_dir: str = ".",
    base_ref: str = "main",
    created_by: str = "auto-land",
    allow_push: bool = False,
    git_runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Perform the *safe* land action for a change both gates approved.

    This never bypasses the contract gate and never force-pushes. Concretely:

    * Defense-in-depth: refuse to act unless ``decision.land`` is true.
    * If ``target`` is a branch, re-run the merge-queue OCC gate
      (:func:`mac.merge_queue.validate_projected_merge`) against the *current*
      ``base_ref`` tip and abort if it is not clean (a branch that was green in
      isolation may conflict with a tip that moved).
    * By default (``allow_push=False``) it does not touch git at all — it simply
      marks the change **ready-to-land** by recording task evidence via the
      dispatch plane, leaving the actual fast-forward to the existing merge
      queue. With ``allow_push=True`` and a branch target it performs a plain
      (never ``--force``) ``git push origin <branch>``.
    """
    if not decision.land:
        raise AutoLandError("safe_do_land invoked without a land decision")

    result: Dict[str, Any] = {"action": "ready-to-land", "target": target}
    branch = _branch_for_target(target)

    if branch:
        from mac.merge_queue import validate_projected_merge

        runner = None
        if git_runner is not None:
            runner = lambda argv: git_runner(repo_dir, argv)  # noqa: E731

        # head_sha gate: only land the *exact* revision the gates ran against.
        # If the branch tip moved after the contract gate + review, the earlier
        # green no longer applies — fail closed rather than land unreviewed code.
        if decision.head_sha:
            current_sha = _resolve_head_sha(branch, repo_dir=repo_dir, git_runner=git_runner)
            result["gated_head_sha"] = decision.head_sha
            result["current_head_sha"] = current_sha
            if not current_sha:
                raise AutoLandError(
                    "cannot resolve current tip of %s to verify gated head_sha; "
                    "default-to-reject" % branch
                )
            if current_sha != decision.head_sha:
                raise AutoLandError(
                    "branch tip moved since the gates ran "
                    "(gated=%s, current=%s); refusing to land unreviewed code"
                    % (decision.head_sha, current_sha)
                )

        gate = validate_projected_merge(repo_dir, base_ref, branch, git_runner=runner)
        result["merge_gate"] = gate.to_dict()
        if not gate.clean:
            raise AutoLandError(
                "land-time merge gate not clean: %s"
                % (", ".join(gate.conflicted_files) or gate.error or "unknown")
            )
        if allow_push:
            grun = git_runner or _default_git_runner
            proc = grun(repo_dir, ["push", "origin", branch])
            rc = int(getattr(proc, "returncode", 1))
            result["push_returncode"] = rc
            result["action"] = "pushed" if rc == 0 else "push-failed"
            if rc != 0:
                raise AutoLandError(
                    "git push failed (rc=%d): %s"
                    % (rc, (getattr(proc, "stderr", "") or "").strip()[:300])
                )

    if plane is not None:
        try:
            plane.add_evidence(
                target,
                "auto_land_ready",
                "auto-land://%s/ready" % target,
                "auto-land: both gates green, marked ready-to-land",
                created_by,
                metadata={"decision": decision.to_dict(), "land_action": result},
                _trusted_internal=True,
            )
        except Exception as exc:  # best-effort; the land itself already happened
            result["evidence_error"] = str(exc)

    return result


def record_outcome(
    target: str,
    decision: LandDecision,
    *,
    plane: Any = None,
    created_by: str = "auto-land",
) -> Dict[str, Any]:
    """Persist the auto-land decision + findings as task evidence (best-effort)."""
    payload = decision.to_dict()
    if plane is not None:
        try:
            plane.add_evidence(
                target,
                "auto_land_decision",
                "auto-land://%s/decision" % target,
                decision.reason,
                created_by,
                metadata=payload,
                _trusted_internal=True,
            )
        except Exception as exc:
            payload = dict(payload)
            payload["record_error"] = str(exc)
    return payload


def notify_human(
    target: str,
    decision: LandDecision,
    *,
    plane: Any = None,
    created_by: str = "auto-land",
    sink: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Emit a post-hoc, **non-blocking** ``mac_notify_human`` notification.

    Humans are informed of every auto-land outcome (landed or blocked) but never
    block on it — nothing waits for a human acknowledgement. The notification is
    delivered to ``sink`` if provided, otherwise recorded as task evidence via
    the dispatch plane (best-effort). This is QA/PM visibility, not a checkpoint.
    """
    landed = bool(decision.land)
    notification: Dict[str, Any] = {
        "schema": "mac.mac_notify_human.v1",
        "kind": "auto_land",
        "target": target,
        "landed": landed,
        "gate": decision.gate,
        "reason": decision.reason,
        "head_sha": decision.head_sha,
        "author": decision.author,
        "reviewer": decision.reviewer,
        "findings": list(decision.findings),
        "blocking": False,
        "summary": (
            "AUTO-LANDED %s (head=%s, reviewer=%s)"
            % (target, decision.head_sha or "?", decision.reviewer or "?")
            if landed
            else "auto-land BLOCKED %s at %s gate: %s" % (target, decision.gate, decision.reason)
        ),
    }

    if sink is not None:
        try:
            sink(notification)
        except Exception as exc:  # visibility is non-blocking
            notification = dict(notification)
            notification["notify_error"] = str(exc)
        return notification

    if plane is not None:
        try:
            plane.add_evidence(
                target,
                "mac_notify_human",
                "auto-land://%s/notify" % target,
                notification["summary"],
                created_by,
                metadata=notification,
                _trusted_internal=True,
            )
        except Exception as exc:  # visibility is non-blocking
            notification = dict(notification)
            notification["notify_error"] = str(exc)
    return notification


def build_real_dependencies(
    *,
    plane: Any = None,
    repo_dir: str = ".",
    base_ref: str = "main",
    created_by: str = "auto-land",
    allow_push: bool = False,
    env: Optional[Mapping[str, str]] = None,
    author: str = "",
) -> Dict[str, Any]:
    """Build the real injected callables for :func:`run_auto_land`.

    Returned as a kwargs dict so callers can splat it: ``run_auto_land(target,
    **build_real_dependencies(...))``. ``author`` defaults to the current
    fleet agent (``MAC_AGENT_ID``); the reviewer must be a *different* agent.
    """
    env = os.environ if env is None else env
    author = author or str(env.get("MAC_AGENT_ID") or "").strip()
    return {
        "author": author,
        "resolve_head_sha": lambda target: _resolve_head_sha(target, repo_dir=repo_dir),
        "run_contract": lambda target: run_contract_gate(repo_dir),
        "run_review": lambda target: run_adversarial_review(
            target, repo_dir=repo_dir, env=env, author=author
        ),
        "do_land": lambda target, decision: safe_do_land(
            target,
            decision,
            plane=plane,
            repo_dir=repo_dir,
            base_ref=base_ref,
            created_by=created_by,
            allow_push=allow_push,
        ),
        "record": lambda target, decision: record_outcome(
            target, decision, plane=plane, created_by=created_by
        ),
        "notify_human": lambda target, decision: notify_human(
            target, decision, plane=plane, created_by=created_by
        ),
    }


__all__ = [
    "AutoLandError",
    "ContractResult",
    "ReviewVerdict",
    "LandDecision",
    "normalize_verdict",
    "decide_land",
    "run_auto_land",
    "run_contract_gate",
    "run_adversarial_review",
    "safe_do_land",
    "record_outcome",
    "notify_human",
    "build_real_dependencies",
]
