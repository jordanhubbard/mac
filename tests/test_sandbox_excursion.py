"""An excursion outside a repository contract must become auditable work.

The executor already records, per task, which contract-required commands the
sandbox image did not supply::

    {"schema": "mac.sandbox_environment_delta.v1",
     "commands": [...required...],
     "missing_after": [...still absent after provisioning...],
     "reason": "repository_contract.toolchain.required_commands"}

It rides back inside ``mac.sandbox_verification.v1``, lands on a check item,
and stops. The sandbox is disposable, so every excursion was measured and then
thrown away.

The visible consequence is deploy/openshell/mac-hermes.Containerfile, whose
package list is a hand-written ledger of exactly these incidents:

    "libssl-dev: nanolang's src/sign.c #includes <openssl/evp.h> ... without it
     a coding agent will destructively stub sign.c just to compile"

That is one excursion, noticed months later, by a person. These tests cover the
consumer that makes noticing automatic -- and, as importantly, the two ways it
could do more harm than good: by blocking work it has no business blocking, and
by filing enough duplicates that nobody reads any of them.
"""

from __future__ import annotations

import pytest

from mac.sandbox_excursion import (
    DELTA_SCHEMA,
    EXCURSION_METADATA_KEY,
    excursion_from_delta,
)
from mac.services import ControlPlane


def _delta(commands=("cc", "make"), missing=(), provisioned=()):
    return {
        "schema": DELTA_SCHEMA,
        "package_manager": "sandbox-toolchain",
        "commands": list(commands),
        "missing_after": list(missing),
        "provisioned": list(provisioned),
        "toolchain_root": "/sandbox/task/.mac-toolchain",
        "reason": "repository_contract.toolchain.required_commands",
    }


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("nanolang", dispatch_paused=False)
    return plane


# --------------------------------------------------------------------------
# What counts as an excursion
# --------------------------------------------------------------------------


def test_a_command_still_missing_after_provisioning_is_an_excursion():
    excursion = excursion_from_delta(_delta(missing=["qemu-system-riscv64"]), project="c26")

    assert excursion["escaped_commands"] == ["qemu-system-riscv64"]
    assert excursion["missing_after"] == ["qemu-system-riscv64"]


def test_a_command_that_had_to_be_provisioned_is_also_an_excursion():
    """The work succeeded and the image still did not cover the contract.

    Counting only hard failures would miss every case the sandbox papered over
    -- which is most of them, and exactly the ones the Containerfile comments
    were eventually written about.
    """
    excursion = excursion_from_delta(_delta(provisioned=["lein"]), project="nanolang")

    assert excursion["escaped_commands"] == ["lein"]
    assert excursion["provisioned"] == ["lein"]


def test_the_two_kinds_are_reported_separately():
    """They mean different things to a reviewer: one gap was papered over, the
    other could not be."""
    excursion = excursion_from_delta(_delta(missing=["qemu"], provisioned=["lein"]), project="c26")

    assert excursion["missing_after"] == ["qemu"]
    assert excursion["provisioned"] == ["lein"]
    assert excursion["escaped_commands"] == ["lein", "qemu"]


def test_a_contract_the_image_fully_covered_is_not_an_excursion():
    """The common case must be silent, or the signal is worthless."""
    assert excursion_from_delta(_delta(), project="nanolang") is None


@pytest.mark.parametrize(
    "delta",
    [None, {}, "junk", {"schema": "something.else.v1", "missing_after": ["cc"]}],
)
def test_a_malformed_or_foreign_record_is_ignored(delta):
    assert excursion_from_delta(delta, project="nanolang") is None


# --------------------------------------------------------------------------
# Filing
# --------------------------------------------------------------------------


def test_an_excursion_becomes_a_task_against_the_offending_project(cp):
    report = cp.record_sandbox_excursion(
        _delta(missing=["libssl-dev"]), project="nanolang", task_id="task_abc"
    )

    assert len(report["filed"]) == 1
    filed = cp.get_task(report["filed"][0])
    assert filed.project == "nanolang", "filed against the wrong project"
    assert "libssl-dev" in filed.title


def test_the_task_carries_the_delta_for_audit(cp):
    report = cp.record_sandbox_excursion(
        _delta(missing=["libssl-dev"]), project="nanolang", task_id="task_abc"
    )

    marker = cp.get_task(report["filed"][0]).metadata[EXCURSION_METADATA_KEY]
    assert marker["command"] == "libssl-dev"
    assert marker["observed_on_task_id"] == "task_abc"
    assert marker["reason"] == "repository_contract.toolchain.required_commands"


def test_the_task_states_the_decision_it_exists_for(cp):
    """A reviewer must be able to close it either way: fix the image, or fix
    the contract. A report that does not name the decision gets read once."""
    report = cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")
    description = cp.get_task(report["filed"][0]).description

    assert "contract is RIGHT" in description
    assert "contract is WRONG" in description
    assert "toolchain.required_commands" in description


def test_the_report_says_the_work_was_not_blocked(cp):
    """Whoever reads this must not think a task failed over it."""
    report = cp.record_sandbox_excursion(_delta(provisioned=["lein"]), project="nanolang")

    assert "NOT blocked" in cp.get_task(report["filed"][0]).description


def test_one_missing_tool_is_one_task(cp):
    """A tool absent across a hundred tasks must not file a hundred tickets.

    Noise is what stops people reading, and this only pays off if it is read.
    """
    first = cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")
    second = cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")

    assert len(first["filed"]) == 1
    assert second["filed"] == []
    assert second["skipped"] == ["libssl-dev"]


def test_dedupe_reads_the_marker_not_the_title(cp):
    """Titles get edited; a title-keyed dedupe starts duplicating the first
    time somebody rewords one."""
    report = cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")
    cp.update_task(report["filed"][0], title="Renamed by a human", actor="operator")

    again = cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")

    assert again["filed"] == []


def test_a_different_command_still_files(cp):
    """Dedupe must be per command, not per project."""
    cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")
    second = cp.record_sandbox_excursion(_delta(missing=["qemu"]), project="nanolang")

    assert len(second["filed"]) == 1


def test_the_same_command_in_another_project_still_files(cp):
    """Two projects needing the same absent tool are two contract questions."""
    cp.create_project("c26", dispatch_paused=False)
    cp.record_sandbox_excursion(_delta(missing=["qemu"]), project="nanolang")
    second = cp.record_sandbox_excursion(_delta(missing=["qemu"]), project="c26")

    assert len(second["filed"]) == 1


def test_a_clean_run_files_nothing(cp):
    report = cp.record_sandbox_excursion(_delta(), project="nanolang")

    assert report["filed"] == [] and report["skipped"] == []


# --------------------------------------------------------------------------
# It must never become a gate
# --------------------------------------------------------------------------


def test_filing_failures_never_raise(cp, monkeypatch):
    """The task that hit the excursion has already succeeded.

    Turning a report into an exception would fail work over a diagnostic --
    strictly worse than the silence being fixed.
    """

    def boom(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(cp, "create_task", boom)

    report = cp.record_sandbox_excursion(_delta(missing=["libssl-dev"]), project="nanolang")

    assert report["filed"] == []
    assert report["skipped"] == ["libssl-dev"]


def test_an_unknown_project_does_not_raise(cp):
    report = cp.record_sandbox_excursion(_delta(missing=["cc"]), project=None)

    assert isinstance(report["filed"], list)
