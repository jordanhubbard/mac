"""Exhaustive transport-contract probes for :class:`RemoteDispatch`.

The CLI has a large remote surface.  These tests deliberately exercise every
public wrapper with both its minimal and fully-populated argument shapes.  A
new wrapper therefore has to be able to serialize a request (or explicitly
declare itself local-only) before it can ship.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from mac.dispatch import DispatchError, RemoteDispatch


_OBJECT_KEYS = """
task lease project workflow run tenant user persona instance context
interaction machine agent mood schedule nap deployment rollout eval_set
eval_run channel policy status stream review publication secret runtime delta
artifact environment finding memory evidence
""".split()
_LIST_KEYS = """
tasks projects items results agents personas moods runs messages streams chunks
audits runtimes deltas artifacts environments deployments findings observations
memories rollouts eval_sets eval_runs channels events policies assignments
action_events
""".split()


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def stream_lines(self, path: str, **_kwargs: Any) -> Any:
        """The second transport verb: a held NDJSON connection.

        Recorded like any other call, because the contract this file enforces
        is "a remote method issues a hub request", not "a remote method calls
        .request". A streaming method that opened a socket without going
        through the client would satisfy the letter and defeat the point.
        """
        self.calls.append(("GET", path, None))
        return iter(())

    def request(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        self.calls.append((method, path, body))
        return {
            **{key: {} for key in _OBJECT_KEYS},
            **{key: [] for key in _LIST_KEYS},
            "id": "id",
            "lease_id": "lease",
            "lease_expires_at": "2030-01-01T00:00:00+00:00",
            "content": "value",
            "deleted": True,
            "removed": 2,
            "value": {},
            "stats": {},
        }


def _sample(name: str) -> Any:
    if name == "action":
        return "pause"
    if name == "channel_type":
        return "slack"
    if name == "package_manager":
        return "pip"
    if name == "tier":
        return "medium"
    if name == "kind":
        return "log"
    if name in {
        "paused",
        "enabled",
        "all_agents",
        "restart",
        "embed_into_medium",
        "emit_dream_artifacts",
        "sync_beads",
    }:
        return True
    if name in {
        "limit",
        "priority",
        "after_sequence",
        "poll_interval_seconds",
        "lease_seconds",
        "stale_after_seconds",
        "max_attempts",
        "baseline_score",
        "expected_plan_version",
        "expected_epoch",
    }:
        return 1
    if name in {"nap_interval_hours", "min_score"}:
        return 1.0
    if name in {
        "manifest",
        "metadata",
        "detail",
        "input",
        "pre_decisions",
        "scopes",
        "checks",
        "plan",
        "probe",
        "expected_attestation",
    }:
        return {"sample": "value"}
    if name == "artifacts":
        return [{}]
    if name in {
        "required_capabilities",
        "dependencies",
        "commands",
        "added_dependencies",
        "recipient_agent_ids",
        "restart_services",
    }:
        return ["sample"]
    return "sample"


def _call_arguments(method: Any, *, include_optional: bool) -> tuple[list[Any], dict[str, Any]]:
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for parameter in list(inspect.signature(method).parameters.values())[1:]:
        if parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
            continue
        if not include_optional and parameter.default is not inspect.Parameter.empty:
            continue
        value = _sample(parameter.name)
        if parameter.kind is parameter.POSITIONAL_ONLY:
            args.append(value)
        else:
            kwargs[parameter.name] = value
    if method.__name__ == "create_openshell_policy":
        kwargs.update(name="sample", policy_text="filesystem: read")
    return args, kwargs


_REMOTE_METHODS = [
    (name, method)
    for name, method in inspect.getmembers(RemoteDispatch, inspect.isfunction)
    if not name.startswith("_")
]


@pytest.mark.parametrize("name,method", _REMOTE_METHODS, ids=[name for name, _ in _REMOTE_METHODS])
@pytest.mark.parametrize("include_optional", [False, True], ids=["minimal", "complete"])
def test_every_remote_dispatch_method_has_a_transport_contract(
    name: str,
    method: Any,
    include_optional: bool,
) -> None:
    client = RecordingClient()
    dispatch = RemoteDispatch(client)
    args, kwargs = _call_arguments(method, include_optional=include_optional)

    try:
        getattr(dispatch, name)(*args, **kwargs)
    except DispatchError as exc:
        message = str(exc)
        assert (
            "local-only" in message
            or "no HTTP endpoint" in message
            or "vector writer on the hub" in message
        )
        assert not client.calls
    else:
        assert client.calls, "%s returned without issuing a hub request" % name
        http_method, path, _body = client.calls[-1]
        assert http_method in {"GET", "POST", "PUT", "DELETE"}
        assert path.startswith("/")


def test_dispatch_result_wrapper_debug_and_mapping_protocol() -> None:
    from mac.dispatch import _Dictish, _wrap_list

    value = _Dictish({"id": "task_1"})
    assert repr(value) == "_Dictish({'id': 'task_1'})"
    assert value.id == "task_1"
    assert value["id"] == "task_1"
    assert value.get("missing", "fallback") == "fallback"
    assert "id" in value
    assert bool(value)
    with pytest.raises(AttributeError):
        _ = value.missing
    assert _wrap_list({"unexpected": "mapping"})[0].to_dict() == {
        "unexpected": "mapping"
    }


def test_update_agent_uses_hub_put_endpoint_and_preserves_actor() -> None:
    client = RecordingClient()
    dispatch = RemoteDispatch(client)

    dispatch.update_agent(
        "agent/worker",
        resources={"openshell_required": True},
        actor="openshell-reconcile",
    )

    assert client.calls[-1] == (
        "PUT",
        "/agents/agent%2Fworker",
        {
            "resources": {"openshell_required": True},
            "actor": "openshell-reconcile",
        },
    )


def test_dispatch_hold_uses_hub_endpoints_and_quotes_agent_id() -> None:
    client = RecordingClient()
    dispatch = RemoteDispatch(client)

    dispatch.set_agent_dispatch_hold("agent/worker", "manual quarantine")
    dispatch.clear_agent_dispatch_hold("agent/worker")

    assert client.calls[-2:] == [
        (
            "POST",
            "/agents/agent%2Fworker/dispatch-hold",
            {"reason": "manual quarantine"},
        ),
        ("DELETE", "/agents/agent%2Fworker/dispatch-hold", None),
    ]


def test_observability_prune_uses_hub_endpoint_and_returns_count() -> None:
    client = RecordingClient()
    dispatch = RemoteDispatch(client)

    removed = dispatch.prune_observability(
        older_than="2026-01-01T00:00:00+00:00",
        keep_last=100,
    )

    assert removed == 2
    assert client.calls[-1] == (
        "POST",
        "/observability/prune",
        {
            "older_than": "2026-01-01T00:00:00+00:00",
            "keep_last": 100,
        },
    )


# ---------------------------------------------------------------------------
# The humans wrappers, by path.
#
# /humans existed on the hub from the multi-user slice and RemoteDispatch never
# wrapped it, so `mac admin human ...` worked against --db and failed against a
# hub with "not yet supported in hub mode". Since an agent's owner has to be a
# registered principal, that made it impossible to name an owner on a live
# fleet at all -- the ownership model shipped with no way to use it.
# ---------------------------------------------------------------------------


def test_registering_a_human_posts_to_humans():
    client = RecordingClient()
    dispatch = RemoteDispatch(client)

    dispatch.register_human("jordanh", display_name="Jordan Hubbard")

    method, path, body = client.calls[-1]
    assert (method, path) == ("POST", "/humans")
    assert body["username"] == "jordanh"


def test_listing_humans_gets_humans():
    client = RecordingClient()
    dispatch = RemoteDispatch(client)

    dispatch.list_humans()

    method, path, _body = client.calls[-1]
    assert method == "GET"
    assert path.startswith("/humans")


def test_a_username_is_resolved_by_the_hub_not_by_the_client():
    """The hub decides what an anchor means. A client that reimplemented that
    rule by listing and filtering would disagree with it the moment the rule
    changed -- and ownership fields store ids, so a wrong resolution silently
    points an agent at the wrong principal."""
    client = RecordingClient()
    dispatch = RemoteDispatch(client)

    dispatch.get_human_by_username("jordanh")

    method, path, _body = client.calls[-1]
    assert method == "GET"
    assert path.startswith("/humans/resolve")
    assert "anchor=jordanh" in path
