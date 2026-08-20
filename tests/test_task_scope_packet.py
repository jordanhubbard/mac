"""A task is not dispatchable until its scope is bounded.

The failure this prevents, in the order it happened on 2026-08-19/20:

  1. A task is filed with a thorough description and no stated boundary.
  2. The sizing heuristic reads the PROSE, scores it "large", and the executor
     enters a planning phase.
  3. The agent cannot create children, emits ``plan_decomposed`` with zero of
     them, and dies non-retryable at attempt 1 of 3.

24 of 24 failures in that window were the contract gate rejecting work that was
never bounded enough to do. The regression that matters most here is the last
one in this file: an ATOMIC task carrying a bounded scope packet must not be
decomposed. That is asserted directly rather than inferred from a success rate,
because a success rate cannot tell you which of ten changes moved it.
"""
from __future__ import annotations

import json

import pytest

from mac.allocator import (
    TASK_ATTEMPTS_EXHAUSTED,
    TASK_SCOPE_UNBOUNDED,
    AllocationTask,
    evaluate_task,
)
from mac.cli import _render_why_unclaimed
from mac.dispatch_preflight import explain as preflight_explain, preflight
from mac.executor_scope import compute_scope_estimate_from_lessons, is_planning_phase
from mac.services import ControlPlane
from mac.task_scope_packet import (
    BOUNDED,
    REQUIRED_FIELDS,
    SCOPE_BOUNDED,
    SCOPE_PACKET_INCOMPLETE,
    SCOPE_PACKET_MALFORMED,
    SCOPE_PACKET_MISSING,
    SCOPE_REASON_CODES,
    UNBOUNDED,
    evaluate,
    evaluate_metadata,
    evaluate_task as evaluate_task_scope,
    filing_advisory,
    gate_enforced,
)


def _packet(**overrides):
    """A packet that bounds the very task this file was written for."""
    packet = {
        "outcome": (
            "mac task preflight refuses an unbounded task and names the "
            "missing scope fields"
        ),
        "current_state": (
            "preflight answers only the capability question, so an unbounded "
            "task is filed and dispatched with nothing said"
        ),
        "surface": ["src/mac/task_scope_packet.py", "src/mac/dispatch_preflight.py"],
        "validation": "pytest -q tests/test_task_scope_packet.py",
    }
    packet.update(overrides)
    return {key: value for key, value in packet.items() if value is not None}


# --- the vocabulary --------------------------------------------------------


def test_a_complete_packet_is_bounded():
    decision = evaluate(_packet())
    assert decision.bounded
    assert decision.outcome == BOUNDED
    assert decision.code == SCOPE_BOUNDED
    assert set(decision.present_fields) == set(REQUIRED_FIELDS)
    assert decision.missing_fields == ()


def test_no_packet_at_all_is_the_common_case_and_names_itself():
    """The 2026-08-19/20 shape: nothing said, so everything must be searched."""
    decision = evaluate(None)
    assert not decision.bounded
    assert decision.code == SCOPE_PACKET_MISSING
    assert set(decision.missing_fields) == set(REQUIRED_FIELDS)
    # The message has to be usable verbatim by whoever has to fix it.
    for name in REQUIRED_FIELDS:
        assert name in decision.message


def test_a_partial_packet_says_which_fields_are_missing():
    decision = evaluate(_packet(validation=None, surface=None))
    assert decision.code == SCOPE_PACKET_INCOMPLETE
    assert set(decision.missing_fields) == {"surface", "validation"}
    assert set(decision.present_fields) == {"outcome", "current_state"}


def test_a_packet_that_is_not_an_object_is_malformed_not_missing():
    """Distinct codes because they are distinct mistakes.

    "you wrote nothing" and "you wrote a string where an object goes" send
    the filer to different places, and collapsing them is exactly the boolean
    reduction ADR 0022 forbids.
    """
    for value in ("bounded", ["outcome"], 7):
        decision = evaluate(value)
        assert decision.code == SCOPE_PACKET_MALFORMED, value
        assert decision.outcome == UNBOUNDED


