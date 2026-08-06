"""Never reap a sandbox that is not ours — asserted without forking anything.

This replaces the flaky half of tests/test_fleet_node_daemon_quiescence.py.
Those tests proved a real safety property, but they proved it by spawning a
subprocess and waiting on it, so under the parallel contract gate they failed
for reasons that had nothing to do with the code: five failures on 2026-08-06
that all passed in isolation and on clean main.

The property is worth keeping. ``classify_orphan_task_sandbox`` decides whether
a listed sandbox may be deleted, and getting it wrong means destroying someone
else's container. The decision is a pure function of the sandbox's name and
labels, so it can be asserted directly and deterministically. Only the harness
was ever timing-dependent.

The function is extracted from deploy/fleet-node-install.sh and executed as-is,
so this tests the shipped decision rather than a copy of it. If the installer's
logic changes, these tests see the change.

Worker instability is handled at runtime, not asserted away here — see
task_85132813.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"


def _load_classifier():
    """Execute the installer's real classification block in this process."""
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("managed_task_sandbox_name = re.compile(")
    end = text.index("def list_openshell_sandboxes():", start)
    source = text[start:end]
    namespace: Dict[str, Any] = {"re": re, "os": os}
    exec(compile(source, str(INSTALLER), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture(scope="module")
def classifier():
    ns = _load_classifier()
    assert "sandbox_pid_is_alive" in ns, (
        "the installer no longer defines sandbox_pid_is_alive; liveness "
        "injection below must be repointed"
    )
    # Decide liveness by fiat rather than by consulting the process table:
    # the real function is os.kill(pid, 0), which makes every assertion depend
    # on scheduling and on pids the OS may reuse. The classifier's OWN logic is
    # what is under test here, not whether a forked child had exited yet.
    ns["sandbox_pid_is_alive"] = lambda pid: str(pid) != DEAD_PID
    assert "classify_orphan_task_sandbox" in ns, (
        "the installer no longer defines classify_orphan_task_sandbox; this "
        "test must be repointed rather than deleted -- it is the guard against "
        "reaping a container that is not ours"
    )
    return ns


def _sandbox(name: str, **labels: Any) -> Dict[str, Any]:
    base = {
        "mac.owner": "mac",
        "mac.kind": "task",
        "mac.keep": "false",
        # A PID that cannot be alive, so the decision never depends on the
        # process table. The cases below return before this is consulted.
        "mac.pid": "0",
    }
    base.update({k.replace("_", "."): v for k, v in labels.items()})
    return {"name": name, "labels": base}


# --------------------------------------------------------------------------
# The property: an unrecognized sandbox is never touched
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "mac-unmanaged-prefix-fixture",
        "task-mac-task-fixture",
        "mac-task-",
        "not-mac-at-all",
        "mac-openclaw-agent-lookalike",
    ],
)
def test_a_name_outside_the_managed_set_is_never_reaped(classifier, name):
    """`mac-openclaw-agent-lookalike` is the one that matters most.

    A near-miss on the gateway's own name must not be mistaken for a disposable
    task sandbox, or a deploy would delete the running chat gateway.
    """
    record = classifier["classify_orphan_task_sandbox"](_sandbox(name))

    assert record["reap"] is False, "would have deleted an unmanaged sandbox"
    assert record["preserve"] is False


@pytest.mark.parametrize("kind", ["gateway", "openshell-gateway", "", "daemon", "unknown-kind"])
def test_an_unmanaged_kind_is_never_reaped(classifier, kind):
    """A recognized name with a foreign mac.kind fails closed.

    `openshell-gateway` is the live gateway's kind: reaping it on a name match
    alone would take the host's chat gateway down.
    """
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox("mac-task-badkind-fixture", mac_kind=kind)
    )

    assert record["reap"] is False, "would have deleted a %r sandbox" % kind
    assert record["preserve"] is False


@pytest.mark.parametrize("owner", ["other", "MAC-imposter", "", "hub"])
def test_a_foreign_or_blank_owner_is_never_reaped(classifier, owner):
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox("mac-task-foreign-fixture", mac_owner=owner)
    )

    assert record["reap"] is False, "would have deleted another owner's sandbox"


def test_a_missing_or_unknown_keep_label_fails_closed(classifier):
    """Absence of permission is not permission."""
    for keep in ("", "maybe", "later", None):
        sandbox = _sandbox("mac-task-keep-fixture")
        if keep is None:
            sandbox["labels"].pop("mac.keep")
        else:
            sandbox["labels"]["mac.keep"] = keep
        record = classifier["classify_orphan_task_sandbox"](sandbox)
        assert record["reap"] is False, "reaped on keep=%r" % keep


def test_a_live_pid_is_never_reaped(classifier):
    """Our own sandbox, still running, must survive."""
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox("mac-task-live-fixture", mac_pid="777777")
    )
    assert record["reap"] is False


@pytest.mark.parametrize("pid", ["", "not-a-number", "-1", "0"])
def test_an_unusable_pid_is_never_reaped(classifier, pid):
    """Without a usable pid there is no death to prove, so do nothing."""
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox("mac-task-pid-fixture", mac_pid=pid)
    )
    assert record["reap"] is False


# --------------------------------------------------------------------------
# The other side: a sandbox that IS ours and provably dead
# --------------------------------------------------------------------------


#: A pid the classifier will be told is dead. Forking a real process to get a
#: genuinely-dead pid would reintroduce exactly the dependence on process
#: scheduling that this file exists to remove, so liveness is injected instead
#: (see the `classifier` fixture).
DEAD_PID = "424242"


def test_our_own_dead_disposable_sandbox_is_reaped(classifier):
    """The positive case, or the guards above would pass vacuously."""
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox("mac-task-dead-fixture", mac_pid=DEAD_PID)
    )
    assert record["reap"] is True
    assert record["preserve"] is False


def test_a_dead_task_marked_keep_is_preserved_not_reaped(classifier):
    """A dead executor may still hold unpublished work in /sandbox/task."""
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox("mac-task-keepme-fixture", mac_keep="true", mac_pid=DEAD_PID)
    )
    assert record["preserve"] is True
    assert record["reap"] is False


@pytest.mark.parametrize(
    "name",
    [
        "mac-task-x",
        "mac-hubverify-x",
        "mac-codingcap-x",
        "mac-runtime-smoke-x",
        "mac-security-probe-x",
    ],
)
def test_every_managed_prefix_is_recognized(classifier, name):
    """If a prefix stops being recognized, its sandboxes leak for ever."""
    record = classifier["classify_orphan_task_sandbox"](
        _sandbox(name, mac_kind=name.split("-", 1)[1].rsplit("-", 1)[0], mac_pid=DEAD_PID)
    )
    assert record["reap"] is True, "%s is no longer recognized as managed" % name
