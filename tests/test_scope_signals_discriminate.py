"""A scope signal must be able to be false.

Completion yield by dependency count, measured across all 7,678 ledger tasks on
2026-08-02:

    0 deps   2,945 tasks   29.3% completed
    1 dep    2,888 tasks    5.5% completed
    2 deps   1,012 tasks    2.0% completed
    3+ deps    833 tasks    2.0% completed

One dependency costs 5.3x completion; two is effectively terminal. And the
system attached them by default -- 62% of tasks carried dependencies, 82% of
the waiting pool were decomposition children. Twelve self-contained,
dependency-free tasks were filed that day against verified main; the planner
decomposed every one, one child failed and stranded its parent permanently, and
none of the twelve completed (task_0ee0b7ce).

THE MECHANISM. ``compute_scope_estimate_from_lessons`` calls a task large on
``>= 2`` signals, which reads like "two independent indicators". Measured on ten
open ledger tasks 2026-08-07, it was not:

  * ``repo_required_cmds`` fired on 10 of 10. It reads required_commands from
    the PROJECT's repository contract, so it is constant within a project:
    ['python3','git','gh'] for every mac task, ['python3','git','gh','make','cc']
    for every nanolang task -- one distinct set each, both over the threshold.
    It described the repository and voted "large" on every task in it.

  * ``desc_words`` (>=200) and ``desc_chars`` (>=800) both fired on 9 of 10.
    200 words of English prose is ~1,100-1,400 characters, so the character
    test is implied by the word test and never fired independently. One
    property, two votes.

So the gate was really "project constant + description length >= 2", and every
task with a substantial description was large. Writing a thorough description
was the trigger for being decomposed.

After the fix, the same ten tasks: 4 large instead of 10, and the four all carry
a second signal that genuinely varies.

These tests assert the property that was missing rather than the arithmetic:
a signal that cannot be false is not evidence.
"""

from __future__ import annotations

import pytest

from mac.executor_scope import (
    _SCOPE_LARGE_DESC_CHARS,
    _SCOPE_LARGE_DESC_WORDS,
    _compute_scope_signals,
    compute_scope_estimate_from_lessons,
)

#: A description that is thorough but describes ONE piece of work. This is the
#: shape of the twelve tasks that were decomposed: measured, self-contained,
#: dependency-free, and long because it is well specified.
THOROUGH_BUT_ATOMIC = " ".join(
    [
        "The window lookup matches on created_by, which holds the actor rather",
        "than the subject, so a summary written by the nap cycle is invisible",
        "to it and the window never advances.",
    ]
    * 30
)

#: The repository contract every task in a project inherits.
PROJECT_CONTRACT = {
    "execution_contract": {
        "repository_contract": {
            "toolchain": {"required_commands": ["python3", "git", "gh"]}
        }
    }
}


def _hard_signals(title, description, metadata=None):
    """Signals that count toward "large" (plan_signal: entries do not)."""
    return [
        s
        for s in _compute_scope_signals(title, description, metadata or {}, [])
        if not s.startswith("plan_signal:")
    ]


def _size(title, description, metadata=None):
    task = {"title": title, "description": description, "metadata": metadata or {}}
    return compute_scope_estimate_from_lessons(task, [])["size"]


# --------------------------------------------------------------------------
# The project constant
# --------------------------------------------------------------------------


def test_the_repository_contract_is_not_a_task_scope_signal():
    """It is identical for every task in a project, so it discriminates nothing."""
    signals = _hard_signals("short title", "short body", PROJECT_CONTRACT)

    assert not any("repo_required_cmds" in s for s in signals), (
        "a project-constant property is voting on per-task scope; measured "
        "2026-08-07 it fired on 10 of 10 open tasks"
    )


def test_two_tasks_in_one_project_can_differ_in_size():
    """The property the constant destroyed.

    With a project constant contributing one guaranteed vote, a single further
    signal was enough, so every substantial task in the project tipped large
    together.
    """
    small = _size("small task", "a short description", PROJECT_CONTRACT)
    large = _size(
        "a plan with numbered phases",
        "1. first step\n2. second step\n3. third step\n" + THOROUGH_BUT_ATOMIC,
        PROJECT_CONTRACT,
    )

    assert small == "small"
    assert large == "large", "nothing in this project can be large any more"


