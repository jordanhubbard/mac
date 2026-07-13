"""Unit tests for the adversarial-agent auto-land pipeline."""

from __future__ import annotations

import pytest

from mac.auto_land import (
    AutoLandError,
    ContractResult,
    LandDecision,
    ReviewVerdict,
    build_real_dependencies,
    decide_land,
    normalize_verdict,
    record_outcome,
    run_adversarial_review,
    run_auto_land,
    run_contract_gate,
    safe_do_land,
)


GREEN = ContractResult(green=True, summary="ok")
RED = ContractResult(green=False, summary="tests failed")
APPROVE = ReviewVerdict.approve(reviewer="r1")
REJECT = ReviewVerdict.reject(reviewer="r1", findings=["missing test for edge X"])


# ---------------------------------------------------------------------------
# normalize_verdict — default-to-reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["approve", "APPROVE", " Approved ", "lgtm"])
def test_normalize_verdict_approves(token):
    assert normalize_verdict(token) == "approve"


@pytest.mark.parametrize("token", ["reject", "REJECTED", "changes_requested", "blocked"])
def test_normalize_verdict_rejects(token):
    assert normalize_verdict(token) == "reject"


@pytest.mark.parametrize("token", [None, "", "maybe", "unsure", "??"])
def test_normalize_verdict_ambiguous_is_not_approve(token):
    assert normalize_verdict(token) == "ambiguous"


# ---------------------------------------------------------------------------
# decide_land — pure decision core
# ---------------------------------------------------------------------------


def test_green_plus_approve_lands():
    decision = decide_land(GREEN, APPROVE, target="task_abc")
    assert decision.land is True
    assert decision.gate == "landed"
    assert decision.target == "task_abc"


def test_red_plus_approve_does_not_land_contract_gate():
    decision = decide_land(RED, APPROVE)
    assert decision.land is False
    assert decision.gate == "contract"
    assert "RED" in decision.reason


def test_green_plus_reject_does_not_land_review_gate_records_findings():
    decision = decide_land(GREEN, REJECT)
    assert decision.land is False
    assert decision.gate == "review"
    assert "missing test for edge X" in decision.findings
    assert decision.verdict is not None
    assert decision.verdict["verdict"] == "reject"


def test_missing_verdict_does_not_land_default_reject():
    decision = decide_land(GREEN, None)
    assert decision.land is False
    assert decision.gate == "review"
    assert "default-to-reject" in decision.reason


def test_ambiguous_verdict_does_not_land():
    ambiguous = ReviewVerdict.of("maybe", reviewer="r1")
    decision = decide_land(GREEN, ambiguous)
    assert decision.land is False
    assert decision.gate == "review"


def test_missing_contract_does_not_land_default_reject():
    decision = decide_land(None, APPROVE)
    assert decision.land is False
    assert decision.gate == "contract"


def test_red_plus_reject_blocks_on_contract_first():
    decision = decide_land(RED, REJECT)
    assert decision.land is False
    assert decision.gate == "contract"


# ---------------------------------------------------------------------------
# run_auto_land — orchestration with injected fakes
# ---------------------------------------------------------------------------


class _Spy:
    def __init__(self, contract, verdict):
        self.contract = contract
        self.verdict = verdict
        self.landed = []
        self.recorded = []

    def run_contract(self, target):
        return self.contract

    def run_review(self, target):
        return self.verdict

    def do_land(self, target, decision):
        self.landed.append((target, decision))

    def record(self, target, decision):
        self.recorded.append((target, decision))


def _run(spy, target="task_x"):
    return run_auto_land(
        target,
        run_contract=spy.run_contract,
        run_review=spy.run_review,
        do_land=spy.do_land,
        record=spy.record,
    )


def test_orchestration_green_approve_lands_once_and_records():
    spy = _Spy(GREEN, APPROVE)
    decision = _run(spy)
    assert decision.land is True
    assert len(spy.landed) == 1
    assert spy.landed[0][0] == "task_x"
    assert len(spy.recorded) == 1
    assert spy.recorded[0][1] is decision


def test_orchestration_red_approve_does_not_land_but_records():
    spy = _Spy(RED, APPROVE)
    decision = _run(spy)
    assert decision.land is False
    assert spy.landed == []
    assert len(spy.recorded) == 1
    assert spy.recorded[0][1].gate == "contract"


def test_orchestration_green_reject_does_not_land_records_findings():
    spy = _Spy(GREEN, REJECT)
    decision = _run(spy)
    assert decision.land is False
    assert spy.landed == []
    assert len(spy.recorded) == 1
    assert "missing test for edge X" in spy.recorded[0][1].findings


def test_orchestration_missing_verdict_does_not_land_but_records():
    spy = _Spy(GREEN, None)
    decision = _run(spy)
    assert decision.land is False
    assert spy.landed == []
    assert len(spy.recorded) == 1


def test_record_always_called_even_when_do_land_raises():
    spy = _Spy(GREEN, APPROVE)

    def boom(target, decision):
        raise AutoLandError("land blew up")

    with pytest.raises(AutoLandError):
        run_auto_land(
            "task_x",
            run_contract=spy.run_contract,
            run_review=spy.run_review,
            do_land=boom,
            record=spy.record,
        )
    assert len(spy.recorded) == 1


# ---------------------------------------------------------------------------
# Real dependency helpers (with injected runners)
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_contract_gate_green_on_exit_zero():
    result = run_contract_gate(".", runner=lambda argv, cwd=None: _Proc(0, "all pass"))
    assert result.green is True
    assert result.details["returncode"] == 0


