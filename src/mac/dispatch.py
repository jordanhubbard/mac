"""Transport abstraction for the `mac` CLI.

The CLI handlers historically called ``_plane(args).method(...)`` against
a directly-instantiated ``ControlPlane(SQLiteStore(args.db))``. That made
``mac`` SQLite-only: it could never talk to a hub, while ``hgmac`` was a
separate HTTP CLI with different verb shapes. This module merges the two.

A ``Dispatch`` is a transport-flavored facade. Two flavors exist:

* ``LocalDispatch`` — a pass-through to an in-process ``ControlPlane``.
* ``RemoteDispatch`` — translates each call to an HTTP request against a
  hub URL using :class:`mac.hgmac.HgMacClient`.

``resolve_dispatch(args)`` decides which flavor to use:

1. ``--db <path>`` (or ``MAC_DB`` env) → local SQLite at that path,
   with a stderr banner so silent local writes can't happen.
2. ``--hub-url URL`` (with optional ``--token``) → remote HTTP.
3. ``MAC_API_URL`` / ``MAC_URL`` / ``MAC_HUB_URL`` env → remote HTTP.
4. ``~/.mac/config.yaml`` ``default_fleet`` + ``~/.mac/fleets.yaml``
   ``hub_url`` → remote HTTP.
5. Nothing configured → error with help text. No silent fallback.

When ``args.fleet`` is set (or ``MAC_FLEET`` env), the token resolution
goes through :func:`mac.fleet_env.resolve` so ``MAC_API_TOKEN__<FLEET>``
takes precedence over the flat ``MAC_API_TOKEN``.

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
from mac.hgmac import HgMacClient, HgMacError
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
            "this command needs direct SQLite access (task ready/search/stats, "
            "memory list/forget). It is not yet served over HTTP. Pass "
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
    :class:`mac.hgmac.HgMacClient` and wraps JSON responses in
    :class:`_Dictish` so the ``_print`` helper's ``to_dict()`` contract
    keeps working.
    """

    def __init__(self, client: HgMacClient) -> None:
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
                "to_state": to_state,
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
    fleet = getattr(args, "fleet", None) or env.get("MAC_FLEET")
    if fleet:
        url = _fleet_url_from_yaml(fleet)
        if url:
            return url
    return None


def _resolve_hub_token(args: Any, env: Dict[str, str]) -> Optional[str]:
    explicit = getattr(args, "token", None)
    if explicit:
        return explicit
    fleet = getattr(args, "fleet", None) or env.get("MAC_FLEET")
    return resolve_env_var("MAC_API_TOKEN", fleet=fleet, env=env)


def _fleet_url_from_yaml(fleet: str) -> Optional[str]:
    """Look up hub_url for a named fleet in ~/.mac/fleets.yaml."""
    candidate = Path(os.environ.get("MAC_FLEETS_CONFIG", "")) if os.environ.get("MAC_FLEETS_CONFIG") else (
        Path.home() / ".mac" / "fleets.yaml"
    )
    if not candidate.is_file():
        return None
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        data = yaml.safe_load(candidate.read_text()) or {}
    except Exception:
        return None
    fleets = (data or {}).get("fleets") or {}
    entry = fleets.get(fleet)
    if not isinstance(entry, dict):
        return None
    url = entry.get("hub_url")
    return str(url) if url else None


def _load_dotenv_into(env: Dict[str, str]) -> None:
    """Merge ~/.mac/.env into the env dict without clobbering live env."""
    path = Path(os.environ.get("MAC_DEPLOY_ENV_FILE") or (Path.home() / ".mac" / ".env"))
    if not path.is_file():
        return
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            env.setdefault(key, value)
    except OSError:
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
        client = HgMacClient(url, token=token)
        return RemoteDispatch(client)

    # Nothing configured.
    print(
        "mac: no hub configured and no --db specified.\n"
        "  Set MAC_API_URL (or pass --hub-url <URL>) to target a hub, or\n"
        "  pass --db <path> to use a local SQLite database.\n"
        "  For a named fleet: --fleet <name> reads ~/.mac/fleets.yaml.",
        file=sys.stderr,
    )
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Query-string helper (mirrors hgmac._query / _query_value)
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
