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
            assert acl.allows("p", TASK_A, other) is expected, (
                "holding %s must not confer %s" % (held, other)
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
