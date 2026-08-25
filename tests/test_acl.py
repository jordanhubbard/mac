"""ACL evaluator (ADR 0019), tested from the refusal side.

A suite that proves permitted operations succeed cannot detect fail-open, and
fail-open is the entire risk this model carries. So most of these assert that
something is REFUSED.
"""

from __future__ import annotations

import pytest

from mac.acl import (
    AccessControlEntry as ACE,
    AclError,
    AclEvaluator,
    PERMISSIONS,
    Permission,
    ancestors,
    normalize_path,
    project_path,
    secret_path,
    task_path,
)

TASK_A = task_path("mac", "task_aaa")
TASK_B = task_path("mac", "task_bbb")
PROJECT = project_path("mac")
FLEET = "/fleet"

SANDBOX = "principal_sandbox_task_aaa"


def sandbox_evaluator():
    """The ADR's motivating credential, verbatim."""
    return AclEvaluator(
        [
            ACE(SANDBOX, TASK_A, Permission.READ),
            ACE(SANDBOX, TASK_A, Permission.APPEND),
            ACE(SANDBOX, TASK_A, Permission.CREATE),
        ]
    )


# --- deny by default -------------------------------------------------------


def test_a_resource_with_no_entry_is_refused():
    acl = AclEvaluator([])
    grant = acl.check("nobody", TASK_A, Permission.READ)
    assert grant.allowed is False
    assert "deny by default" in grant.reason


def test_an_unknown_principal_is_refused_even_where_others_are_allowed():
    acl = sandbox_evaluator()
    assert acl.allows("someone_else", TASK_A, Permission.READ) is False


# --- the motivating case: what the sandbox may and may not do --------------


def test_the_sandbox_may_work_its_own_task():
    acl = sandbox_evaluator()
    for perm in (Permission.READ, Permission.APPEND, Permission.CREATE):
        assert acl.allows(SANDBOX, TASK_A, perm) is True


def test_the_sandbox_cannot_touch_another_task():
    """The probe that matters: a valid narrow credential exceeding itself."""
    acl = sandbox_evaluator()
    for perm in sorted(PERMISSIONS):
        assert acl.allows(SANDBOX, TASK_B, perm) is False


def test_the_sandbox_cannot_control_even_its_own_task():
    """No heartbeat, no claim, no transition -- the rationale for withholding
    hub credentials in the first place."""
    acl = sandbox_evaluator()
    assert acl.allows(SANDBOX, TASK_A, Permission.CONTROL) is False


def test_the_sandbox_cannot_read_a_secret_or_reach_the_fleet_root():
    acl = sandbox_evaluator()
    assert acl.allows(SANDBOX, secret_path("ANTHROPIC_API_KEY"), Permission.READ) is False
    assert acl.allows(SANDBOX, FLEET, Permission.READ) is False


def test_the_sandbox_cannot_widen_its_own_grants():
    acl = sandbox_evaluator()
    assert acl.allows(SANDBOX, TASK_A, Permission.GRANT) is False


# --- no permission implies another ----------------------------------------


def test_no_permission_implies_any_other():
    """The defect that made the old `write` scope mean three domains."""
    for held in sorted(PERMISSIONS):
        acl = AclEvaluator([ACE("p", TASK_A, held)])
        for other in sorted(PERMISSIONS):
            expected = held == other
            assert acl.allows("p", TASK_A, other) is expected, "holding %s must not confer %s" % (
                held,
                other,
            )


# --- inheritance -----------------------------------------------------------


def test_a_project_grant_reaches_tasks_created_later():
    """The property that makes this usable at 6,849-task scale."""
    acl = AclEvaluator([ACE("worker", PROJECT, Permission.READ)])
    assert acl.allows("worker", task_path("mac", "task_created_tomorrow"), Permission.READ)


def test_inheritance_is_reported_with_its_source():
    acl = AclEvaluator([ACE("worker", PROJECT, Permission.READ)])
    grant = acl.check("worker", TASK_A, Permission.READ)
    assert grant.allowed is True
    assert grant.inherited is True
    assert grant.inherited_from == PROJECT


