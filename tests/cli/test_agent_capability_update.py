"""An agent's advertised capabilities must be editable without a redeploy.

Capabilities are DECLARED, not probed: the worker install writes a hardcoded
default list that never mentioned `c`, `make` or `python3`. Measured on the
live fleet 2026-08-08, every host had cc/gcc/clang/make/python3 installed and
exactly ONE agent advertised `c` -- because somebody had set
MAC_WORKER_CAPABILITIES on it by hand. Every C task could therefore match one
worker, and a single transient failure that excluded it made the work
permanently undispatchable.

``ControlPlane.update_agent`` and ``PUT /agents/{id}`` have always accepted
``capabilities``. Only this command ignored it, so the sole way to change what
an agent advertises was to re-register it through a fleet deploy -- which is a
long way round for "this machine gained a toolchain and the record has not
caught up".

--add/--remove edit the set in place; --capabilities replaces it wholesale.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


@pytest.fixture()
def agent(tmp_path):
    rc, machine = _run(tmp_path, "machine", "register", "host-a")
    assert rc == 0
    rc, created = _run(
        tmp_path, "agent", "register", machine["id"], "worker-a",
        "--capabilities", "python,testing",
    )
    assert rc == 0
    return created


def _caps(tmp_path, agent_id):
    _rc, shown = _run(tmp_path, "agent", "show", agent_id)
    return sorted(shown.get("capabilities") or [])


def test_a_capability_can_be_added_in_place(tmp_path, agent):
    """The live case: the machine has a C toolchain, the record does not."""
    rc, _ = _run(tmp_path, "agent", "update", agent["id"], "--add-capability", "c")

    assert rc == 0
    assert _caps(tmp_path, agent["id"]) == ["c", "python", "testing"]


def test_adding_keeps_the_existing_set(tmp_path, agent):
    """An add must not be a silent replace; that is how an agent loses work."""
    _run(tmp_path, "agent", "update", agent["id"], "--add-capability", "make")

    caps = _caps(tmp_path, agent["id"])
    assert "python" in caps and "testing" in caps and "make" in caps


def test_several_capabilities_can_be_added_at_once(tmp_path, agent):
    _run(
        tmp_path, "agent", "update", agent["id"],
        "--add-capability", "c", "--add-capability", "make",
    )

    assert _caps(tmp_path, agent["id"]) == ["c", "make", "python", "testing"]


def test_a_capability_can_be_removed(tmp_path, agent):
    """Withdrawing a claim matters as much: an agent advertising something it
    cannot do gets matched, claims the work, and fails it."""
    _run(tmp_path, "agent", "update", agent["id"], "--remove-capability", "testing")

    assert _caps(tmp_path, agent["id"]) == ["python"]


def test_capabilities_can_be_replaced_wholesale(tmp_path, agent):
    _run(tmp_path, "agent", "update", agent["id"], "--capabilities", "c,make")

    assert _caps(tmp_path, agent["id"]) == ["c", "make"]


def test_adding_a_capability_twice_is_idempotent(tmp_path, agent):
    _run(tmp_path, "agent", "update", agent["id"], "--add-capability", "c")
    _run(tmp_path, "agent", "update", agent["id"], "--add-capability", "c")

    assert _caps(tmp_path, agent["id"]).count("c") == 1


def test_removing_something_absent_is_not_an_error(tmp_path, agent):
    rc, _ = _run(tmp_path, "agent", "update", agent["id"], "--remove-capability", "fortran")

    assert rc == 0
    assert _caps(tmp_path, agent["id"]) == ["python", "testing"]


def test_instance_kind_still_works_on_its_own(tmp_path, agent):
    """It used to be required; making it optional must not break it."""
    rc, _ = _run(tmp_path, "agent", "update", agent["id"], "--instance-kind", "static")

    assert rc == 0
    assert _caps(tmp_path, agent["id"]) == ["python", "testing"]


def test_an_update_with_no_fields_is_refused(tmp_path, agent):
    """Silently succeeding would hide a typo'd flag."""
    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, "agent", "update", agent["id"])

    assert "nothing to update" in str(excinfo.value)