def test_placeholders_do_not_bound_anything():
    """A field filled in to get past the gate has not answered it."""
    for filler in ("tbd", "TBD", "  n/a ", "?", "none", "unknown"):
        decision = evaluate(_packet(validation=filler))
        assert decision.code == SCOPE_PACKET_INCOMPLETE, filler
        assert decision.missing_fields == ("validation",)


def test_a_placeholder_word_inside_a_real_sentence_is_fine():
    """The check is on the whole value, not a substring search.

    "the current behaviour is unknown to callers" is a statement about the
    defect; refusing it would teach filers that the gate is a word filter.
    """
    decision = evaluate(
        _packet(current_state="the failure reason is unknown to the operator")
    )
    assert decision.bounded


def test_a_single_path_surface_is_accepted_as_a_one_entry_list():
    """What a submitter actually writes for a one-file change."""
    decision = evaluate(_packet(surface="src/mac/cli.py"))
    assert decision.bounded
    assert decision.packet["surface"] == ["src/mac/cli.py"]


def test_short_real_paths_survive_the_placeholder_floor():
    """MIN_FIELD_CHARS applies to prose, not to paths.

    ``cli.py`` is six characters and is a legitimate whole surface; a floor
    that rejected it would be a gate that refuses correct input.
    """
    assert evaluate(_packet(surface=["cli.py", "Makefile"])).bounded


def test_every_reason_code_is_reachable_and_declared():
    """A rejection path cannot be added anonymously (ADR 0022)."""
    produced = {
        evaluate(_packet()).code,
        evaluate(None).code,
        evaluate("nope").code,
        evaluate({"outcome": "only this one"}).code,
    }
    assert produced == set(SCOPE_REASON_CODES)


def test_evaluate_metadata_and_evaluate_task_agree_with_evaluate():
    packet = _packet()
    metadata = {"scope_packet": packet}
    assert evaluate_metadata(metadata).code == evaluate(packet).code
    assert evaluate_task_scope({"metadata": metadata}).code == evaluate(packet).code
    # Metadata that is not a mapping is "missing", not a crash: this runs on
    # every task snapshot, including rows written before the packet existed.
    assert evaluate_metadata(None).code == SCOPE_PACKET_MISSING
    assert evaluate_task_scope({}).code == SCOPE_PACKET_MISSING


def test_the_gate_is_off_until_a_project_asks_for_it():
    """The entire existing ledger predates the packet.

    A default-on gate would make every open task undispatchable in one commit,
    which is a fleet outage dressed up as a policy.
    """
    assert gate_enforced(None) is False
    assert gate_enforced({}) is False
    assert gate_enforced({"require_scope_packet": True}) is True
    assert gate_enforced({"require_scope_packet": "on"}) is True
    assert gate_enforced({"require_scope_packet": False}) is False


# --- the allocator gate ----------------------------------------------------


def _allocation_task(**overrides) -> AllocationTask:
    fields = {
        "id": "task_scope",
        "priority": 0,
        "created_at": "2026-08-20T00:00:00+00:00",
    }
    fields.update(overrides)
    return AllocationTask(**fields)


def test_an_unbounded_task_is_dispatchable_where_the_gate_is_not_enforced():
    """Filing one stays legal, and so does running one, until a project opts in."""
    evaluation = evaluate_task(_allocation_task(scope_bounded=False))
    assert evaluation.allowed
    assert TASK_SCOPE_UNBOUNDED not in evaluation.rejections


def test_an_unbounded_task_is_not_dispatched_where_the_gate_is_enforced():
    evaluation = evaluate_task(
        _allocation_task(scope_bounded=False, scope_gate_enforced=True)
    )
    assert not evaluation.allowed
    assert TASK_SCOPE_UNBOUNDED in evaluation.rejections


def test_the_enforced_gate_lets_a_bounded_task_through():
    evaluation = evaluate_task(
        _allocation_task(scope_bounded=True, scope_gate_enforced=True)
    )
    assert evaluation.allowed