def test_a_grant_does_not_leak_upward():
    """Authority on a task says nothing about the project containing it."""
    acl = AclEvaluator([ACE("p", TASK_A, Permission.WRITE)])
    assert acl.allows("p", PROJECT, Permission.WRITE) is False
    assert acl.allows("p", FLEET, Permission.WRITE) is False


# --- longest path wins, deterministically ---------------------------------


def test_explicit_deny_carves_one_task_out_of_a_project_grant():
    acl = AclEvaluator(
        [
            ACE("worker", PROJECT, Permission.READ),
            ACE("worker", TASK_B, Permission.READ, allow=False),
        ]
    )
    assert acl.allows("worker", TASK_A, Permission.READ) is True
    assert acl.allows("worker", TASK_B, Permission.READ) is False


def test_a_deeper_allow_overrides_a_shallower_deny():
    acl = AclEvaluator(
        [
            ACE("worker", PROJECT, Permission.READ, allow=False),
            ACE("worker", TASK_A, Permission.READ),
        ]
    )
    assert acl.allows("worker", TASK_A, Permission.READ) is True
    assert acl.allows("worker", TASK_B, Permission.READ) is False


def test_deny_beats_allow_at_the_same_path():
    acl = AclEvaluator(
        [
            ACE("worker", TASK_A, Permission.READ),
            ACE("worker", TASK_A, Permission.READ, allow=False),
        ]
    )
    assert acl.allows("worker", TASK_A, Permission.READ) is False


@pytest.mark.parametrize("rotation", range(4))
def test_the_decision_does_not_depend_on_entry_order(rotation):
    """The specific failure of the model this replaces.

    `_required_scope` was an ordered if/elif chain where a broad early prefix
    shadowed a narrow later one by accident of position. Shuffle the entries;
    the answer must not move.
    """
    entries = [
        ACE("worker", FLEET, Permission.READ),
        ACE("worker", PROJECT, Permission.READ, allow=False),
        ACE("worker", TASK_A, Permission.READ),
        ACE("worker", TASK_B, Permission.READ, allow=False),
    ]
    rotated = entries[rotation:] + entries[:rotation]
    acl = AclEvaluator(rotated)
    assert acl.allows("worker", TASK_A, Permission.READ) is True
    assert acl.allows("worker", TASK_B, Permission.READ) is False
    assert acl.allows("worker", PROJECT, Permission.READ) is False
    assert acl.allows("worker", FLEET, Permission.READ) is True


# --- roles are groups ------------------------------------------------------


def test_a_principal_inherits_through_a_role():
    acl = AclEvaluator(
        [ACE("role_reviewers", PROJECT, Permission.READ)],
        roles={"alice": ["role_reviewers"]},
    )
    grant = acl.check("alice", TASK_A, Permission.READ)
    assert grant.allowed is True
    assert grant.via_role == "role_reviewers"


def test_role_membership_is_transitive_and_terminates_on_a_cycle():
    acl = AclEvaluator(
        [ACE("role_root", FLEET, Permission.GRANT)],
        roles={"a": ["b"], "b": ["role_root", "a"]},  # deliberate cycle
    )
    assert acl.allows("a", FLEET, Permission.GRANT) is True


def test_admin_is_not_magic_it_is_grant_at_the_fleet_root():
    """`admin` no longer short-circuits every check."""
    acl = AclEvaluator([ACE("operator", FLEET, Permission.GRANT)])
    assert acl.allows("operator", TASK_A, Permission.GRANT) is True
    # ...and it confers nothing else, because no permission implies another.
    assert acl.allows("operator", TASK_A, Permission.WRITE) is False


# --- paths -----------------------------------------------------------------


def test_paths_are_canonical_regardless_of_spelling():
    assert normalize_path("/fleet/project/mac/") == "/fleet/project/mac"
    assert normalize_path("fleet//project///mac") == "/fleet/project/mac"


def test_ancestors_are_longest_first():
    assert ancestors(TASK_A)[0] == TASK_A
    assert ancestors(TASK_A)[-1] == FLEET


def test_a_sibling_prefix_is_not_an_ancestor():
    """String-prefix matching would make /fleet/project/mac-dev inherit from
    /fleet/project/mac. Segment matching must not."""
    acl = AclEvaluator([ACE("p", project_path("mac"), Permission.READ)])
    assert acl.allows("p", task_path("mac-dev", "task_x"), Permission.READ) is False


