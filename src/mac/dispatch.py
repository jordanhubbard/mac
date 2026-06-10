"""Transport abstraction for the `mac` CLI.

The CLI handlers historically called ``_plane(args).method(...)`` against
a directly-instantiated ``ControlPlane(SQLiteStore(args.db))``. That made
``mac`` SQLite-only: it could never talk to a hub. The merged CLI has
two transports, picked by :func:`resolve_dispatch`.

A ``Dispatch`` is a transport-flavored facade. Two flavors exist:

* ``LocalDispatch`` — a pass-through to an in-process ``ControlPlane``.
* ``RemoteDispatch`` — translates each call to an HTTP request against a
  hub URL using :class:`mac.http_client.HubClient`.

``resolve_dispatch(args)`` decides which flavor to use:

1. ``--db <path>`` (or ``MAC_DB`` env) → local SQLite at that path,
   with a stderr banner so silent local writes can't happen.
2. ``--hub-url URL`` (with optional ``--token``) → remote HTTP.
3. ``MAC_API_URL`` / ``MAC_URL`` / ``MAC_HUB_URL`` env → remote HTTP.
4. A fleet → its ``hub_url`` in ``~/.mac/fleets.yaml`` → remote HTTP. The
   fleet is ``--fleet`` / ``MAC_FLEET`` if set, else the default fleet:
   the lone fleet, or the one marked ``default: true`` in fleets.yaml.
5. Nothing configured → error with help text. No silent fallback.

The effective fleet (explicit, env, or default) also scopes the token via
:func:`mac.fleet_env.resolve` so ``MAC_API_TOKEN__<FLEET>`` takes precedence
over the flat ``MAC_API_TOKEN``. Token values missing from the live
environment are filled from ``~/.mac/.env`` (see :func:`_load_dotenv_into`),
so a configured fleet needs no manual ``source``.

Mirroring ControlPlane's surface
--------------------------------

CLI handlers consume the dispatch with the same call shape as before:
``_dispatch(args).some_method(*args)``. ``LocalDispatch`` forwards
unchanged to ControlPlane. ``RemoteDispatch`` wraps each method as an
HTTP call and returns a ``_Dictish`` (or a list of them) so the
``_print(result)`` helper's ``hasattr(value, 'to_dict')`` check still
works without changes to the handlers.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union
from urllib.parse import quote, urlencode

from mac.fleet_env import resolve as resolve_env_var
from mac.http_client import HubClient, HubClientError
from mac.models import json_dumps


class DispatchError(RuntimeError):
    """Raised when transport resolution or a remote call fails."""


# ---------------------------------------------------------------------------
# Result wrapping
# ---------------------------------------------------------------------------


class _Dictish:
    """Thin wrapper around a JSON-decoded dict.

    Exposes the ``.to_dict()`` contract that ``_print(...)`` in cli.py
    relies on (``hasattr(value, 'to_dict')``). Supports basic dict-like
    access for the (rare) handler that reads a field directly.
    """

    __slots__ = ("_data",)

    def __init__(self, data: Dict[str, Any]) -> None:
        self._data = data or {}

    def to_dict(self) -> Dict[str, Any]:
        return self._data

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getattr__(self, name: str) -> Any:
        # Lets CLI handlers do `lease.id` the same as on a typed object.
        # `__slots__` keeps real attributes (`_data`) off this path.
        try:
            return self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __bool__(self) -> bool:
        return bool(self._data)

    def __repr__(self) -> str:
        return "_Dictish(%r)" % self._data


def _wrap_list(items: Any) -> List[_Dictish]:
    if items is None:
        return []
    if isinstance(items, dict):
        # Some endpoints return {"items": [...]} or {"<resource>": [...]}.
        # The caller is expected to extract the right key before calling
        # _wrap_list; this fallback handles the case where they passed
        # the whole envelope by mistake.
        for key in ("items", "results"):
            if key in items and isinstance(items[key], list):
                items = items[key]
                break
        else:
            return [_Dictish(items)]
    return [_Dictish(it) if isinstance(it, dict) else it for it in items]


# ---------------------------------------------------------------------------
# Local dispatch — pass-through to ControlPlane
# ---------------------------------------------------------------------------


class LocalDispatch:
    """Pass-through to an in-process :class:`mac.services.ControlPlane`.

    Forwarding via ``__getattr__`` keeps the implementation tiny and
    automatically tracks new ControlPlane methods.
    """

    def __init__(self, plane: Any) -> None:
        self._plane = plane

    def __getattr__(self, name: str) -> Any:
        return getattr(self._plane, name)

    @property
    def store(self) -> Any:
        # Some CLI handlers (task ready/search/stats, memory list/forget)
        # reach into ControlPlane.store for direct SQL. Expose it.
        return self._plane.store


# ---------------------------------------------------------------------------
# Remote dispatch — HTTP to a hub
# ---------------------------------------------------------------------------


class _RemoteStore:
    """Stand-in for ``ControlPlane.store`` in remote mode.

    Several CLI handlers reach into ``.store.query_all`` / ``.store.execute``
    to run direct SQL (task ready/search/stats, memory list/forget). Those
    can't be served over HTTP without dedicated routes. Until those routes
    land, raising here gives the user a clear next step instead of a
    confusing AttributeError.
    """

    def _refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise DispatchError(
            "this command needs direct SQLite access (memory list/forget, "
            "observability prune). It is not yet served over HTTP. Pass "
            "--db <path> to run against a local SQLite database, or wait for "
            "the matching hub endpoint to be added."
        )

    # Match the SQLiteStore surface the cli handlers touch.
    query_all = _refuse
    query_one = _refuse
    execute = _refuse


class RemoteDispatch:
    """Translate ControlPlane method calls to HTTP requests against a hub.

    The wrapped methods mirror the ControlPlane surface that ``cli.py``
    handlers invoke. Each method makes a single HTTP request via
    :class:`mac.http_client.HubClient` and wraps JSON responses in
    :class:`_Dictish` so the ``_print`` helper's ``to_dict()`` contract
    keeps working.
    """

    def __init__(self, client: HubClient) -> None:
        self._client = client
        self.store = _RemoteStore()

    # -- low-level helpers ---------------------------------------------------

    def _get(self, path: str, **params: Any) -> Any:
        query = _query(params)
        return self._client.request("GET", path + query)

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._client.request("POST", path, body=body or {})

    def _put(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        return self._client.request("PUT", path, body=body or {})

    def _delete(self, path: str) -> Any:
        return self._client.request("DELETE", path)

    # -- Task surface --------------------------------------------------------

    def create_task(
        self,
        title: str,
        *,
        description: str = "",
        project: Optional[str] = None,
        priority: Optional[int] = None,
        required_capabilities: Optional[List[str]] = None,
        dependencies: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_attempts: Optional[int] = None,
        actor: Optional[str] = None,
        **extra: Any,
    ) -> _Dictish:
        body = _drop_none(
            {
                "title": title,
                "description": description,
                "project": project,
                "priority": priority,
                "required_capabilities": required_capabilities or None,
                "dependencies": dependencies or None,
                "metadata": metadata,
                "max_attempts": max_attempts,
                "actor": actor,
                **extra,
            }
        )
        return _Dictish(self._post("/tasks", body))

    def list_tasks(
        self,
        state: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[_Dictish]:
        return _wrap_list(self._get("/tasks", state=state, tenant_id=tenant_id))

    def ready_tasks(
        self,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[_Dictish]:
        return _wrap_list(
            self._get("/tasks/ready", project=project, tenant_id=tenant_id, limit=limit)
        )

    def search_tasks(
        self,
        query: str,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[_Dictish]:
        return _wrap_list(
            self._get("/tasks/search", q=query, project=project, tenant_id=tenant_id, limit=limit)
        )

    def task_stats(
        self,
        *,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._get("/tasks/stats", project=project, tenant_id=tenant_id)

    def task_detail(self, task_id: str) -> _Dictish:
        return _Dictish(self._get("/tasks/%s" % quote(task_id, safe="")))

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        lease_seconds: Optional[int] = None,
        sync_beads: Optional[bool] = None,
    ) -> tuple:  # type: ignore[type-arg]
        body = _drop_none(
            {
                "agent_id": agent_id,
                "lease_seconds": lease_seconds,
                "sync_beads": sync_beads,
            }
        )
        resp = self._post("/tasks/%s/claim" % quote(task_id, safe=""), body)
        task_payload = resp.get("task") if isinstance(resp, dict) else None
        lease_payload = resp.get("lease") if isinstance(resp, dict) else None
        return _Dictish(task_payload or {}), _Dictish(lease_payload or {})

    def transition_task(
        self,
        task_id: str,
        to_state: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "target_state": to_state,
                "actor": actor,
                "detail": detail or {},
            }
        )
        return _Dictish(self._post("/tasks/%s/transition" % quote(task_id, safe=""), body))

    def start_task(self, task_id: str, agent_id: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/tasks/%s/start" % quote(task_id, safe=""),
                {"agent_id": agent_id},
            )
        )

    def submit_for_review(self, task_id: str, agent_id: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/tasks/%s/submit-for-review" % quote(task_id, safe=""),
                {"agent_id": agent_id},
            )
        )

    def add_evidence(
        self,
        task_id: str,
        kind: str,
        uri: str,
        summary: str,
        created_by: str,
        *,
        checksum: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "kind": kind,
                "uri": uri,
                "summary": summary,
                "created_by": created_by,
                "checksum": checksum,
                "metadata": metadata,
            }
        )
        return _Dictish(self._post("/tasks/%s/evidence" % quote(task_id, safe=""), body))

    # -- Project surface -----------------------------------------------------

    def list_projects(self) -> List[_Dictish]:
        return _wrap_list(self._get("/projects"))

    def create_project(
        self,
        name: str,
        *,
        description: str = "",
        metadata: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "name": name,
                "description": description,
                "metadata": metadata,
                "status": status,
                "actor": actor,
                "project_id": project_id,
            }
        )
        return _Dictish(self._post("/projects", body))

    def get_project(self, project: str) -> _Dictish:
        return _Dictish(self._get("/projects/%s" % quote(project, safe="")))

    # -- Workflow decisions (wf-02) -----------------------------------------

    def workflow_decisions(
        self,
        workflow_id_or_slug: str,
        *,
        tenant_id: Optional[str] = None,
    ) -> _Dictish:
        return _Dictish(
            self._get(
                "/workflows/%s/decisions" % quote(workflow_id_or_slug, safe=""),
                tenant_id=tenant_id,
            )
        )

    def workflow_run_decisions(self, run_id: str) -> _Dictish:
        return _Dictish(
            self._get("/workflows/runs/%s/decisions" % quote(run_id, safe=""))
        )

    def start_workflow(
        self,
        workflow_id_or_slug: str,
        *,
        started_by: str = "human",
        input: Optional[Dict[str, Any]] = None,
        tenant_id: Optional[str] = None,
        pre_decisions: Optional[Dict[str, str]] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "started_by": started_by,
                "input": input or {},
                "tenant_id": tenant_id,
                "pre_decisions": pre_decisions or {},
            }
        )
        return _Dictish(
            self._post("/workflows/%s/start" % quote(workflow_id_or_slug, safe=""), body)
        )

    # -- Tenant / User / Persona / Hermes / Binding / Interaction -----------

    def register_tenant(self, name: str, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/tenants", _drop_none({"name": name, **kw})))

    def list_tenants(self) -> List[_Dictish]:
        return _wrap_list(self._get("/tenants"))

    def register_user(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/users", _drop_none(kw)))

    def register_persona(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/personas", _drop_none(kw)))

    def register_hermes_instance(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/hermes-instances", _drop_none(kw)))

    def hermes_context(self, instance_id: str) -> _Dictish:
        return _Dictish(self._get("/hermes-instances/%s/context" % quote(instance_id, safe="")))

    def hermes_work_context(self, instance_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._get("/hermes-instances/%s/work-context" % quote(instance_id, safe=""), **kw)
        )

    def hermes_runtime_proof(self, instance_id: str, *, hermes_startup: Any = None) -> _Dictish:
        if hermes_startup is None:
            return _Dictish(
                self._get("/hermes-instances/%s/runtime-proof" % quote(instance_id, safe=""))
            )
        return _Dictish(
            self._post(
                "/hermes-instances/%s/runtime-proof" % quote(instance_id, safe=""),
                {"hermes_startup": hermes_startup},
            )
        )

    def register_platform_binding(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/platform-bindings", _drop_none(kw)))

    def create_interaction_task(self, instance_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/hermes-instances/%s/tasks" % quote(instance_id, safe=""),
                _drop_none(kw),
            )
        )

    # -- Machine / Agent / Fleet --------------------------------------------

    def register_machine(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/machines", _drop_none(kw)))

    def register_agent(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/agents", _drop_none(kw)))

    def list_agents(self) -> List[_Dictish]:
        return _wrap_list(self._get("/agents"))

    def heartbeat_agent(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post("/agents/%s/heartbeat" % quote(agent_id, safe=""), _drop_none(kw))
        )

    def fleet_build_distribution(self) -> _Dictish:
        return _Dictish(self._get("/fleet/build-distribution"))

    # -- Mood (per-agent overlay) -------------------------------------------

    def set_mood(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post("/agents/%s/mood" % quote(agent_id, safe=""), _drop_none(kw))
        )

    def get_current_mood(self, agent_id: str) -> Optional[_Dictish]:
        resp = self._get("/agents/%s/mood" % quote(agent_id, safe=""))
        return _Dictish(resp) if resp else None

    def clear_mood(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._delete("/agents/%s/mood" % quote(agent_id, safe=""))
            if not kw
            else self._client.request(
                "DELETE", "/agents/%s/mood" % quote(agent_id, safe=""), body=_drop_none(kw)
            )
        )

    def list_mood_history(self, agent_id: str, *, limit: Optional[int] = None) -> List[_Dictish]:
        return _wrap_list(
            self._get("/agents/%s/mood/history" % quote(agent_id, safe=""), limit=limit)
        )

    # -- Nap (per-agent consolidation) --------------------------------------

    def configure_nap(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post("/agents/%s/nap-schedule" % quote(agent_id, safe=""), _drop_none(kw))
        )

    def get_nap_schedule(self, agent_id: str) -> Optional[_Dictish]:
        resp = self._get("/agents/%s/nap-schedule" % quote(agent_id, safe=""))
        return _Dictish(resp) if resp else None

    def next_nap_window(self, agent_id: str) -> _Dictish:
        return _Dictish(self._get("/agents/%s/nap-schedule/next" % quote(agent_id, safe="")))

    def begin_nap(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post("/agents/%s/nap-runs" % quote(agent_id, safe=""), _drop_none(kw))
        )

    def complete_nap(self, run_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post("/nap-runs/%s/complete" % quote(run_id, safe=""), _drop_none(kw))
        )

    def fail_nap(self, run_id: str, reason: str, *, actor: Optional[str] = None) -> _Dictish:
        return _Dictish(
            self._post(
                "/nap-runs/%s/fail" % quote(run_id, safe=""),
                _drop_none({"reason": reason, "actor": actor}),
            )
        )

    def list_nap_runs(self, agent_id: Optional[str] = None) -> List[_Dictish]:
        return _wrap_list(self._get("/nap-runs", agent_id=agent_id))

    # -- Dispatch -----------------------------------------------------------

    def dispatch_once(self, lease_seconds: Optional[int] = None) -> Optional[_Dictish]:
        resp = self._post("/dispatch/assign", _drop_none({"lease_seconds": lease_seconds}))
        return _Dictish(resp) if resp else None

    def tick(
        self,
        lease_seconds: Optional[int] = None,
        limit: Optional[int] = None,
        stale_after_seconds: Optional[int] = None,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/dispatch/tick",
                _drop_none(
                    {
                        "lease_seconds": lease_seconds,
                        "limit": limit,
                        "stale_after_seconds": stale_after_seconds,
                    }
                ),
            )
        )

    # -- Messaging (control bus + structured agentbus) ----------------------

    def send_message(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/messages", _drop_none(kw)))

    def deliver_messages(self, agent_id: str, limit: Optional[int] = None) -> List[_Dictish]:
        return _wrap_list(
            self._post(
                "/agents/%s/messages/deliver" % quote(agent_id, safe=""),
                _drop_none({"limit": limit}),
            )
        )

    def open_agentbus_stream(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/agentbus/streams", _drop_none(kw)))

    def append_agentbus_chunk(self, stream_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/agentbus/streams/%s/chunks" % quote(stream_id, safe=""),
                _drop_none(kw),
            )
        )

    def close_agentbus_stream(self, stream_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/agentbus/streams/%s/close" % quote(stream_id, safe=""),
                _drop_none(kw),
            )
        )

    def list_agentbus_streams(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/agentbus/streams", **kw))

    def read_agentbus_chunks(self, stream_id: str, **kw: Any) -> List[_Dictish]:
        return _wrap_list(
            self._get("/agentbus/streams/%s/chunks" % quote(stream_id, safe=""), **kw)
        )

    def publish_agentbus_content(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/agentbus", _drop_none(kw)))

    def publish_agentbus_artifact(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/agentbus/artifact-publish", _drop_none(kw)))

    # -- Review / Publish ---------------------------------------------------

    def request_review(self, task_id: str, reviewer_agent_id: str, actor: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/tasks/%s/reviews" % quote(task_id, safe=""),
                {"reviewer_agent_id": reviewer_agent_id, "actor": actor},
            )
        )

    def submit_review(self, review_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/reviews/%s/decision" % quote(review_id, safe=""),
                _drop_none(kw),
            )
        )

    def publish_task(
        self,
        task_id: str,
        target: str,
        created_by: str,
        *,
        evidence_id: Optional[str] = None,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/publications",
                _drop_none(
                    {
                        "task_id": task_id,
                        "target": target,
                        "created_by": created_by,
                        "evidence_id": evidence_id,
                    }
                ),
            )
        )

    # -- Secret -------------------------------------------------------------

    def create_secret(
        self,
        name: str,
        value: str,
        scopes: Dict[str, Any],
        created_by: str,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/secrets",
                {"name": name, "value": value, "scopes": scopes, "created_by": created_by},
            )
        )

    def rotate_secret(self, name: str, value: str, actor: str = "operator") -> _Dictish:
        return _Dictish(
            self._post(
                "/secrets/%s/rotate" % quote(name, safe=""),
                {"value": value, "actor": actor},
            )
        )

    def list_secrets(self) -> List[_Dictish]:
        return _wrap_list(self._get("/secrets"))

    def request_secret(self, secret: str, agent_id: str, purpose: str) -> _Dictish:
        # The api.py route is /secrets/{secret_id}/access; the cli passes
        # `secret` as the id-or-name (mac.cli.cmd_secret_request).
        return _Dictish(
            self._post(
                "/secrets/%s/access" % quote(secret, safe=""),
                {"agent_id": agent_id, "purpose": purpose},
            )
        )

    def list_secret_audits(self, secret_id: str) -> List[_Dictish]:
        return _wrap_list(self._get("/secret-audits", secret_id=secret_id))

    # -- Runtime / Artifact / Environment / Deployment ----------------------

    def create_runtime(self, name: str, manifest: Dict[str, Any], created_by: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/runtimes",
                {"name": name, "manifest": manifest, "created_by": created_by},
            )
        )

    def list_runtimes(self) -> List[_Dictish]:
        return _wrap_list(self._get("/runtimes"))

    def propose_runtime_delta(
        self,
        task_id: str,
        agent_id: str,
        package_manager: str,
        commands: List[str],
        added_dependencies: List[Any],
        reason: str,
        **kw: Any,
    ) -> _Dictish:
        body = _drop_none(
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "package_manager": package_manager,
                "commands": commands,
                "added_dependencies": added_dependencies,
                "reason": reason,
                **kw,
            }
        )
        return _Dictish(self._post("/runtime-deltas", body))

    def list_runtime_deltas(
        self,
        *,
        status: Optional[str] = None,
        task_id: Optional[str] = None,
        project: Optional[str] = None,
        limit: int = 200,
    ) -> List[_Dictish]:
        return _wrap_list(
            self._get(
                "/runtime-deltas",
                status=status,
                task_id=task_id,
                project=project,
                limit=limit,
            )
        )

    def get_runtime_delta(self, delta: str) -> _Dictish:
        return _Dictish(self._get("/runtime-deltas/%s" % quote(delta, safe="")))

    def validate_runtime_delta(self, delta: str, actor: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/runtime-deltas/%s/validate" % quote(delta, safe=""),
                {"actor": actor},
            )
        )

    def reject_runtime_delta(self, delta: str, actor: str, reason: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/runtime-deltas/%s/reject" % quote(delta, safe=""),
                {"actor": actor, "reason": reason},
            )
        )

    def promote_runtime_delta(
        self,
        delta: str,
        actor: str,
        *,
        runtime_name: Optional[str] = None,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/runtime-deltas/%s/promote" % quote(delta, safe=""),
                _drop_none({"actor": actor, "runtime_name": runtime_name}),
            )
        )

    def register_artifact(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/artifacts", _drop_none(kw)))

    def list_artifacts(self, kind: Optional[str] = None) -> List[_Dictish]:
        return _wrap_list(self._get("/artifacts", kind=kind))

    def get_artifact(self, artifact: str) -> _Dictish:
        return _Dictish(self._get("/artifacts/%s" % quote(artifact, safe="")))

    def delete_artifact(self, artifact: str, actor: Optional[str] = None) -> _Dictish:
        return _Dictish(self._delete("/artifacts/%s" % quote(artifact, safe="")))

    def register_environment(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/environments", _drop_none(kw)))

    def list_environments(
        self,
        tenant_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[_Dictish]:
        return _wrap_list(self._get("/environments", tenant_id=tenant_id, channel=channel))

    def get_environment(self, environment: str) -> _Dictish:
        return _Dictish(self._get("/environments/%s" % quote(environment, safe="")))

    def deploy_artifact(self, environment: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/environments/%s/deploy" % quote(environment, safe=""),
                _drop_none(kw),
            )
        )

    def current_deployment(self, environment: str) -> Optional[_Dictish]:
        resp = self._get("/environments/%s/current" % quote(environment, safe=""))
        return _Dictish(resp) if resp else None

    def list_deployments(self, environment: str) -> List[_Dictish]:
        return _wrap_list(self._get("/environments/%s/deployments" % quote(environment, safe="")))

    # -- Bridge (project items) ---------------------------------------------
    # beads bridge endpoints removed: beads is no longer a read/write source.

    def import_project_item(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/bridge/items", _drop_none(kw)))

    def list_project_items(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/bridge/items", **kw))

    # -- Integrations -------------------------------------------------------

    def list_integration_findings(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/integrations/findings", **kw))

    def record_integration_finding(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/integrations/findings", _drop_none(kw)))

    def list_integration_observations(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/integrations/observations", **kw))

    # -- Memory (note: list/forget remain SQL-direct, handled via store) ----

    def add_memory(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/memory", _drop_none(kw)))

    def search_memory(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/memory", **kw))

    # mem-10: memory-tier health snapshot.
    def memory_health(self, *, nap_interval_hours: float = 24.0) -> _Dictish:
        return _Dictish(
            self._get("/v1/memory/health", nap_interval_hours=nap_interval_hours)
        )

    # mem-09: recall over the vector tier.
    def recall_memory(
        self,
        query: str,
        *,
        tier: str = "medium",
        limit: int = 5,
        min_score: Optional[float] = None,
        project: Optional[str] = None,
        tenant_id: Optional[str] = None,
        **_extra: Any,
    ) -> List[_Dictish]:
        return _wrap_list(
            self._get(
                "/v1/memory/recall",
                q=query,
                tier=tier,
                limit=limit,
                min_score=min_score,
                project=project,
                tenant_id=tenant_id,
            )
        )

    # -- Rollout ------------------------------------------------------------

    def create_rollout(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/rollouts", _drop_none(kw)))

    def list_rollouts(
        self,
        tenant_id: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> List[_Dictish]:
        return _wrap_list(self._get("/rollouts", tenant_id=tenant_id, channel=channel))

    def advance_rollout(
        self,
        rollout_id: str,
        action: str,
        actor: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/rollouts/%s/advance" % quote(rollout_id, safe=""),
                _drop_none({"action": action, "actor": actor, "detail": detail or {}}),
            )
        )

    def verify_rollout_artifact(
        self,
        rollout_id: str,
        artifact_uri: str,
        artifact_hash: str,
        actor: str,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/rollouts/%s/artifact" % quote(rollout_id, safe=""),
                {
                    "artifact_uri": artifact_uri,
                    "artifact_hash": artifact_hash,
                    "actor": actor,
                },
            )
        )

    def evaluate_rollout_health(
        self,
        rollout_id: str,
        checks: Dict[str, Any],
        actor: str,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/rollouts/%s/health" % quote(rollout_id, safe=""),
                {"checks": checks, "actor": actor},
            )
        )

    def rescue_rollout(
        self,
        rollout_id: str,
        actor: str,
        reason: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> tuple:  # type: ignore[type-arg]
        resp = self._post(
            "/rollouts/%s/rescue" % quote(rollout_id, safe=""),
            _drop_none({"actor": actor, "reason": reason, "detail": detail or {}}),
        )
        rollout = resp.get("rollout") if isinstance(resp, dict) else None
        task = resp.get("task") if isinstance(resp, dict) else None
        return _Dictish(rollout or {}), _Dictish(task or {})

    # -- Eval ---------------------------------------------------------------

    def create_eval_set(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/eval-sets", _drop_none(kw)))

    def list_eval_sets(self) -> List[_Dictish]:
        return _wrap_list(self._get("/eval-sets"))

    def get_eval_set(self, eval_set: str) -> _Dictish:
        return _Dictish(self._get("/eval-sets/%s" % quote(eval_set, safe="")))

    def update_eval_set_baseline(self, eval_set: str, baseline_score: float, actor: str) -> _Dictish:
        return _Dictish(
            self._post(
                "/eval-sets/%s/baseline" % quote(eval_set, safe=""),
                {"baseline_score": baseline_score, "actor": actor},
            )
        )

    def record_eval_run(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/eval-runs", _drop_none(kw)))

    def list_eval_runs(
        self,
        eval_set: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> List[_Dictish]:
        return _wrap_list(self._get("/eval-runs", eval_set=eval_set, target_id=target_id))

    # -- Notifier / Observability / Command audit / Events ------------------

    def configure_notifier_channel(self, name: str, channel_type: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/notifier/channels",
                _drop_none({"name": name, "channel_type": channel_type, **kw}),
            )
        )

    def list_notifier_channels(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/notifier/channels", **kw))

    def delete_notifier_channel(self, channel_id_or_name: str) -> _Dictish:
        return _Dictish(
            self._delete("/notifier/channels/%s" % quote(channel_id_or_name, safe=""))
        )

    def deliver_pending_notifications(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/notifier/deliver", _drop_none(kw)))

    def list_events(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/events", **kw))

    def list_command_audit(self, **kw: Any) -> List[_Dictish]:
        agent_id = kw.pop("agent_id", None)
        path = (
            "/agents/%s/command-audit" % quote(agent_id, safe="")
            if agent_id
            else "/command-audit"
        )
        return _wrap_list(self._get(path, **kw))

    def list_observability(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/observability", **kw))

    def prune_observability(
        self,
        older_than: Optional[str] = None,
        keep_last: Optional[int] = None,
    ) -> int:
        # No HTTP endpoint exposes prune. Surface a clear refusal so the
        # operator either uses --db or waits for the matching route.
        raise DispatchError(
            "prune_observability has no HTTP endpoint yet; run `mac --db <path> "
            "observability prune` against the hub's SQLite file via ssh."
        )

    # -- Unknown methods ----------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        raise DispatchError(
            "`mac %s` is not yet supported in hub mode. Pass --db <path> to "
            "run against a local SQLite database, or wait for the matching "
            "hub endpoint to be wrapped in RemoteDispatch." % name
        )


# ---------------------------------------------------------------------------
# Resolution: which dispatch flavor + connection params from args/env/config
# ---------------------------------------------------------------------------


_LOCAL_BANNER_PRINTED = False


def _maybe_print_local_banner(db_path: str) -> None:
    """Print one stderr banner per process when SQLite mode is in use.

    Prevents the original failure mode: silent writes to a private
    mac.db that the fleet never sees.
    """
    global _LOCAL_BANNER_PRINTED
    if _LOCAL_BANNER_PRINTED:
        return
    _LOCAL_BANNER_PRINTED = True
    try:
        resolved = str(Path(db_path).resolve())
    except OSError:
        resolved = db_path
    # Suppressable for tests / scripted users.
    if os.environ.get("MAC_QUIET_LOCAL_BANNER") == "1":
        return
    print("mac: writing LOCAL db at %s" % resolved, file=sys.stderr)


def _resolve_hub_url(args: Any, env: Dict[str, str]) -> Optional[str]:
    explicit = getattr(args, "hub_url", None)
    if explicit:
        return explicit
    for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL", "HGMAC_URL"):
        value = env.get(name)
        if value:
            return value
    fleet = _effective_fleet(args, env)
    if fleet:
        url = _fleet_url_from_yaml(fleet)
        if url:
            return url
    return None


def _resolve_hub_token(args: Any, env: Dict[str, str]) -> Optional[str]:
    explicit = getattr(args, "token", None)
    if explicit:
        return explicit
    fleet = _effective_fleet(args, env)
    token = resolve_env_var("MAC_API_TOKEN", fleet=fleet, env=env)
    if token:
        return token
    # K8s Job pods carry MAC_WORKER_TOKEN (set by the runner); accept it
    # as a fallback so wrappers can call ``mac pull-request open`` etc.
    # without an extra env-export shim.
    return env.get("MAC_WORKER_TOKEN") or None


def _fleets_config_path() -> Path:
    override = os.environ.get("MAC_FLEETS_CONFIG")
    return Path(override) if override else (Path.home() / ".mac" / "fleets.yaml")


def _load_fleets_yaml() -> Dict[str, Any]:
    """Parse ~/.mac/fleets.yaml (or $MAC_FLEETS_CONFIG); {} on any problem."""
    path = _fleets_config_path()
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _fleet_url_from_yaml(fleet: str) -> Optional[str]:
    """Look up hub_url for a named fleet in ~/.mac/fleets.yaml."""
    fleets = _load_fleets_yaml().get("fleets") or {}
    entry = fleets.get(fleet)
    if not isinstance(entry, dict):
        return None
    url = entry.get("hub_url")
    return str(url) if url else None


def _default_fleet_from_yaml() -> Optional[str]:
    """Pick the default fleet when --fleet / $MAC_FLEET are unset.

    A fleet entry in ~/.mac/fleets.yaml may set ``default: true``.
    Resolution:
      * exactly one fleet marked ``default: true`` -> that fleet;
      * none marked but exactly one fleet defined -> that lone fleet;
      * otherwise (none/several marked among multiple fleets) -> None,
        leaving the choice to an explicit --fleet so we never guess.
    """
    fleets = _load_fleets_yaml().get("fleets") or {}
    if not isinstance(fleets, dict) or not fleets:
        return None
    marked = [
        name
        for name, entry in fleets.items()
        if isinstance(entry, dict) and entry.get("default") is True
    ]
    if len(marked) == 1:
        return marked[0]
    if not marked and len(fleets) == 1:
        return next(iter(fleets))
    return None


def _effective_fleet(args: Any, env: Dict[str, str]) -> Optional[str]:
    """The fleet to use: explicit --fleet, then $MAC_FLEET, then the
    fleets.yaml default (the lone fleet, or the one marked ``default: true``)."""
    return getattr(args, "fleet", None) or env.get("MAC_FLEET") or _default_fleet_from_yaml()


def _load_dotenv_into(env: Dict[str, str]) -> None:
    """Merge ~/.mac/.env into the env dict without clobbering live env.

    Uses :func:`mac.fleet_env.parse_env_file` so ``export``-prefixed lines and
    quoted values (e.g. a JSON ``MAC_API_TOKENS=...``) are handled the same way
    a shell would, rather than landing as a bogus ``export MAC_API_TOKEN`` key
    or a value with stray quotes.
    """
    path = Path(os.environ.get("MAC_DEPLOY_ENV_FILE") or (Path.home() / ".mac" / ".env"))
    if not path.is_file():
        return
    try:
        from mac.fleet_env import parse_env_file

        for key, value in parse_env_file(path).items():
            env.setdefault(key, value)
    except Exception:
        return


def resolve_dispatch(args: Any) -> Union[LocalDispatch, RemoteDispatch]:
    """Pick the dispatch flavor based on args + env + config.

    See module docstring for the resolution order. Raises ``SystemExit(2)``
    with a helpful message when nothing is configured.
    """
    # Build a merged env: live process env wins, ~/.mac/.env fills gaps.
    env: Dict[str, str] = dict(os.environ)
    _load_dotenv_into(env)

    # Priority 1: explicit --db beats everything (lets you debug against
    # a local copy even if a hub is configured in your shell).
    db_path = getattr(args, "db", None) or env.get("MAC_DB")
    if db_path:
        from mac.services import ControlPlane
        from mac.store import SQLiteStore

        _maybe_print_local_banner(db_path)
        return LocalDispatch(ControlPlane(SQLiteStore(db_path)))

    # Priority 2-4: hub mode.
    url = _resolve_hub_url(args, env)
    if url:
        token = _resolve_hub_token(args, env)
        client = HubClient(url, token=token)
        return RemoteDispatch(client)

    # Nothing configured.
    print(
        "mac: no hub configured and no --db specified.\n"
        "  Set MAC_API_URL (or pass --hub-url <URL>) to target a hub, or\n"
        "  pass --db <path> to use a local SQLite database.\n"
        "  For a named fleet: --fleet <name> reads ~/.mac/fleets.yaml.\n"
        "  With multiple fleets, mark one `default: true` in fleets.yaml to\n"
        "  make it the flagless default (a lone fleet is the default already).",
        file=sys.stderr,
    )
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Query-string helper
# ---------------------------------------------------------------------------


def _query(values: Dict[str, Any]) -> str:
    filtered = {
        key: _query_value(value)
        for key, value in values.items()
        if value is not None
    }
    if not filtered:
        return ""
    return "?" + urlencode(filtered, doseq=True)


def _query_value(value: Any) -> Any:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


def _drop_none(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