def test_run_contract_gate_red_on_nonzero():
    result = run_contract_gate(".", runner=lambda argv, cwd=None: _Proc(1, "boom"))
    assert result.green is False
    assert result.details["returncode"] == 1


class _Choice:
    def __init__(self, available=True, agent="claude"):
        self.available = available
        self.agent = agent


def test_adversarial_review_parses_approve():
    verdict = run_adversarial_review(
        "task_x",
        runner=lambda argv, cwd=None: _Proc(0, "analysis...\nVERDICT: APPROVE\nFINDINGS: none"),
        resolve=lambda env=None: _Choice(),
        build_argv=lambda choice, prompt, env=None: ["true"],
    )
    assert verdict.is_approve is True


def test_adversarial_review_parses_reject_with_findings():
    verdict = run_adversarial_review(
        "task_x",
        runner=lambda argv, cwd=None: _Proc(
            0, "VERDICT: REJECT\nFINDINGS: bug A; weak test B"
        ),
        resolve=lambda env=None: _Choice(),
        build_argv=lambda choice, prompt, env=None: ["true"],
    )
    assert verdict.is_approve is False
    assert "bug A" in verdict.findings and "weak test B" in verdict.findings


def test_adversarial_review_missing_verdict_is_not_approve():
    verdict = run_adversarial_review(
        "task_x",
        runner=lambda argv, cwd=None: _Proc(0, "I have no opinion"),
        resolve=lambda env=None: _Choice(),
        build_argv=lambda choice, prompt, env=None: ["true"],
    )
    assert verdict.is_approve is False
    assert verdict.verdict == "ambiguous"


def test_adversarial_review_fails_closed_without_agent():
    verdict = run_adversarial_review(
        "task_x",
        resolve=lambda env=None: _Choice(available=False, agent=""),
    )
    assert verdict.is_approve is False
    assert verdict.verdict == "reject"


# ---------------------------------------------------------------------------
# safe_do_land — never lands without a land decision, records evidence
# ---------------------------------------------------------------------------


class _FakePlane:
    def __init__(self):
        self.evidence = []

    def add_evidence(self, target, kind, uri, summary, created_by, metadata=None):
        self.evidence.append((target, kind, summary, metadata))


def _land_decision(target="task_x"):
    return decide_land(GREEN, APPROVE, target=target)


def test_safe_do_land_refuses_without_land_decision():
    no_land = decide_land(GREEN, REJECT, target="task_x")
    with pytest.raises(AutoLandError):
        safe_do_land("task_x", no_land)


def test_safe_do_land_task_target_marks_ready_to_land():
    plane = _FakePlane()
    result = safe_do_land("task_x", _land_decision(), plane=plane)
    assert result["action"] == "ready-to-land"
    assert "merge_gate" not in result  # task id -> no branch merge check
    assert plane.evidence and plane.evidence[0][1] == "auto_land_ready"


def test_safe_do_land_branch_checks_merge_gate_and_can_push():
    calls = []

    def git_runner(repo_dir, argv):
        calls.append(argv)
        if argv[0] == "rev-parse":
            return _Proc(0, "deadbeef")
        if argv[0] == "merge-tree":
            return _Proc(0, "treeoid")  # clean
        if argv[0] == "push":
            return _Proc(0, "pushed")
        return _Proc(0, "")

    result = safe_do_land(
        "feature/x",
        _land_decision("feature/x"),
        repo_dir="/repo",
        base_ref="main",
        allow_push=True,
        git_runner=git_runner,
    )
    assert result["action"] == "pushed"
    assert result["merge_gate"]["clean"] is True
    assert ["push", "origin", "feature/x"] in calls


def test_safe_do_land_branch_aborts_on_dirty_merge_gate():
    def git_runner(repo_dir, argv):
        if argv[0] == "rev-parse":
            # distinct SHAs for base vs topic so the merge is non-trivial
            return _Proc(0, "base111" if "main" in argv[-1] else "topic222")
        if argv[0] == "merge-tree":
            # rc=1 => conflicts
            return _Proc(1, "treeoid\n\nsrc/conflict.py")
        return _Proc(0, "")

    with pytest.raises(AutoLandError):
        safe_do_land(
            "feature/x",
            _land_decision("feature/x"),
            repo_dir="/repo",
            base_ref="main",
            allow_push=True,
            git_runner=git_runner,
        )


# ---------------------------------------------------------------------------
# record_outcome + build_real_dependencies
# ---------------------------------------------------------------------------


def test_record_outcome_persists_evidence():
    plane = _FakePlane()
    payload = record_outcome("task_x", _land_decision(), plane=plane)
    assert payload["land"] is True
    assert plane.evidence and plane.evidence[0][1] == "auto_land_decision"


def test_record_outcome_best_effort_when_plane_raises():
    class _Boom:
        def add_evidence(self, *a, **k):
            raise RuntimeError("no such task")

    payload = record_outcome("task_x", _land_decision(), plane=_Boom())
    assert "record_error" in payload


def test_build_real_dependencies_returns_four_callables():
    deps = build_real_dependencies(repo_dir=".", base_ref="main")
    assert set(deps) == {"run_contract", "run_review", "do_land", "record"}
    assert all(callable(v) for v in deps.values())


def test_land_decision_to_dict_shape():
    d = _land_decision().to_dict()
    assert d["schema"] == "mac.auto_land.decision.v1"
    assert d["land"] is True
    assert d["gate"] == "landed"
