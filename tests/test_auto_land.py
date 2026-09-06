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
    notify_human,
    record_outcome,
    run_adversarial_review,
    run_auto_land,
    run_contract_gate,
    safe_do_land,
)


GREEN = ContractResult(green=True, summary="ok")
RED = ContractResult(green=False, summary="tests failed")
AUTHOR = "author_agent"
REVIEWER = "reviewer_agent"  # a *different* agent than AUTHOR
APPROVE = ReviewVerdict.approve(reviewer=REVIEWER)
REJECT = ReviewVerdict.reject(reviewer=REVIEWER, findings=["missing test for edge X"])


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
    decision = decide_land(GREEN, APPROVE, target="task_abc", author=AUTHOR)
    assert decision.land is True
    assert decision.gate == "landed"
    assert decision.target == "task_abc"
    assert decision.author == AUTHOR
    assert decision.reviewer == REVIEWER


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


def _run(spy, target="task_x", author=AUTHOR):
    return run_auto_land(
        target,
        run_contract=spy.run_contract,
        run_review=spy.run_review,
        do_land=spy.do_land,
        record=spy.record,
        author=author,
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
            author=AUTHOR,
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


def test_run_contract_gate_defaults_to_mac_own_script_via_bash_dash_c():
    recorded = {}

    def runner(argv, cwd=None):
        recorded["argv"] = argv
        recorded["cwd"] = cwd
        return _Proc(0, "all pass")

    result = run_contract_gate(".", runner=runner)
    assert result.green is True
    # "mac"'s own contract script is a bare path; bash -c must still run it
    # correctly (no regression for the primary repo).
    assert recorded["argv"] == ["bash", "-c", "scripts/run-contract-tests.sh"]


def test_run_contract_gate_runs_a_foreign_repos_compound_test_command():
    recorded = {}

    def runner(argv, cwd=None):
        recorded["argv"] = argv
        return _Proc(0, "1 passed")

    result = run_contract_gate(
        "/some/other/repo",
        command=".venv/bin/pytest -q && .venv/bin/mypy toolkit",
        runner=runner,
    )
    assert result.green is True
    # A compound shell command (&&) must run via `bash -c`, never treated as
    # a filename -- `bash <that string>` would fail with "No such file".
    assert recorded["argv"] == [
        "bash",
        "-c",
        ".venv/bin/pytest -q && .venv/bin/mypy toolkit",
    ]


def test_resolve_target_test_command_uses_the_targets_own_repository_contract():
    from mac.auto_land import _resolve_target_test_command

    class _FakeTask:
        metadata = {
            "execution_contract": {
                "repository_contract": {
                    "test": {"command": ".venv/bin/pytest -q && .venv/bin/mypy toolkit"}
                }
            }
        }

    class _FakePlane:
        def get_task(self, target):
            assert target == "task_abc123"
            return _FakeTask()

    command = _resolve_target_test_command("task_abc123", plane=_FakePlane())
    assert command == ".venv/bin/pytest -q && .venv/bin/mypy toolkit"


def test_resolve_target_test_command_falls_back_to_none_for_a_non_task_target():
    from mac.auto_land import _resolve_target_test_command

    class _RaisingPlane:
        def get_task(self, target):
            raise LookupError("not a task id")

    assert _resolve_target_test_command("some-branch", plane=_RaisingPlane()) is None
    assert _resolve_target_test_command("task_x", plane=None) is None


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
        runner=lambda argv, cwd=None: _Proc(0, "VERDICT: REJECT\nFINDINGS: bug A; weak test B"),
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

    def add_evidence(self, target, kind, uri, summary, created_by, metadata=None, **_kwargs):
        self.evidence.append((target, kind, summary, metadata))


def _land_decision(target="task_x"):
    return decide_land(GREEN, APPROVE, target=target, author=AUTHOR)


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


def test_build_real_dependencies_returns_expected_callables():
    deps = build_real_dependencies(repo_dir=".", base_ref="main", author=AUTHOR)
    assert set(deps) == {
        "author",
        "resolve_head_sha",
        "run_contract",
        "run_review",
        "do_land",
        "record",
        "notify_human",
    }
    assert deps["author"] == AUTHOR
    assert all(callable(v) for k, v in deps.items() if k != "author")


def test_land_decision_to_dict_shape():
    d = _land_decision().to_dict()
    assert d["schema"] == "mac.auto_land.decision.v1"
    assert d["land"] is True
    assert d["gate"] == "landed"


# ---------------------------------------------------------------------------
# Reviewer independence (author != reviewer)
# ---------------------------------------------------------------------------


def test_reviewer_equal_author_does_not_land_independence_gate():
    same = ReviewVerdict.approve(reviewer=AUTHOR)
    decision = decide_land(GREEN, same, target="task_x", author=AUTHOR)
    assert decision.land is False
    assert decision.gate == "independence"
    assert "author" in decision.reason.lower()


def test_missing_author_does_not_land_independence_gate():
    decision = decide_land(GREEN, APPROVE, target="task_x", author="")
    assert decision.land is False
    assert decision.gate == "independence"


def test_missing_reviewer_identity_does_not_land():
    anon = ReviewVerdict.approve(reviewer="")
    decision = decide_land(GREEN, anon, target="task_x", author=AUTHOR)
    assert decision.land is False
    assert decision.gate == "independence"


def test_run_adversarial_review_fails_closed_when_reviewer_is_author():
    verdict = run_adversarial_review(
        "task_x",
        env={"MAC_REVIEWER_AGENT_ID": "agent_dup"},
        author="agent_dup",
        runner=lambda argv, cwd=None: _Proc(0, "VERDICT: APPROVE\nFINDINGS: none"),
        resolve=lambda env=None: _Choice(),
        build_argv=lambda choice, prompt, env=None: ["true"],
    )
    assert verdict.is_approve is False
    assert verdict.verdict == "reject"
    assert verdict.reviewer == "agent_dup"


def test_run_adversarial_review_uses_reviewer_env_identity():
    verdict = run_adversarial_review(
        "task_x",
        env={"MAC_REVIEWER_AGENT_ID": "agent_reviewer"},
        author="agent_author",
        runner=lambda argv, cwd=None: _Proc(0, "VERDICT: APPROVE\nFINDINGS: none"),
        resolve=lambda env=None: _Choice(),
        build_argv=lambda choice, prompt, env=None: ["true"],
    )
    assert verdict.is_approve is True
    assert verdict.reviewer == "agent_reviewer"


# ---------------------------------------------------------------------------
# head_sha land gate — only land the revision the gates ran against
# ---------------------------------------------------------------------------


def _sha_decision(target, head_sha):
    return decide_land(GREEN, APPROVE, target=target, author=AUTHOR, head_sha=head_sha)


def test_safe_do_land_head_sha_match_lands():
    def git_runner(repo_dir, argv):
        if argv[0] == "rev-parse":
            return _Proc(0, "cafe1234")
        if argv[0] == "merge-tree":
            return _Proc(0, "treeoid")
        if argv[0] == "push":
            return _Proc(0, "pushed")
        return _Proc(0, "")

    result = safe_do_land(
        "feature/x",
        _sha_decision("feature/x", "cafe1234"),
        repo_dir="/repo",
        base_ref="main",
        allow_push=True,
        git_runner=git_runner,
    )
    assert result["action"] == "pushed"
    assert result["gated_head_sha"] == "cafe1234"
    assert result["current_head_sha"] == "cafe1234"


def test_safe_do_land_head_sha_moved_aborts():
    def git_runner(repo_dir, argv):
        if argv[0] == "rev-parse":
            return _Proc(0, "moved999")
        return _Proc(0, "")

    with pytest.raises(AutoLandError) as exc:
        safe_do_land(
            "feature/x",
            _sha_decision("feature/x", "cafe1234"),
            repo_dir="/repo",
            base_ref="main",
            allow_push=True,
            git_runner=git_runner,
        )
    assert "moved" in str(exc.value).lower()


def test_safe_do_land_head_sha_unresolvable_aborts():
    def git_runner(repo_dir, argv):
        if argv[0] == "rev-parse":
            return _Proc(1, "")
        return _Proc(0, "")

    with pytest.raises(AutoLandError):
        safe_do_land(
            "feature/x",
            _sha_decision("feature/x", "cafe1234"),
            repo_dir="/repo",
            base_ref="main",
            allow_push=True,
            git_runner=git_runner,
        )


# ---------------------------------------------------------------------------
# mac_notify_human — post-hoc, non-blocking visibility on every outcome
# ---------------------------------------------------------------------------


def test_notify_human_on_land_records_evidence():
    plane = _FakePlane()
    notify_human("task_x", _land_decision(), plane=plane)
    assert plane.evidence and plane.evidence[0][1] == "mac_notify_human"
    meta = plane.evidence[0][3]
    assert meta["landed"] is True
    assert meta["blocking"] is False
    assert meta["schema"] == "mac.mac_notify_human.v1"


def test_notify_human_on_block_records_evidence():
    plane = _FakePlane()
    blocked = decide_land(RED, APPROVE, target="task_x", author=AUTHOR)
    notification = notify_human("task_x", blocked, plane=plane)
    assert notification["landed"] is False
    assert notification["blocking"] is False
    assert "BLOCKED" in notification["summary"]


def test_notify_human_uses_sink_when_provided():
    seen = []
    notify_human("task_x", _land_decision(), sink=seen.append)
    assert seen and seen[0]["kind"] == "auto_land"


def test_notify_human_is_non_blocking_on_sink_error():
    def boom(_):
        raise RuntimeError("downstream down")

    out = notify_human("task_x", _land_decision(), sink=boom)
    assert "notify_error" in out


def test_run_auto_land_notifies_human_on_every_outcome():
    spy = _Spy(GREEN, APPROVE)
    notifications = []
    run_auto_land(
        "task_x",
        run_contract=spy.run_contract,
        run_review=spy.run_review,
        do_land=spy.do_land,
        record=spy.record,
        author=AUTHOR,
        notify_human=lambda t, d: notifications.append((t, d)),
    )
    assert len(notifications) == 1
    assert notifications[0][1].land is True


def test_run_auto_land_notifies_even_on_no_land():
    spy = _Spy(RED, APPROVE)
    notifications = []
    run_auto_land(
        "task_x",
        run_contract=spy.run_contract,
        run_review=spy.run_review,
        do_land=spy.do_land,
        record=spy.record,
        author=AUTHOR,
        notify_human=lambda t, d: notifications.append((t, d)),
    )
    assert len(notifications) == 1
    assert notifications[0][1].land is False


def test_run_auto_land_threads_head_sha_into_decision():
    spy = _Spy(GREEN, APPROVE)
    decision = run_auto_land(
        "feature/x",
        run_contract=spy.run_contract,
        run_review=spy.run_review,
        do_land=spy.do_land,
        record=spy.record,
        author=AUTHOR,
        resolve_head_sha=lambda target: "abc123",
    )
    assert decision.head_sha == "abc123"
    assert spy.landed and spy.landed[0][1].head_sha == "abc123"


# ---------------------------------------------------------------------------
# _resolve_head_sha + notify_human plane best-effort
# ---------------------------------------------------------------------------


def test_resolve_head_sha_branch_and_task_and_failure():
    from mac.auto_land import _resolve_head_sha

    def ok_runner(repo_dir, argv):
        assert argv[0] == "rev-parse"
        return _Proc(0, " sha_value \n")

    assert _resolve_head_sha("feature/x", git_runner=ok_runner) == "sha_value"
    # task target resolves HEAD, not a branch ref
    seen = {}

    def head_runner(repo_dir, argv):
        seen["ref"] = argv[-1]
        return _Proc(0, "headsha")

    assert _resolve_head_sha("task_x", git_runner=head_runner) == "headsha"
    assert seen["ref"].startswith("HEAD")

    def bad_runner(repo_dir, argv):
        return _Proc(1, "")

    assert _resolve_head_sha("feature/x", git_runner=bad_runner) == ""

    def raiser(repo_dir, argv):
        raise RuntimeError("git missing")

    assert _resolve_head_sha("feature/x", git_runner=raiser) == ""


def test_notify_human_best_effort_when_plane_raises():
    class _Boom:
        def add_evidence(self, *a, **k):
            raise RuntimeError("hub down")

    out = notify_human("task_x", _land_decision(), plane=_Boom())
    assert "notify_error" in out
    assert out["blocking"] is False
