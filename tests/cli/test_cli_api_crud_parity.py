"""The CLI and the API must expose the same CRUD vocabulary.

A first-class object should mean the same thing whichever door you come in by.
Measured on 2026-08-07, it did not: the HTTP API already had

    PUT  /tasks/{task_id}        -- and the CLI had no `mac task update`
    GET  /agents/{agent_id}      -- and the CLI had no `mac agent show`

so two operations existed, were reachable over HTTP, and were simply
unreachable from the command line. The CLI was the side that had drifted.

These tests pin the two surfaces together, in both directions:

  * every CRUD verb the CLI claims has a corresponding HTTP route, so the CLI
    cannot promise something the control plane cannot do; and
  * every CRUD route the API exposes has a CLI verb, so an operation cannot
    again be reachable over HTTP and invisible from the terminal.

Where an operation genuinely does not exist -- work-package has neither an
update nor a delete on EITHER surface -- the two agree about that too, and the
gap is asserted rather than quietly tolerated.
"""

from __future__ import annotations

import argparse
import os

import pytest

from mac.cli import build_parser
from mac.cli_surface import CRUD_VERBS, FIRST_CLASS

#: HTTP shape of each CRUD verb, given a collection base path.
#: ``show``/``update``/``delete`` address a single resource by id.
_COLLECTION = {"create": "POST", "list": "GET"}
_RESOURCE = {"show": "GET", "update": ("PUT", "PATCH"), "delete": "DELETE"}

#: Collection route for each first-class object.
BASE_PATH = {
    "project": "/projects",
    "task": "/tasks",
    "agent": "/agents",
    "work-package": "/work-packages",
}


@pytest.fixture(scope="module")
def api_crud():
    """Which CRUD verbs the HTTP API implements, per first-class object."""
    os.environ.setdefault("MAC_SECRET_KEY", "x" * 40)
    from mac.api import create_app
    from mac.services import ControlPlane

    app = create_app(control_plane=ControlPlane.in_memory())
    routes = [
        (sorted(r.methods - {"HEAD", "OPTIONS"}), r.path)
        for r in app.routes
        if hasattr(r, "methods")
    ]
    found = {}
    for name, base in BASE_PATH.items():
        verbs = set()
        for methods, path in routes:
            if path != base and not path.startswith(base + "/"):
                continue
            tail = path[len(base):]
            resource = tail.startswith("/{") and tail.count("/") == 1
            for method in methods:
                for verb, wanted in _COLLECTION.items():
                    if tail == "" and method == wanted:
                        verbs.add(verb)
                for verb, wanted in _RESOURCE.items():
                    wanted = (wanted,) if isinstance(wanted, str) else wanted
                    if resource and method in wanted:
                        verbs.add(verb)
        found[name] = verbs
    return found


@pytest.fixture(scope="module")
def cli_crud():
    parser = build_parser()
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    found = {}
    for obj in FIRST_CLASS:
        sub = action.choices[obj.name]
        sub_action = next(
            a for a in sub._actions if isinstance(a, argparse._SubParsersAction)
        )
        found[obj.name] = {v for v in CRUD_VERBS if v in sub_action.choices}
    return found


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_the_cli_never_promises_more_than_the_api_implements(obj, cli_crud, api_crud):
    """A CLI verb with no route behind it is a promise the tool cannot keep."""
    extra = cli_crud[obj.name] - api_crud[obj.name]

    assert not extra, (
        "mac %s exposes %s with no corresponding HTTP route"
        % (obj.name, sorted(extra))
    )


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_the_api_never_implements_more_crud_than_the_cli_exposes(obj, cli_crud, api_crud):
    """The direction that was actually broken.

    PUT /tasks/{id} and GET /agents/{id} both existed while `mac task update`
    and `mac agent show` did not, so two operations were reachable over HTTP
    and invisible from the terminal.
    """
    missing = api_crud[obj.name] - cli_crud[obj.name]

    assert not missing, (
        "the API implements %s for %s and the CLI does not expose it"
        % (sorted(missing), obj.name)
    )


@pytest.mark.parametrize("obj", FIRST_CLASS, ids=lambda o: o.name)
def test_the_two_surfaces_agree_exactly(obj, cli_crud, api_crud):
    assert cli_crud[obj.name] == api_crud[obj.name]


def test_work_package_has_update_and_delete_on_both_surfaces(cli_crud, api_crud):
    """This used to assert the opposite, and correctly: neither verb existed,
    and `replan` was not an update -- it installs a compiled replacement plan
    into a package that must already be paused.

    Both are implemented now, and the point of asserting it HERE is that they
    arrived on both surfaces together. A CLI verb with no route behind it is a
    promise the tool cannot keep; a route with no CLI verb is a capability
    nobody can reach.
    """
    for surface in (cli_crud, api_crud):
        assert "update" in surface["work-package"]
        assert "delete" in surface["work-package"]


def test_the_other_three_objects_have_complete_crud(cli_crud, api_crud):
    """Nothing else is allowed to be partial."""
    for name in ("project", "task", "agent"):
        assert cli_crud[name] == set(CRUD_VERBS), (
            "mac %s is missing %s" % (name, sorted(set(CRUD_VERBS) - cli_crud[name]))
        )
        assert api_crud[name] == set(CRUD_VERBS)
