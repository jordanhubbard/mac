"""Every route the CLI calls must exist on the hub, with a compatible shape.

The Fleet IDE has had this gate for its TypeScript client since the day someone
noticed a prose claim that 15 methods mapped one-to-one while the client
exported 32 -- "a canary that misses half its surface trains readers to trust
it" (tests/ui/test_fleet_ide_api_contracts.py). The FIRST-PARTY Python client
had no equivalent.

WHAT THAT COST. #418, reported by a human as "mac task list for nanolang
returns nothing". #407 taught the CLI to send one `state=` per active state:

    ?state=open&state=waiting&state=blocked&...&project=nanolang

while `GET /tasks` still declared `state: Optional[str]`. FastAPI kept only the
LAST repeat, so the default view filtered on `reviewing` alone and any project
without a task under review looked empty. The CLI was taught to send a list
without the route being taught to read one, and nothing compared them.

That is statically detectable, which is what this file does. It checks the
DISPATCH LAYER rather than the CLI, because RemoteDispatch is the single seam
every hub-mode client passes through -- the CLI today, the MCP server next.
One gate, both clients.
"""

from __future__ import annotations

import ast
import re
import typing
from pathlib import Path

import pytest

from mac.api import create_app
from mac.services import ControlPlane

ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "src" / "mac" / "dispatch.py"
HTTP_HELPERS = {"_get": "GET", "_post": "POST", "_put": "PUT", "_delete": "DELETE"}


def _path_literals(node: ast.AST, assigns: dict) -> list:
    """Every path string this expression can evaluate to.

    A ternary yields BOTH branches rather than one: `list_command_audit` picks
    between /agents/{id}/command-audit and /command-audit, and checking only
    the first would leave the other unverified while looking thorough.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        left = _path_literals(node.left, assigns)
        # A trailing "%s" fed by _query(...) is a QUERY STRING, not a path
        # segment. Two call sites build "/projects/%s%s" that way; treating the
        # second placeholder as a path component invents a route that has never
        # existed.
        right = node.right
        elements = right.elts if isinstance(right, ast.Tuple) else [right]
        trailing_query = bool(elements) and _is_query_call(elements[-1])
        if trailing_query:
            left = [re.sub(r"%s$", "", value) for value in left]
        return left
    if isinstance(node, ast.IfExp):
        return _path_literals(node.body, assigns) + _path_literals(node.orelse, assigns)
    if isinstance(node, ast.Name):
        return list(assigns.get(node.id, []))
    return []


def _is_query_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_query"
    )


def _dispatch_calls():
    """(verb, path, lineno, kwargs) for every hub call in dispatch.py."""
    tree = ast.parse(DISPATCH.read_text(encoding="utf-8"))
    calls, unresolved = [], []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigns: dict = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found = _path_literals(node.value, assigns)
                        if found:
                            assigns[target.id] = found
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
                and func.attr in HTTP_HELPERS
                and node.args
            ):
                continue
            found = _path_literals(node.args[0], assigns)
            kwargs = [kw.arg for kw in node.keywords if kw.arg]
            if found:
                calls.extend(
                    (HTTP_HELPERS[func.attr], path, node.lineno, tuple(kwargs))
                    for path in found
                )
            else:
                unresolved.append((func.attr, node.lineno))
    return calls, unresolved


def _normalise(path: str) -> str:
    return re.sub(r"%[sd]", "{}", path.split("?")[0]).rstrip("/") or "/"


@pytest.fixture(scope="module")
def hub_routes():
    app = create_app(control_plane=ControlPlane.in_memory())
    routes = {}
    for route in app.routes:
        template = re.sub(r"\{[^}]+\}", "{}", getattr(route, "path", ""))
        for method in getattr(route, "methods", None) or []:
            routes.setdefault((method, template.rstrip("/") or "/"), []).append(route)
    return routes


# --------------------------------------------------------------------------
# every call resolves
# --------------------------------------------------------------------------


def test_every_route_the_dispatch_layer_calls_exists(hub_routes):
    calls, _ = _dispatch_calls()
    assert calls, "extracted no calls; the extractor is broken, not the code"

    missing = sorted(
        {
            "%s %s  (dispatch.py:%d)" % (verb, _normalise(path), lineno)
            for verb, path, lineno, _ in calls
            if (verb, _normalise(path)) not in hub_routes
        }
    )

    assert not missing, "dispatch calls routes the hub does not serve:\n  %s" % (
        "\n  ".join(missing)
    )


def test_no_call_site_is_silently_skipped():
    """Coverage must not shrink quietly.

    A gate that cannot read a call site and says nothing is the failure the
    Fleet IDE canary was rewritten to fix. If a future refactor builds a path
    in a way this extractor cannot follow, that must be loud.
    """
    _, unresolved = _dispatch_calls()

    assert not unresolved, (
        "could not resolve the path for %d dispatch call(s): %s. Either make "
        "the path statically visible or teach _path_literals to follow it -- "
        "do not leave it unchecked." % (len(unresolved), unresolved)
    )


def test_the_extractor_sees_the_whole_surface():
    """A floor, so a broken extractor cannot pass by finding nothing."""
    calls, _ = _dispatch_calls()

    assert len(calls) > 200, "only %d calls extracted; expected the full surface" % len(
        calls
    )


# --------------------------------------------------------------------------
# parameter shapes -- this is where #418 lived
# --------------------------------------------------------------------------


def _query_params(route) -> dict:
    hints = typing.get_type_hints(route.endpoint)
    return {name: hint for name, hint in hints.items() if name != "return"}


def test_task_listing_accepts_repeated_state(hub_routes):
    """The #418 regression, pinned at the route.

    `mac task list` sends one `state=` per active state. Declared as a scalar,
    FastAPI keeps only the last repeat and the default view silently filters on
    one state.
    """
    route = hub_routes[("GET", "/tasks")][0]
    state = _query_params(route)["state"]

    assert "List" in str(state) or "list" in str(state), (
        "GET /tasks declares state as %s; the CLI sends it repeated, so a "
        "scalar keeps only the last value" % state
    )


def test_every_query_parameter_the_client_sends_is_declared(hub_routes):
    """A kwarg the route does not declare is silently dropped by FastAPI --
    the request succeeds and the filter does nothing."""
    calls, _ = _dispatch_calls()
    unknown = []
    for verb, path, lineno, kwargs in calls:
        if verb != "GET" or not kwargs:
            continue
        matches = hub_routes.get((verb, _normalise(path)))
        if not matches:
            continue
        declared = set(_query_params(matches[0]))
        for name in kwargs:
            if name not in declared:
                unknown.append(
                    "%s %s sends %r (dispatch.py:%d) -- not declared"
                    % (verb, _normalise(path), name, lineno)
                )

    assert not unknown, "query parameters the hub ignores:\n  %s" % "\n  ".join(
        sorted(set(unknown))
    )


def test_eval_run_filtering_actually_filters():
    """Regression for the first bug this gate found.

    `list_eval_runs` sent `eval_set=` while GET /eval-runs declares
    `eval_set_id`. FastAPI drops an undeclared query parameter silently, so
    `mac admin eval run list --eval-set X` returned EVERY run rather than X's
    -- a filter that reports success while filtering nothing.
    """
    import inspect

    from mac.dispatch import RemoteDispatch

    source = inspect.getsource(RemoteDispatch.list_eval_runs)

    assert "eval_set_id=" in source
    assert 'self._get("/eval-runs", eval_set=' not in source
