"""`GET /tasks` must accept the repeated `state=` the CLI actually sends.

REPORTED FROM A REAL HUB. `mac task list` in a nanolang checkout printed
"(none)" while `--all-states` showed a RUNNING task and a BLOCKED one.

#407 changed `mac task list` to default to active work, sending one `state=`
per active state:

    ?state=open&state=waiting&state=blocked&state=claimed&state=running
    &state=needs_review&state=needs_input&state=reviewing&project=nanolang

The route declared `state: Optional[str]`, so FastAPI kept only the LAST
repeat. The default view therefore filtered on `reviewing` alone, and any
project without a task under review looked empty.

WHY THE #407 TESTS PASSED. They drove `ControlPlane.list_tasks` directly, which
already accepted a sequence. Nothing exercised the HTTP route the deployed hub
serves -- the same shape as the retention bug, where the fix was verified
against a path production does not use. These tests go through TestClient for
that reason.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.models import ACTIVE_TASK_STATES
from mac.services import ControlPlane


@pytest.fixture()
def client_and_cp():
    cp = ControlPlane.in_memory()
    return TestClient(create_app(control_plane=cp)), cp


def _titles(resp):
    return {row["title"] for row in resp.json()}


def _force_state(cp, task, state):
    """Set a task's state directly.

    These tests exercise the QUERY FILTER, not the transition rules -- and the
    legitimate transitions require a worker lease, which would make every case
    here about leasing instead of about `state=`.
    """
    cp.store.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task.id))


def test_repeated_state_params_are_all_honoured(client_and_cp):
    """The exact query the CLI sends. This is the reported bug."""
    client, cp = client_and_cp
    cp.create_task("running one", project="nanolang")
    cp.create_task("open one", project="nanolang")

    query = "&".join("state=%s" % s for s in ACTIVE_TASK_STATES)
    resp = client.get("/tasks?%s&project=nanolang" % query)

    assert resp.status_code == 200
    assert _titles(resp) == {"running one", "open one"}, (
        "only the last repeated state= survived, so the default CLI view "
        "filtered on one state and reported (none)"
    )


def test_a_single_state_still_filters_exactly(client_and_cp):
    """`--state=open` must keep meaning open, not 'any active'."""
    client, cp = client_and_cp
    open_task = cp.create_task("stays open", project="nanolang")
    other = cp.create_task("gets blocked", project="nanolang")
    _force_state(cp, other, "blocked")

    resp = client.get("/tasks?state=open&project=nanolang")

    assert _titles(resp) == {"stays open"}
    assert open_task.id in {row["id"] for row in resp.json()}


def test_two_states_select_exactly_those_two(client_and_cp):
    client, cp = client_and_cp
    cp.create_task("a", project="p")
    b = cp.create_task("b", project="p")
    _force_state(cp, b, "blocked")
    c = cp.create_task("c", project="p")
    _force_state(cp, c, "cancelled")

    resp = client.get("/tasks?state=open&state=blocked&project=p")

    assert _titles(resp) == {"a", "b"}


def test_no_state_still_returns_every_state(client_and_cp):
    """Omitting the filter is not the same as filtering on nothing."""
    client, cp = client_and_cp
    cp.create_task("live", project="p")
    done = cp.create_task("done", project="p")
    _force_state(cp, done, "cancelled")

    assert _titles(client.get("/tasks?project=p")) == {"live", "done"}


def test_a_terminal_state_is_selectable_on_its_own(client_and_cp):
    """`--state=cancelled` is how an operator audits what was cancelled."""
    client, cp = client_and_cp
    cp.create_task("live", project="p")
    done = cp.create_task("done", project="p")
    _force_state(cp, done, "cancelled")

    assert _titles(client.get("/tasks?state=cancelled&project=p")) == {"done"}


def test_the_project_filter_still_applies_alongside_many_states(client_and_cp):
    """The reported symptom was project-scoped; both filters must compose."""
    client, cp = client_and_cp
    cp.create_task("mine", project="nanolang")
    cp.create_task("theirs", project="mac")

    query = "&".join("state=%s" % s for s in ACTIVE_TASK_STATES)
    assert _titles(client.get("/tasks?%s&project=nanolang" % query)) == {"mine"}


def test_an_unknown_state_matches_nothing_rather_than_everything(client_and_cp):
    """Fail closed. A typo must not silently widen the view."""
    client, cp = client_and_cp
    cp.create_task("live", project="p")

    assert client.get("/tasks?state=oepn&project=p").json() == []
