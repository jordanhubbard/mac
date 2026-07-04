"""Transport abstraction for the `mac` CLI.

The CLI handlers historically called ``_plane(args).method(...)`` against
a directly-instantiated ``ControlPlane(SQLiteStore(args.db))``. That made
``mac`` SQLite-only: it could never talk to a hub. The merged CLI has
two transports, picked by :func:`resolve_dispatch`.

A ``Dispatch`` is a transport-flavored facade. Two flavors exist:

* ``LocalDispatch`` — direct access to an in-process ``ControlPlane`` backed by
  one authoritative SQLite database. It is not an offline hub replica.
* ``RemoteDispatch`` — translates each call to an HTTP request against a
  hub URL using :class:`mac.http_client.HubClient`.

``resolve_dispatch(args)`` decides which flavor to use:

1. ``--db <path>`` (or ``MAC_DB`` env) → direct SQLite authority at that path,
   with a stderr banner so silent isolated writes can't happen. The canonical
   client-side ``~/.mac/mac.db`` requires ``--local-authority`` before any
   task-producing operation; SQLite tasks are never reconciled with a hub.
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
from mac.http_client import HubClient
from mac.models import MACError


class DispatchError(MACError):
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
    """Direct access to one authoritative :class:`mac.services.ControlPlane`.

    Forwarding via ``__getattr__`` keeps the implementation tiny and
    automatically tracks new ControlPlane methods. A database under a client
    home directory is not a cache: task-producing calls are guarded until the
    operator confirms that its API, dispatcher, and workers use this same
    database as their authority.
    """

    _TASK_PRODUCING_METHODS = frozenset(
        {
            "convert_ticketing_source",
            "create_interaction_task",
            "create_task",
            "evaluate_rollout_health",
            "import_project_item",
            "onboard_repository",
            "rescue_rollout",
            "start_workflow",
        }
    )

    def __init__(
        self,
        plane: Any,
        *,
        db_path: Optional[str] = None,
        local_authority_confirmed: bool = True,
        remote_authority: Optional[str] = None,
    ) -> None:
        self._plane = plane
        self._db_path = db_path
        self._local_authority_confirmed = local_authority_confirmed
        self._remote_authority = remote_authority

    @staticmethod
    def _require_hub_reconciler(*_args: Any, **_kwargs: Any) -> Any:
        raise DispatchError(
            "repository-ref reconciler status and triggers belong to the running hub; "
            "target a hub URL instead of --db"
        )

    repository_ref_reconciler_status = _require_hub_reconciler
    reconcile_repository_refs = _require_hub_reconciler

    def _require_task_authority(self, operation: str) -> None:
        if self._local_authority_confirmed:
            return
        raise _task_authority_error(
            self._db_path or "the selected SQLite database",
            operation,
            self._remote_authority,
        )

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._plane, name)
        if name not in self._TASK_PRODUCING_METHODS:
            return target

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if name != "convert_ticketing_source" or not kwargs.get("dry_run"):
                self._require_task_authority(name.replace("_", " "))
            return target(*args, **kwargs)

        return guarded

    @property
    def store(self) -> Any:
        # Some CLI handlers (task ready/search/stats)
        # reach into ControlPlane.store for direct SQL. Expose it.
        return self._plane.store


# ---------------------------------------------------------------------------
# Remote dispatch — HTTP to a hub
# ---------------------------------------------------------------------------


class _RemoteStore:
    """Stand-in for ``ControlPlane.store`` in remote mode.

    Several CLI handlers reach into ``.store.query_all`` / ``.store.execute``
    to run direct SQL (task ready/search/stats). Those
    can't be served over HTTP without dedicated routes. Until those routes
    land, raising here gives the user a clear next step instead of a
    confusing AttributeError.
    """

    def _refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise DispatchError(
            "this command needs direct SQLite access (observability prune). "
            "It is not yet served over HTTP. Pass "
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

    def assign_review_experiment(
        self,
        task_id: str,
        *,
        experiment_id: str,
        arm: Optional[str] = None,
        arms: Optional[Dict[str, Any]] = None,
        assignment_probability: Optional[float] = None,
        blind: bool = False,
        blind_arms: Optional[List[str]] = None,
        policy_version: str = "v1",
        hypothesis: str = "",
        stratum: str = "",
        actor: str = "human",
    ) -> _Dictish:
        body = _drop_none(
            {
                "experiment_id": experiment_id,
                "arm": arm,
                "arms": arms,
                "assignment_probability": assignment_probability,
                "blind": blind,
                "blind_arms": blind_arms or None,
                "policy_version": policy_version,
                "hypothesis": hypothesis,
                "stratum": stratum,
                "actor": actor,
            }
        )
        return _Dictish(
            self._post(
                "/tasks/%s/review-experiment" % quote(task_id, safe=""),
                body,
            )
        )

    def review_observation(self, task_id: str) -> _Dictish:
        return _Dictish(
            self._get(
                "/tasks/%s/review-observation" % quote(task_id, safe="")
            )
        )

    def record_review_outcome(
        self,
        task_id: str,
        *,
        kind: str,
        status: str,
        finding_id: str = "",
        severity_weight: float = 1.0,
        source: str = "operator",
        detail: Optional[Dict[str, Any]] = None,
        actor: str = "human",
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/tasks/%s/review-outcomes" % quote(task_id, safe=""),
                {
                    "kind": kind,
                    "status": status,
                    "finding_id": finding_id,
                    "severity_weight": severity_weight,
                    "source": source,
                    "detail": detail or {},
                    "actor": actor,
                },
            )
        )

    def review_experiment_report(
        self,
        experiment_id: str,
        *,
        project: Optional[str] = None,
        min_tasks_per_arm: int = 5,
        min_validated_outcomes_per_arm: int = 3,
    ) -> _Dictish:
        return _Dictish(
            self._get(
                "/review-experiments/%s" % quote(experiment_id, safe=""),
                project=project,
                min_tasks_per_arm=min_tasks_per_arm,
                min_validated_outcomes_per_arm=min_validated_outcomes_per_arm,
            )
        )

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        lease_seconds: Optional[int] = None,
        sync_beads: Optional[bool] = None,
    ) -> tuple:  # type: ignore[type-arg]
        # The hub endpoint models these as query parameters. ``sync_beads`` is
        # intentionally local-only; remote claims always use the hub's
        # one-way-migration-safe behavior.
        path = "/tasks/%s/claim" % quote(task_id, safe="")
        path += _query(
            {
                "agent_id": agent_id,
                "lease_seconds": lease_seconds,
            }
        )
        resp = self._post(path)
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

    def reopen_task(
        self,
        task_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none({"actor": actor, "reason": reason})
        return _Dictish(self._post("/tasks/%s/reopen" % quote(task_id, safe=""), body))

    def force_complete_task(
        self,
        task_id: str,
        actor: str,
        reason: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none({"actor": actor, "reason": reason})
        return _Dictish(self._post("/tasks/%s/force-complete" % quote(task_id, safe=""), body))

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

    def release_task(self, task_id: str, *, actor: Optional[str] = None) -> _Dictish:
        return _Dictish(
            self._post(
                "/tasks/%s/release" % quote(task_id, safe=""),
                _drop_none({"actor": actor}),
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
        artifacts: Optional[List[Dict[str, Any]]] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "kind": kind,
                "uri": uri,
                "summary": summary,
                "created_by": created_by,
                "checksum": checksum,
                "metadata": metadata,
                "artifacts": artifacts,
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
        dispatch_paused: Optional[bool] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "name": name,
                "description": description,
                "metadata": metadata,
                "status": status,
                "actor": actor,
                "project_id": project_id,
                "dispatch_paused": dispatch_paused,
            }
        )
        return _Dictish(self._post("/projects", body))

    def set_project_dispatch(
        self,
        name_or_id: str,
        *,
        paused: bool,
        actor: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none({"paused": bool(paused), "actor": actor})
        return _Dictish(
            self._post(
                "/projects/%s/dispatch" % quote(name_or_id, safe=""),
                body,
            )
        )

    def get_project(self, project: str) -> _Dictish:
        return _Dictish(self._get("/projects/%s" % quote(project, safe="")))

    def update_project(
        self,
        name_or_id: str,
        *,
        metadata: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "metadata": metadata,
                "description": description,
                "status": status,
                "actor": actor,
            }
        )
        return _Dictish(
            self._put("/projects/%s" % quote(name_or_id, safe=""), body)
        )

    def github_ingest_status(self) -> _Dictish:
        return _Dictish(self._get("/github-ingest/status"))

    def github_ingest_run(self) -> _Dictish:
        return _Dictish(self._post("/github-ingest/run", {}))

    def backlog_groom_status(self) -> _Dictish:
        return _Dictish(self._get("/backlog-groom/status"))

    def backlog_groom_run(self) -> _Dictish:
        return _Dictish(self._post("/backlog-groom/run", {}))

    def model_selection_status(self) -> _Dictish:
        return _Dictish(self._get("/model-selection/status"))

    def model_selection_refresh(self) -> _Dictish:
        return _Dictish(self._post("/model-selection/refresh", {}))

    def model_selection_promote(self) -> _Dictish:
        return _Dictish(self._post("/model-selection/promote", {}))

    def onboard_repository(
        self,
        repository_url: str,
        *,
        project: Optional[str] = None,
        default_branch: Optional[str] = None,
        title: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Optional[List[str]] = None,
        actor: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "repository_url": repository_url,
                "project": project,
                "default_branch": default_branch,
                "title": title,
                "priority": priority,
                "required_capabilities": required_capabilities,
                "actor": actor,
            }
        )
        return _Dictish(self._post("/repositories/onboard", body))

    def register_project_repository(
        self,
        name: str,
        path: str,
        *,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[Dict[str, Any]] = None,
        actor: Optional[str] = None,
    ) -> _Dictish:
        body = _drop_none(
            {
                "name": name,
                "path": path,
                "source": source,
                "project": project,
                "required_capabilities": list(required_capabilities or []),
                "enabled": enabled,
                "poll_interval_seconds": poll_interval_seconds,
                "metadata": metadata,
                "actor": actor,
            }
        )
        return _Dictish(self._post("/bridge/repositories", body))

    def list_project_repositories(
        self,
        *,
        enabled: Optional[bool] = None,
    ) -> List[_Dictish]:
        return _wrap_list(self._get("/bridge/repositories", enabled=enabled))

    def repository_ref_reconciler_status(self) -> _Dictish:
        return _Dictish(self._get("/repository-refs/reconciler"))

    def reconcile_repository_refs(
        self,
        *,
        mode: Optional[str] = None,
        actor: str = "operator",
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/repository-refs/reconcile",
                _drop_none({"mode": mode, "actor": actor}),
            )
        )

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

    def update_agent(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._put(
                "/agents/%s" % quote(agent_id, safe=""),
                _drop_none(kw),
            )
        )

    def list_agents(self) -> List[_Dictish]:
        return _wrap_list(self._get("/agents"))

    def list_personas(self) -> List[_Dictish]:
        return _wrap_list(self._get("/personas"))

    def delete_agent(self, agent_id: str, *, actor: str = "human") -> _Dictish:
        # DELETE /agents/{id} removes the agent + its agent-scoped ephemera
        # (mood/nap/events/messages) and records an agent.deleted audit event;
        # task history is task-keyed and preserved. Refused if it holds a lease.
        path = "/agents/%s?actor=%s" % (quote(agent_id, safe=""), quote(actor, safe=""))
        return _Dictish(self._delete(path))

    def heartbeat_agent(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post("/agents/%s/heartbeat" % quote(agent_id, safe=""), _drop_none(kw))
        )

    def fleet_build_distribution(self) -> _Dictish:
        return _Dictish(self._get("/fleet/build-distribution"))

    def fleet_snapshot(
        self,
        *,
        exclude_agent_id: Optional[str] = None,
        limit: int = 30,
    ) -> _Dictish:
        return _Dictish(
            self._get(
                "/fleet/snapshot",
                exclude_agent_id=exclude_agent_id,
                limit=limit,
            )
        )

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

    def list_due_nap_agents(self, *, as_of: Optional[str] = None) -> List[_Dictish]:
        return _wrap_list(self._get("/nap-due", as_of=as_of))

    def run_nap_cycle(
        self,
        agent_id: str,
        *,
        actor: Optional[str] = None,
        vector_writer: Any = None,
        embed_into_medium: bool = True,
        emit_dream_artifacts: bool = True,
        qdrant_url: Optional[str] = None,
    ) -> _Dictish:
        if vector_writer is not None:
            raise DispatchError("hub mode builds the nap vector writer on the hub")
        body = _drop_none(
            {
                "actor": actor,
                "embed_into_medium": embed_into_medium,
                "emit_dream_artifacts": emit_dream_artifacts,
                "qdrant_url": qdrant_url,
            }
        )
        return _Dictish(
            self._post("/agents/%s/nap-cycle" % quote(agent_id, safe=""), body)
        )

    def consolidate_nap(
        self,
        agent_id: str,
        *,
        since: Optional[str] = None,
        nap_run_id: Optional[str] = None,
        embed_into_medium: bool = True,
        emit_dream_artifacts: bool = True,
        vector_writer: Any = None,
        created_by: Optional[str] = None,
        qdrant_url: Optional[str] = None,
    ) -> _Dictish:
        if vector_writer is not None:
            raise DispatchError("hub mode builds the nap vector writer on the hub")
        body = _drop_none(
            {
                "since": since,
                "nap_run_id": nap_run_id,
                "embed_into_medium": embed_into_medium,
                "emit_dream_artifacts": emit_dream_artifacts,
                "created_by": created_by,
                "qdrant_url": qdrant_url,
            }
        )
        return _Dictish(
            self._post("/agents/%s/nap-consolidate" % quote(agent_id, safe=""), body)
        )

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

    def read_agentbus_chunks(
        self,
        agent_id: str,
        stream_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> List[_Dictish]:
        # Match ControlPlane.read_agentbus_chunks(agent_id, stream_id, ...) and the
        # GET /agentbus/streams/{stream_id}/chunks endpoint (agent_id is a query
        # param). The old (stream_id, **kw) signature dropped agent_id and broke
        # `mac agentbus read` in hub mode with a positional-arg TypeError.
        return _wrap_list(
            self._get(
                "/agentbus/streams/%s/chunks" % quote(stream_id, safe=""),
                agent_id=agent_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        )

    def publish_agentbus_content(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/agentbus", _drop_none(kw)))

    def publish_agentbus_repo_update(
        self,
        *,
        sender_agent_id: str,
        recipient_agent_ids: Optional[List[str]] = None,
        all_agents: bool = False,
        repo_path: Optional[str] = None,
        remote: str = "origin",
        branch: str = "main",
        restart: bool = True,
        restart_services: Optional[List[str]] = None,
        request_id: Optional[str] = None,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/agentbus/repo-update",
                _drop_none(
                    {
                        "sender_agent_id": sender_agent_id,
                        "recipient_agent_ids": list(recipient_agent_ids or []),
                        "all_agents": all_agents,
                        "repo_path": repo_path,
                        "remote": remote,
                        "branch": branch,
                        "restart": restart,
                        "restart_services": list(restart_services or [])
                        if restart_services
                        else None,
                        "request_id": request_id,
                    }
                ),
            )
        )

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
                {"accessor_agent_id": agent_id, "purpose": purpose},
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

    # -- Memory -------------------------------------------------------------

    def add_memory(self, *args: Any, **kw: Any) -> _Dictish:
        if args:
            names = (
                "task_id",
                "subject_type",
                "subject_id",
                "record_type",
                "content",
                "evidence_id",
                "created_by",
            )
            if len(args) > len(names):
                raise TypeError(
                    "add_memory expected at most %d positional arguments" % len(names)
                )
            kw = {**dict(zip(names, args)), **kw}
        return _Dictish(self._post("/memory", _drop_none(kw)))

    def search_memory(self, *args: Any, **kw: Any) -> List[_Dictish]:
        if args:
            names = ("task_id", "subject_type", "subject_id")
            if len(args) > len(names):
                raise TypeError(
                    "search_memory expected at most %d positional arguments" % len(names)
                )
            kw = {**dict(zip(names, args)), **kw}
        return _wrap_list(self._get("/memory", **kw))

    def remember_memory(
        self,
        key: str,
        content: str,
        *,
        project: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> _Dictish:
        return _Dictish(
            self._post(
                "/memory/remembered",
                _drop_none(
                    {
                        "key": key,
                        "content": content,
                        "project": project,
                        "actor": actor,
                    }
                ),
            )
        )

    def list_remembered_memory(self, *, project: Optional[str] = None) -> List[_Dictish]:
        return _wrap_list(self._get("/memory/remembered", project=project))

    def forget_memory(self, key: str, *, project: Optional[str] = None) -> _Dictish:
        return _Dictish(
            self._delete(
                "/memory/remembered/%s%s"
                % (quote(key, safe=""), _query({"project": project}))
            )
        )

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

    def recall_dream_artifacts(
        self,
        query: str,
        *,
        tier: str = "medium",
        limit: int = 5,
        min_score: Optional[float] = None,
        project: Optional[str] = None,
        agent_id: Optional[str] = None,
        scope: Optional[str] = None,
        kind: Optional[str] = None,
        min_confidence: Optional[str] = None,
        tenant_id: Optional[str] = None,
        **_extra: Any,
    ) -> List[_Dictish]:
        return _wrap_list(
            self._get(
                "/v1/memory/dreams/recall",
                q=query,
                tier=tier,
                limit=limit,
                min_score=min_score,
                project=project,
                agent_id=agent_id,
                scope=scope,
                kind=kind,
                min_confidence=min_confidence,
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

    def create_openshell_policy(self, *args: Any, **kw: Any) -> _Dictish:
        name = args[0] if args else kw.pop("name")
        policy_text = args[1] if len(args) > 1 else kw.pop("policy_text")
        return _Dictish(
            self._post(
                "/openshell/policies",
                _drop_none({"name": name, "policy_text": policy_text, **kw}),
            )
        )

    def list_openshell_policies(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/openshell/policies", **kw))

    def get_openshell_policy(self, policy_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._get("/openshell/policies/%s" % quote(policy_id, safe=""), **kw)
        )

    def update_openshell_policy(self, policy_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._put(
                "/openshell/policies/%s" % quote(policy_id, safe=""),
                _drop_none(kw),
            )
        )

    def delete_openshell_policy(self, policy_id: str, **kw: Any) -> _Dictish:
        actor = kw.get("actor")
        path = "/openshell/policies/%s" % quote(policy_id, safe="")
        if actor:
            path += _query({"actor": actor})
        return _Dictish(self._delete(path))

    def render_openshell_policy(self, policy_id: str, **kw: Any) -> Dict[str, Any]:
        return self._post(
            "/openshell/policies/%s/render" % quote(policy_id, safe=""),
            _drop_none(kw),
        )

    def list_openshell_policy_versions(self, policy_id: str) -> List[_Dictish]:
        return _wrap_list(
            self._get("/openshell/policies/%s/versions" % quote(policy_id, safe=""))
        )

    def assign_openshell_policy(self, policy_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/openshell/policies/%s/assignments" % quote(policy_id, safe=""),
                _drop_none(kw),
            )
        )

    def list_openshell_policy_assignments(self, **kw: Any) -> List[_Dictish]:
        policy_id = kw.pop("policy_id", None)
        if policy_id:
            return _wrap_list(
                self._get(
                    "/openshell/policies/%s/assignments" % quote(policy_id, safe="")
                )
            )
        raise DispatchError("listing all OpenShell assignments is local-only for now")

    def get_openshell_status(self, agent_id: str) -> Dict[str, Any]:
        return self._get("/agents/%s/openshell/status" % quote(agent_id, safe=""))

    def report_openshell_status(self, agent_id: str, **kw: Any) -> _Dictish:
        return _Dictish(
            self._post(
                "/agents/%s/openshell/status" % quote(agent_id, safe=""),
                _drop_none(kw),
            )
        )

    def record_action_event(self, **kw: Any) -> _Dictish:
        return _Dictish(self._post("/action-events", _drop_none(kw)))

    def list_action_events(self, **kw: Any) -> List[_Dictish]:
        return _wrap_list(self._get("/action-events", **kw))

    def export_action_events_otlp(self, **kw: Any) -> Dict[str, Any]:
        return self._get("/action-events/export/otlp", **kw)

    def summarize_actions_to_memory(self, **kw: Any) -> Dict[str, Any]:
        return self._post("/memory/summarize-actions", _drop_none(kw))

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
    print(
        "mac: using DIRECT SQLite authority at %s; it does not synchronize "
        "tasks with any hub" % resolved,
        file=sys.stderr,
    )


def _canonical_client_db_path() -> Path:
    return (Path.home() / ".mac" / "mac.db").expanduser().resolve()


def _is_canonical_client_db(db_path: str) -> bool:
    try:
        return Path(db_path).expanduser().resolve() == _canonical_client_db_path()
    except OSError:
        return False


def _configured_remote_authority(args: Any, env: Dict[str, str]) -> Optional[str]:
    """Describe a configured remote authority without opening a login tunnel."""

    profile = getattr(args, "profile", None) or env.get("MAC_PROFILE")
    if not profile:
        try:
            from mac.client_profiles import active_profile_name

            profile = active_profile_name()
        except Exception:  # noqa: BLE001 - this is only an error-message hint.
            profile = None
    if profile:
        return "client profile %r" % profile

    explicit_url = getattr(args, "hub_url", None)
    if explicit_url:
        return "the explicitly selected hub"
    if any(env.get(name) for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL", "HGMAC_URL")):
        return "a hub URL from the environment"

    fleet = _effective_fleet(args, env)
    if fleet:
        return "fleet %r" % fleet
    return None


def _task_authority_error(
    db_path: str,
    operation: str,
    remote_authority: Optional[str],
) -> DispatchError:
    message = (
        "refusing %s against %s. This is a direct SQLite control-plane "
        "authority, not a repository ticket store or an offline hub replica; "
        "its tasks are never uploaded or reconciled with a hub. " % (operation, db_path)
    )
    if remote_authority:
        message += (
            "%s is configured for fleet work. Omit --db and target that "
            "authority (run `mac login` first if it has no client profile). "
            % remote_authority
        )
    else:
        message += "Target a configured hub for fleet work. "
    message += (
        "If this SQLite file is intentionally the authoritative database used "
        "by the MAC API, dispatcher, and workers, rerun with --local-authority."
    )
    return DispatchError(message)


def _task_producing_cli_operation(args: Any) -> Optional[str]:
    """Return a user-facing operation name when this CLI call can create tasks."""

    command = getattr(args, "command", None)
    if command == "interaction" and getattr(args, "interaction_command", None) == "task":
        return "interaction task creation"
    if command == "project" and getattr(args, "project_command", None) == "onboard":
        return "project onboarding"
    if command == "bridge" and getattr(args, "bridge_command", None) == "import":
        return "bridge task import"
    if command == "workflow" and getattr(args, "workflow_command", None) == "start":
        return "workflow start"
    if command == "rollout" and getattr(args, "rollout_command", None) in {"health", "rescue"}:
        return "rollout %s" % getattr(args, "rollout_command")
    if command == "migrate":
        migrate_command = getattr(args, "migrate_command", None)
        if migrate_command == "import":
            return "task migration import"
        if migrate_command == "acc" and getattr(args, "mode", "dry-run") == "import":
            return "ACC task migration"
        if migrate_command == "local-ledger" and getattr(args, "execute", False):
            return "local ledger migration"
    if command != "task":
        return None
    task_command = getattr(args, "task_command", None)
    if task_command == "create":
        return "task creation"
    if task_command == "migrate-beads" and not (
        getattr(args, "dry_run", False) or getattr(args, "tickets_only", False)
    ):
        return "beads task migration"
    if task_command == "convert-ticketing" and not getattr(args, "dry_run", False):
        return "ticket conversion"
    return None


def _resolve_hub_url(args: Any, env: Dict[str, str]) -> Optional[str]:
    explicit = getattr(args, "hub_url", None)
    if explicit:
        return explicit
    profile = _resolve_client_profile(args, env)
    if profile:
        value = (profile.get("connection") or {}).get("api_url")
        if value:
            return str(value)
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
    profile = _resolve_client_profile(args, env)
    if profile:
        value = (profile.get("credential") or {}).get("token")
        if value:
            return str(value)
    fleet = _effective_fleet(args, env)
    token = resolve_env_var("MAC_API_TOKEN", fleet=fleet, env=env)
    if token:
        return token
    # K8s Job pods carry MAC_WORKER_TOKEN (set by the runner); accept it
    # as a fallback so wrappers can call ``mac pull-request open`` etc.
    # without an extra env-export shim.
    return env.get("MAC_WORKER_TOKEN") or None


def _resolve_client_profile(args: Any, env: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Load the selected secure client profile once per parsed argument set."""

    cached = getattr(args, "_mac_client_profile", None)
    if cached is False:
        return None
    if isinstance(cached, dict):
        return cached
    explicit = getattr(args, "profile", None) or env.get("MAC_PROFILE")
    fleet = _effective_fleet(args, env)
    try:
        from mac.client_profiles import ClientProfileError, list_profiles, load_profile

        selected = explicit
        if not selected and fleet:
            candidates = [item for item in list_profiles() if item.get("fleet") == fleet]
            if len(candidates) == 1:
                selected = str(candidates[0]["profile"])
        profile = load_profile(selected, include_token=True)
        if fleet and not explicit and profile.get("fleet") not in (None, "", fleet):
            setattr(args, "_mac_client_profile", False)
            return None
        if (profile.get("connection") or {}).get("mode") == "ssh-tunnel":
            try:
                from mac.client_login import ClientLoginError, ensure_session

                ensure_session(str(profile.get("profile") or selected or ""))
            except ClientLoginError as exc:
                raise DispatchError(
                    "could not establish client login tunnel for %r: %s"
                    % (profile.get("profile") or selected, exc)
                ) from exc
        setattr(args, "_mac_client_profile", profile)
        return profile
    except (FileNotFoundError, ClientProfileError) as exc:
        if explicit:
            raise DispatchError("could not load client profile %r: %s" % (explicit, exc)) from exc
        setattr(args, "_mac_client_profile", False)
        return None


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
    local_authority = bool(getattr(args, "local_authority", False))
    explicit_remote = [
        name
        for name, value in (
            ("--hub-url", getattr(args, "hub_url", None)),
            ("--fleet", getattr(args, "fleet", None)),
            ("--profile", getattr(args, "profile", None)),
        )
        if value
    ]
    if db_path and explicit_remote:
        raise DispatchError(
            "conflicting control-plane authorities: --db selects direct SQLite, "
            "while %s selects a remote hub. Choose exactly one."
            % ", ".join(explicit_remote)
        )
    if local_authority and not db_path:
        raise DispatchError("--local-authority requires --db or MAC_DB")
    if db_path:
        from mac.services import ControlPlane
        from mac.store import SQLiteStore

        _maybe_print_local_banner(db_path)
        canonical_client_db = _is_canonical_client_db(db_path)
        resolved_db_path = str(Path(db_path).expanduser().resolve())
        remote_authority = _configured_remote_authority(args, env)
        operation = _task_producing_cli_operation(args)
        if canonical_client_db and not local_authority and operation:
            raise _task_authority_error(resolved_db_path, operation, remote_authority)
        return LocalDispatch(
            ControlPlane(SQLiteStore(db_path)),
            db_path=resolved_db_path,
            local_authority_confirmed=local_authority or not canonical_client_db,
            remote_authority=remote_authority,
        )

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
        "  A secure profile can be selected with --profile <name>.\n"
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
