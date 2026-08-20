"""Every control-plane method the CLI calls must be reachable in hub mode.

`_plane(args)` returns a LocalDispatch (`--db`) or a RemoteDispatch (a hub).
A method implemented on ControlPlane but missing from RemoteDispatch fails at
runtime with:

    `mac <method>` is not yet supported in hub mode. Pass --db <path> ...

which is a real dead end for operators, because the hub IS how they reach the
ledger. Direct `--db` access is for maintenance and tests.

This caught a live one. ADR 0020's `mac task stop` / `mac task start` shipped
with the CLI verb, the ControlPlane method, tests, and even the hub route — and
no RemoteDispatch wrapper. Nineteen tests passed because every one of them ran
against `dsn_for(tmp_path)`, i.e. direct-DB mode. The feature was verified
everywhere except where it is used.

`test_dispatch_remote_contract` cannot catch this: it parametrizes over methods
that EXIST on RemoteDispatch, so a method missing entirely is invisible to it.
This test starts from the CLI instead, which is where the requirement comes
from.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac.dispatch import RemoteDispatch
from mac.services import ControlPlane

CLI = Path(__file__).resolve().parents[1] / "src" / "mac" / "cli.py"

#: Pre-existing gaps, recorded as a BASELINE rather than as approval.
#:
#: Each of these is a CLI verb that works with `--db` and dead-ends against a
#: hub. They were found by this test on the day it was written, and they are
#: listed so the test can lock the count at "no new ones" instead of staying
#: red and being ignored. Shrinking this set is the work; growing it needs a
#: reason in the diff.
#:
#: They cluster, which is a hint about how the gap happens: dream runs,
#: personas, and AgentBus reads were each built against a local database and
#: never wired for the transport every operator actually uses.
KNOWN_LOCAL_ONLY: frozenset = frozenset(
    {
        "agentbus_inbox_cursor",
        "convert_ticketing_source",
        "decay_memory",
        "delete_secret",
        "detect_ticketing",
        "discard_dream_run",
        "get_dream_run",
        "list_dream_runs",
        "persona_context",
        "persona_runtime_proof",
        "persona_work_context",
        "promote_dream_run",
        "read_agentbus_inbox",
        "register_persona_instance",
        "run_dream_cycle",
    }
)


def _cli_plane_methods() -> set:
    """Method names invoked on the dispatch object from the CLI."""
    text = CLI.read_text(encoding="utf-8")
    # `_plane(args).foo(` and `cp.foo(` where cp = _plane(args)
    names = set(re.findall(r"_plane\([^)]*\)\.([a-zA-Z_][a-zA-Z0-9_]*)\(", text))
    names |= set(re.findall(r"\bcp\.([a-zA-Z_][a-zA-Z0-9_]*)\(", text))
    return {n for n in names if not n.startswith("_")}


def test_the_scan_finds_the_cli_surface():
    """A regex that silently matched nothing would make this suite vacuous."""
    found = _cli_plane_methods()
    assert len(found) > 50, "expected the CLI to reach many control-plane methods"
    assert "create_task" in found


@pytest.mark.parametrize("name", sorted(_cli_plane_methods()))
def test_a_cli_reachable_method_is_reachable_in_hub_mode(name: str):
    """Present on LocalDispatch implies present on RemoteDispatch.

    The asymmetry is the bug: a method that works with --db and not against a
    hub looks finished in every test that uses a temporary database.
    """
    if name in KNOWN_LOCAL_ONLY:
        # Asserted in the NEGATIVE on purpose. An imperative xfail/skip would
        # stay quiet forever once someone fixed one of these, and the baseline
        # would rot into a list of things that are no longer true. Failing when
        # a gap closes forces the entry to be deleted, so the set can only
        # shrink deliberately.
        assert not hasattr(RemoteDispatch, name), (
            "%s now has a RemoteDispatch wrapper -- remove it from "
            "KNOWN_LOCAL_ONLY." % name
        )
        return
    # LocalDispatch forwards to ControlPlane via __getattr__, so ControlPlane
    # is what "reachable with --db" actually means. Checking LocalDispatch
    # directly skips almost everything and makes this suite vacuous.
    if not hasattr(ControlPlane, name):
        pytest.skip("not a control-plane method (CLI helper or local attribute)")

    assert hasattr(RemoteDispatch, name), (
        "`mac` can call %s with --db but not against a hub. Add a RemoteDispatch "
        "wrapper (and the hub route it posts to), or add it to KNOWN_LOCAL_ONLY "
        "with a reason." % name
    )


def test_the_adr_0020_verbs_specifically():
    """Named explicitly because this is the case that shipped broken."""
    for name in ("stop_task", "start_stopped_task"):
        assert hasattr(RemoteDispatch, name), name