def test_the_scope_gate_names_itself_beside_the_other_task_gates():
    """ADR 0022: the reason IS the return value, not a reconstruction.

    Asserted next to an unrelated task-level rejection so it is clear the two
    stay distinguishable rather than collapsing into "task not ready".
    """
    evaluation = evaluate_task(
        _allocation_task(
            scope_bounded=False,
            scope_gate_enforced=True,
            attempt_count=3,
            max_attempts=3,
        )
    )
    assert set(evaluation.rejections) == {
        TASK_SCOPE_UNBOUNDED,
        TASK_ATTEMPTS_EXHAUSTED,
    }


def test_scope_defaults_fail_open():
    """A snapshot builder that says nothing must not strand the task.

    Unlike requires_execution, the safe direction here is open: refusing to
    route a task whose packet simply was not populated stops work that was
    fine, while the reverse merely wastes one attempt.
    """
    assert evaluate_task(_allocation_task()).allowed


# --- preflight -------------------------------------------------------------


class _Agent:
    def __init__(self, name, capabilities):
        self.id = name
        self.name = name
        self.status = "online"
        self.capabilities = list(capabilities)
        self.resources = {"hardware": {"os": "linux", "cpu_arch": "x86_64"}}
        self.visibility = "shared"
        self.owner_human_id = None


def test_preflight_reports_unbounded_scope_separately_from_capabilities():
    """Two findings, two remedies.

    The fleet answer and the task answer both end in "nothing happens", and a
    caller told only "not dispatchable" cannot tell whether to go and change
    the fleet or the task.
    """
    result = preflight([_Agent("worker", ["python"])], required_capabilities=["python"])
    assert result["dispatchable"] is True
    assert result["missing_capabilities"] == []
    assert result["scope_bounded"] is False
    assert result["scope"]["code"] == SCOPE_PACKET_MISSING
    explanation = preflight_explain(result)
    assert "dispatchable" in explanation
    assert "scope not bounded" in explanation


def test_preflight_with_a_bounded_packet_reports_no_scope_finding():
    result = preflight(
        [_Agent("worker", ["python"])],
        required_capabilities=["python"],
        scope_packet=_packet(),
    )
    assert result["scope_bounded"] is True
    assert "scope not bounded" not in preflight_explain(result)


def test_an_unsatisfiable_capability_is_still_its_own_finding():
    """The pre-existing answer must not be diluted by the new one."""
    result = preflight(
        [_Agent("worker", ["python"])],
        required_capabilities=["rust"],
        scope_packet=_packet(),
    )
    assert result["dispatchable"] is False
    assert result["missing_capabilities"] == ["rust"]
    assert result["scope_bounded"] is True
    assert "rust" in preflight_explain(result)


def test_both_findings_appear_together_without_hiding_each_other():
    result = preflight([_Agent("worker", ["python"])], required_capabilities=["rust"])
    explanation = preflight_explain(result)
    assert "no agent advertises: rust" in explanation
    assert "scope not bounded" in explanation


def test_a_caller_that_never_offered_a_packet_can_opt_out_of_the_scope_clause():
    """The litai adapter states capabilities and hardware and nothing else."""
    result = preflight([_Agent("worker", ["python"])], required_capabilities=["rust"])
    assert "scope" not in preflight_explain(result, include_scope=False)


# --- the whole path, through the control plane -----------------------------


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _codes(explanation):
    return [reason["code"] for reason in explanation["task_reasons"]]


def test_filing_an_unbounded_task_succeeds(cp):
    """The ledger is an inbox and a place to think. Filing stays legal."""
    task = cp.create_task("think about the retry ladder")
    assert cp.get_task(task.id).id == task.id
    assert cp.explain_task_dispatch(task.id)["task_ready"] is True