def test_a_richer_toolchain_does_not_make_a_task_larger():
    """nanolang's contract lists five commands, mac's three. Same task, same size."""
    mac_like = _size("fix a typo", "one line", PROJECT_CONTRACT)
    nanolang_like = _size(
        "fix a typo",
        "one line",
        {
            "execution_contract": {
                "repository_contract": {
                    "toolchain": {
                        "required_commands": ["python3", "git", "gh", "make", "cc"]
                    }
                }
            }
        },
    )

    assert mac_like == nanolang_like == "small"


# --------------------------------------------------------------------------
# The double-counted description
# --------------------------------------------------------------------------


def test_description_length_votes_once():
    """The regression: 9 of 10 tasks scored both desc_words and desc_chars."""
    signals = _hard_signals("t", THOROUGH_BUT_ATOMIC)
    length_signals = [s for s in signals if s.startswith("desc_")]

    assert len(length_signals) == 1, (
        "description length contributed %d votes: %s" % (len(length_signals), length_signals)
    )


def test_the_word_threshold_implies_the_character_threshold():
    """Why they were never independent, asserted rather than assumed.

    Anything reaching 200 words of prose has long since passed 800 characters,
    so the second test could only ever agree with the first.
    """
    prose = " ".join(["consolidation"] * _SCOPE_LARGE_DESC_WORDS)

    assert len(prose) > _SCOPE_LARGE_DESC_CHARS


def test_a_long_but_wordless_description_still_registers():
    """Both bounds are kept: either can be the one exceeded.

    A description can be long in characters and short in words -- a table, a
    stack trace, a list of paths -- and that is still a long description.
    """
    dense = "/very/long/path/to/some/module/file.py\n" * 40

    assert len(dense.split()) < _SCOPE_LARGE_DESC_WORDS
    assert len(dense) >= _SCOPE_LARGE_DESC_CHARS
    assert any(s.startswith("desc_length") for s in _hard_signals("t", dense))


def test_a_short_description_registers_nothing():
    assert _hard_signals("t", "fix the typo on line 12") == []


# --------------------------------------------------------------------------
# The behaviour that was actually costing completions
# --------------------------------------------------------------------------


def test_a_thorough_but_atomic_task_is_not_large():
    """The twelve tasks. Being well specified must not be the trigger.

    Before the fix this scored desc_words + desc_chars + repo_required_cmds =
    3 and was decomposed. It is one piece of work described carefully.
    """
    assert _size("Unfreeze the nap window", THOROUGH_BUT_ATOMIC, PROJECT_CONTRACT) == "small"


def test_a_genuinely_multi_part_task_is_still_large():
    """The other half. Decomposition is kept where it is warranted.

    A fix that made nothing large would satisfy the yield table and lose the
    capability, which is not what was asked for.
    """
    size = _size(
        "Migrate task ids from TEXT to BIGINT across the schema and application",
        "1. Convert the schema.\n2. Update the 4 generation sites.\n"
        "3. Migrate ids embedded in JSON metadata.\n4. Fix ~180 shape-coupled "
        "call sites.\n" + THOROUGH_BUT_ATOMIC,
        PROJECT_CONTRACT,
    )

    assert size == "large"


def test_an_empty_task_is_small():
    assert _size("", "", {}) == "small"


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"execution_contract": None},
        {"execution_contract": {"repository_contract": "not-an-object"}},
        {"execution_contract": {"repository_contract": {"toolchain": []}}},
        {"origin": {"repository_contract": {"toolchain": {"required_commands": "git"}}}},
    ],
)
def test_malformed_contract_metadata_does_not_raise(metadata):
    """Sizing runs at control-plane admission; it must not reject a task."""
    assert _size("t", "short", metadata) == "small"


def test_the_estimate_reports_its_own_signals():
    """An operator has to be able to see WHY a task was called large.

    This is the whole reason the defect was findable: the signal list named
    repo_required_cmds on every task, which is what exposed it as a constant.
    """
    estimate = compute_scope_estimate_from_lessons(
        {"title": "t", "description": THOROUGH_BUT_ATOMIC, "metadata": PROJECT_CONTRACT},
        [],
    )

    assert estimate["schema"] == "mac.scope_estimate.v1"
    assert estimate["signals"]
    assert "desc_length" in estimate["rationale"]