def test_identifiers_containing_a_slash_are_rejected():
    with pytest.raises(AclError):
        task_path("mac", "task_a/../../fleet")


def test_an_unknown_permission_is_an_error_not_a_denial():
    """Silently returning False would let a typo read as a policy decision."""
    acl = sandbox_evaluator()
    with pytest.raises(AclError):
        acl.check(SANDBOX, TASK_A, "delete_everything")
    with pytest.raises(AclError):
        ACE("p", TASK_A, "sudo")


# --- tooling ---------------------------------------------------------------


def test_effective_permissions_answers_what_can_this_principal_do():
    acl = sandbox_evaluator()
    eff = acl.effective_permissions(SANDBOX, TASK_A)
    assert {p for p, g in eff.items() if g.allowed} == {"read", "append", "create"}


def test_who_can_reach_expands_roles():
    acl = AclEvaluator(
        [ACE("role_reviewers", PROJECT, Permission.READ)],
        roles={"alice": ["role_reviewers"], "bob": ["role_reviewers"]},
    )
    assert acl.who_can_reach(TASK_A, Permission.READ) == [
        "alice",
        "bob",
        "role_reviewers",
    ]


# --- update / stop / start (ADR 0020) -------------------------------------


def test_update_does_not_confer_delete():
    """`update` is split out of `write` so a principal can correct a task's
    scope without being able to destroy it."""
    acl = AclEvaluator([ACE("editor", TASK_A, Permission.UPDATE)])
    assert acl.allows("editor", TASK_A, Permission.UPDATE) is True
    assert acl.allows("editor", TASK_A, Permission.WRITE) is False


def test_update_does_not_confer_stop_or_start():
    """The atomic edit cycle is authorised by `update` alone; the STANDALONE
    stop and start verbs are separate grants."""
    acl = AclEvaluator([ACE("editor", TASK_A, Permission.UPDATE)])
    assert acl.allows("editor", TASK_A, Permission.STOP) is False
    assert acl.allows("editor", TASK_A, Permission.START) is False


def test_stop_does_not_confer_start():
    """Deliberate: a principal that may halt runaway work is not thereby
    trusted to release it back into the fleet."""
    acl = AclEvaluator([ACE("halter", TASK_A, Permission.STOP)])
    assert acl.allows("halter", TASK_A, Permission.STOP) is True
    assert acl.allows("halter", TASK_A, Permission.START) is False


def test_stop_and_start_are_not_control():
    """`control` is what an EXECUTOR needs -- claim, heartbeat, lease. Folding
    the operator's edit cycle into it would mean letting someone halt a bad
    task also let them claim work and impersonate a worker's lifecycle."""
    acl = AclEvaluator(
        [ACE("operator", TASK_A, Permission.STOP), ACE("operator", TASK_A, Permission.START)]
    )
    assert acl.allows("operator", TASK_A, Permission.CONTROL) is False


def test_the_sandbox_credential_gains_none_of_them():
    """ADR 0020: an agent must not be able to stop its own task to escape a
    gate, nor rewrite the criteria it is being judged against."""
    acl = sandbox_evaluator()
    for perm in (
        Permission.UPDATE,
        Permission.STOP,
        Permission.START,
        Permission.CONTROL,
        Permission.WRITE,
        Permission.GRANT,
    ):
        assert acl.allows(SANDBOX, TASK_A, perm) is False


def test_an_operator_edit_grant_is_expressible_without_lifecycle_authority():
    """The grant ADR 0020 actually wants for a human correcting scope."""
    acl = AclEvaluator(
        [
            ACE("human_jkh", PROJECT, Permission.READ),
            ACE("human_jkh", PROJECT, Permission.UPDATE),
            ACE("human_jkh", PROJECT, Permission.STOP),
            ACE("human_jkh", PROJECT, Permission.START),
        ]
    )
    for perm in (Permission.READ, Permission.UPDATE, Permission.STOP, Permission.START):
        assert acl.allows("human_jkh", TASK_A, perm) is True
    # ...and still cannot delete a task or claim work as an agent.
    assert acl.allows("human_jkh", TASK_A, Permission.WRITE) is False
    assert acl.allows("human_jkh", TASK_A, Permission.CONTROL) is False