def test_filing_an_unbounded_task_records_what_is_missing(cp):
    """Legal, but not silent. The reason lands in the task's own observations."""
    task = cp.create_task("think about the retry ladder")
    logged = [
        record
        for record in cp.list_observability(
            subject_type="task", subject_id=task.id
        )
        if record.name == "task.scope.unbounded"
    ]
    assert len(logged) == 1
    assert logged[0].detail["code"] == SCOPE_PACKET_MISSING
    assert set(logged[0].detail["missing_fields"]) == set(REQUIRED_FIELDS)


def test_filing_a_bounded_task_records_nothing(cp):
    task = cp.create_task("bounded work", metadata={"scope_packet": _packet()})
    assert not [
        record
        for record in cp.list_observability(
            subject_type="task", subject_id=task.id
        )
        if record.name == "task.scope.unbounded"
    ]


def test_an_unbounded_task_is_reported_but_not_gated_by_default(cp):
    """Every task in the ledger predates the packet.

    Defaulting the gate on would strand the entire backlog in one commit, so
    the decision is REPORTED for a project that has not opted in and enforced
    only for one that has.
    """
    task = cp.create_task("unbounded work")
    explanation = cp.explain_task_dispatch(task.id)
    assert explanation["task_ready"] is True
    assert TASK_SCOPE_UNBOUNDED not in _codes(explanation)
    assert explanation["scope"]["bounded"] is False
    assert explanation["scope"]["enforced"] is False


def test_why_unclaimed_names_the_scope_gate(cp):
    """ADR 0022: the gate must name itself.

    This is the acceptance test for the whole change -- an operator asking why
    nothing is happening has to be told "nobody bounded it", not "task not
    ready".
    """
    project = cp.create_project(
        "bounded-only", metadata={"require_scope_packet": True}
    )
    task = cp.create_task("unbounded work", project=project.name)
    explanation = cp.explain_task_dispatch(task.id)
    assert explanation["task_ready"] is False
    assert TASK_SCOPE_UNBOUNDED in _codes(explanation)
    assert explanation["scope"]["enforced"] is True
    assert task.id not in {item.id for item in cp.ready_tasks()}


def test_a_bounded_task_dispatches_in_a_project_that_requires_packets(cp):
    project = cp.create_project(
        "bounded-only", metadata={"require_scope_packet": True}
    )
    task = cp.create_task(
        "bounded work", project=project.name, metadata={"scope_packet": _packet()}
    )
    explanation = cp.explain_task_dispatch(task.id)
    assert explanation["task_ready"] is True
    assert explanation["scope"]["bounded"] is True


def test_the_renderer_shows_the_scope_reason_rather_than_dropping_it(cp):
    """The payload having the reason is not the same as the operator seeing it.

    That gap is precisely how why-unclaimed came to print a title and two
    attempt counters for a task nothing could take.
    """
    project = cp.create_project(
        "bounded-only", metadata={"require_scope_packet": True}
    )
    task = cp.create_task("unbounded work", project=project.name)
    rendered = _render_why_unclaimed(cp.explain_task_dispatch(task.id))
    assert TASK_SCOPE_UNBOUNDED in rendered
    assert "SCOPE: unbounded" in rendered
    for name in REQUIRED_FIELDS:
        assert name in rendered


def test_the_advisory_is_rendered_even_where_no_gate_is_closed(cp):
    """An unbounded task that IS dispatchable is the input to the failure.

    "No gate is closed" on its own would be true and misleading, so the scope
    verdict prints for an unenforced project too.
    """
    task = cp.create_task("unbounded work")
    rendered = _render_why_unclaimed(cp.explain_task_dispatch(task.id))
    assert "SCOPE: unbounded" in rendered
    assert "advisory" in rendered
    assert TASK_SCOPE_UNBOUNDED not in rendered


# --- filing ----------------------------------------------------------------


def test_filing_an_unbounded_task_says_what_is_missing():
    """Filing succeeds. It just stops succeeding SILENTLY."""
    advisory = filing_advisory(evaluate(None))
    assert advisory
    assert "filed" in advisory
    assert "scope_packet" in advisory
    for name in REQUIRED_FIELDS:
        assert name in advisory


def test_the_filing_advisory_says_whether_the_task_can_actually_run():
    """Two different situations; the filer needs to know which one they are in."""
    decision = evaluate(None)
    assert "NOT dispatchable" in filing_advisory(decision, enforced=True)
    assert "NOT dispatchable" not in filing_advisory(decision, enforced=False)


def test_filing_a_bounded_task_says_nothing():
    assert filing_advisory(evaluate(_packet())) == ""


def test_mac_task_create_prints_the_advisory_to_stderr(tmp_path, capsys, monkeypatch):
    """It must not go to stdout: `mac task create` output is parsed as JSON.

    An advisory that broke every scripted caller would be removed within the
    day, which is a worse outcome than not having said anything.
    """
    from mac.cli import main
    from mac.test_support import dsn_for

    # The CLI builds a ControlPlane directly, which needs a signing key. Set
    # here rather than borrowed from tests/cli/conftest.py so this file stays
    # runnable on its own.
    monkeypatch.setenv("MAC_SECRET_KEY", "scope-packet-test-key-at-least-32-chars")
    dsn = dsn_for(tmp_path)
    assert main(["--db", dsn, "admin", "init"]) == 0
    capsys.readouterr()

    assert main(["--db", dsn, "task", "create", "unbounded work"]) == 0
    captured = capsys.readouterr()
    filed = json.loads(captured.out[captured.out.index("{"):])
    assert filed["title"] == "unbounded work"
    assert "scope_packet" in captured.err
    for name in REQUIRED_FIELDS:
        assert name in captured.err


def test_mac_task_create_stays_quiet_for_a_bounded_task(tmp_path, capsys, monkeypatch):
    from mac.cli import main
    from mac.test_support import dsn_for

    # The CLI builds a ControlPlane directly, which needs a signing key. Set
    # here rather than borrowed from tests/cli/conftest.py so this file stays
    # runnable on its own.
    monkeypatch.setenv("MAC_SECRET_KEY", "scope-packet-test-key-at-least-32-chars")
    dsn = dsn_for(tmp_path)
    assert main(["--db", dsn, "admin", "init"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                "--db", dsn, "task", "create", "bounded work",
                "--metadata", json.dumps({"scope_packet": _packet()}),
            ]
        )
        == 0
    )
    assert "scope_packet" not in capsys.readouterr().err


# --- the regression this whole change exists to prevent --------------------
#
# task_0936d282: a two-line rename -- `_assert_task_actor` dropping its task_id
# parameter -- decomposed into "a rename child plus two test children", then
# dead at attempt 1 of 3. It was already atomic. Nothing said so.

_TITLE = "Drop the unused task_id parameter from _assert_task_actor"

#: Long, careful, and about ONE two-line change. The signals do not read the
#: work, they read the prose ABOUT the work: over 800 characters scores
#: desc_length, and listing the call sites as bullets past 300 words scores
#: plan_detected. Writing a thorough description is how a filer earns both.
_THOROUGH_DESCRIPTION = (
    "Remove the unused task_id parameter from _assert_task_actor. "
    "It was added when the helper was expected to look the task up itself, "
    "which it never did -- every call site already holds the task and passes "
    "the id purely to satisfy the signature. "
    "The parameter is dead weight and, worse, it reads as though the helper "
    "validates the actor against that specific task, which it does not: it "
    "checks the actor against the caller's principal and nothing else. "
    "Someone will eventually rely on the meaning the name implies. "
    "The call sites, all of which already hold the task they are asking "
    "about:\n"
    + "".join(
        "  - %s passes task.id and then never uses it again in that branch, "
        "so dropping the argument is a pure deletion there.\n" % site
        for site in (
            "claim_task",
            "release_task",
            "close_task",
            "reopen_task",
            "update_task",
            "add_child_tasks",
            "record_task_evidence",
        )
    )
    + "None of these is a behaviour change; the helper's body does not "
    "reference the parameter at all, so the only risk is a caller outside "
    "this repository passing it positionally, and there is none. "
    "The rename is safe to do in one pass because the helper is private to "
    "the module and every reference to it is in the same file, which means "
    "the change is a single search and replace followed by running the actor "
    "tests. There is no migration, no compatibility shim, and no deprecation "
    "window to think about, because nothing outside this module can see the "
    "signature at all. The reason this is worth writing down at length is "
    "that the name is actively misleading right now, and a reader who trusts "
    "it will write a check that does not exist."
)


def _atomic_task(*, with_packet: bool, record_estimate: bool = True):
    # The executor records the sizing estimate on the task at attempt 1 and
    # then reads it back, so a test that only computes the estimate is not
    # testing the path that decides anything. Recorded here the way
    # maybe_preflight_scope_estimate would have: from the description, before
    # anyone thought to add a packet.
    metadata = {}
    if record_estimate:
        metadata["scope_estimate"] = compute_scope_estimate_from_lessons(
            {"title": _TITLE, "description": _THOROUGH_DESCRIPTION, "metadata": {}},
            [],
        )
    if with_packet:
        metadata["scope_packet"] = _packet(
            outcome=(
                "_assert_task_actor takes no task_id parameter and all seven "
                "call sites are updated"
            ),
            current_state=(
                "_assert_task_actor accepts a task_id it never reads, implying "
                "a per-task check it does not perform"
            ),
            surface=["src/mac/services.py"],
            validation="pytest -q tests/test_control_plane.py -k actor",
        )
    return {
        "id": "task_0936d282",
        "title": _TITLE,
        "description": _THOROUGH_DESCRIPTION,
        "attempt_count": 1,
        "metadata": metadata,
    }


def test_without_a_packet_a_thorough_description_alone_scores_large():
    """The precondition. If this stops being true the regression below is vacuous."""
    task = _atomic_task(with_packet=False, record_estimate=False)
    assert compute_scope_estimate_from_lessons(task, [])["size"] == "large"
    assert is_planning_phase(_atomic_task(with_packet=False)) is True


def test_an_atomic_task_carrying_a_bounded_scope_is_not_decomposed():
    """THE regression.

    Same title, same description, same attempt, same recorded "large" sizing
    estimate. The only difference is that the submitter stated the boundary --
    and the executor must therefore not enter a planning phase, which is where
    the zero-child plan_decomposed death starts.

    The recorded estimate matters: metadata.scope_estimate is written once at
    attempt 1 and persists, so a task scored "large" before anyone added a
    packet would otherwise keep planning forever on the strength of a blob
    nothing recomputes. The packet is read fresh and wins.
    """
    task = _atomic_task(with_packet=True)
    assert task["metadata"]["scope_estimate"]["size"] == "large"
    assert is_planning_phase(task) is False


def test_a_bounded_packet_makes_a_fresh_estimate_small_and_says_why():
    task = _atomic_task(with_packet=True, record_estimate=False)
    estimate = compute_scope_estimate_from_lessons(task, [])
    assert estimate["size"] == "small"
    assert "scope_packet:bounded" in estimate["signals"]
    assert "scope_packet" in estimate["rationale"]


def test_an_explicit_plan_first_still_wins_over_a_packet():
    """Both are the submitter speaking; a task carrying both is contradictory.

    Honouring the one that asks for LESS work per run is the safe reading, and
    it is the same precedence no_decompose already has over a decomposition
    budget.
    """
    task = _atomic_task(with_packet=True)
    task["metadata"]["plan_first"] = True
    assert is_planning_phase(task) is True


def test_an_incomplete_packet_does_not_buy_atomicity():
    """Half a boundary is not a boundary.

    Otherwise ``scope_packet: {"outcome": "fix it"}`` becomes the cheapest way
    to switch the sizing heuristic off.
    """
    task = _atomic_task(with_packet=True)
    del task["metadata"]["scope_packet"]["validation"]
    assert evaluate_metadata(task["metadata"]).code == SCOPE_PACKET_INCOMPLETE
    assert is_planning_phase(task) is True
