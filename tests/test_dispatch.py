"""Unit tests for the transport-resolution layer in :mod:`mac.dispatch`."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest


@pytest.fixture(autouse=True)
def _mac_secret_key(monkeypatch):
    """LocalDispatch instantiates ControlPlane which requires MAC_SECRET_KEY."""
    monkeypatch.setenv("MAC_SECRET_KEY", "dispatch-test-key-with-at-least-32-characters")


from mac.dispatch import (
    DispatchError,
    LocalDispatch,
    RemoteDispatch,
    _Dictish,
    _wrap_list,
    resolve_dispatch,
)


# ---------------------------------------------------------------------------
# _Dictish — the wrapper that satisfies cli._print's `.to_dict()` contract
# ---------------------------------------------------------------------------


def test_dictish_to_dict_returns_underlying():
    d = _Dictish({"a": 1, "b": "two"})
    assert d.to_dict() == {"a": 1, "b": "two"}


def test_dictish_supports_dict_access():
    d = _Dictish({"id": "task_1", "state": "open"})
    assert d["id"] == "task_1"
    assert d.get("state") == "open"
    assert d.get("missing", "default") == "default"
    assert "id" in d
    assert "missing" not in d


def test_dictish_handles_empty_payload():
    d = _Dictish(None)  # type: ignore[arg-type]
    assert d.to_dict() == {}


def test_wrap_list_wraps_dicts():
    wrapped = _wrap_list([{"a": 1}, {"b": 2}])
    assert all(isinstance(item, _Dictish) for item in wrapped)
    assert [item.to_dict() for item in wrapped] == [{"a": 1}, {"b": 2}]


def test_wrap_list_handles_none_and_empty():
    assert _wrap_list(None) == []
    assert _wrap_list([]) == []


def test_wrap_list_handles_envelope_wrapper():
    # Some endpoints wrap lists in {"items": [...]} or {"results": [...]}.
    payload = {"items": [{"id": "x"}, {"id": "y"}]}
    wrapped = _wrap_list(payload)
    assert [w.to_dict() for w in wrapped] == [{"id": "x"}, {"id": "y"}]


# ---------------------------------------------------------------------------
# LocalDispatch — pass-through to a ControlPlane stand-in
# ---------------------------------------------------------------------------


class _FakePlane:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, tuple, dict]] = []
        self.store = object()

    def make_task(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("make_task", args, kwargs))
        return "ok"


def test_local_dispatch_forwards_method_calls():
    plane = _FakePlane()
    disp = LocalDispatch(plane)
    result = disp.make_task("hello", priority=1)
    assert result == "ok"
    assert plane.calls == [("make_task", ("hello",), {"priority": 1})]


def test_local_dispatch_exposes_store():
    plane = _FakePlane()
    assert LocalDispatch(plane).store is plane.store


# ---------------------------------------------------------------------------
# RemoteDispatch — refuses direct SQL, errors on unwrapped methods
# ---------------------------------------------------------------------------


class _FakeHttpClient:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        self.calls.append((method, path, body))
        return {"echo": path}


def test_remote_dispatch_refuses_direct_store_access():
    disp = RemoteDispatch(_FakeHttpClient())  # type: ignore[arg-type]
    with pytest.raises(DispatchError, match="direct SQLite access"):
        disp.store.query_all("SELECT 1")


def test_remote_dispatch_errors_on_unwrapped_method():
    disp = RemoteDispatch(_FakeHttpClient())  # type: ignore[arg-type]
    with pytest.raises(DispatchError, match="not yet supported in hub mode"):
        disp.completely_made_up_method(1, 2)


# ---------------------------------------------------------------------------
# resolve_dispatch — argument resolution and the no-silent-fallback rule
# ---------------------------------------------------------------------------


def _ns(**kwargs: Any) -> argparse.Namespace:
    base = dict(db=None, hub_url=None, token=None, fleet=None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_resolve_dispatch_with_explicit_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    db_path = tmp_path / "mac.db"
    args = _ns(db=str(db_path))
    disp = resolve_dispatch(args)
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_with_mac_db_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.setenv("MAC_DB", str(tmp_path / "from_env.db"))
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    disp = resolve_dispatch(_ns())
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_with_explicit_hub_url(monkeypatch):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    # Block ~/.mac/.env from leaking in by pointing the loader at /dev/null.
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    args = _ns(hub_url="http://hub.example:8789", token="t")
    disp = resolve_dispatch(args)
    assert isinstance(disp, RemoteDispatch)


def test_resolve_dispatch_with_mac_api_url_env(monkeypatch):
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_API_URL", "http://hub.example:8789")
    monkeypatch.setenv("MAC_API_TOKEN", "tok")
    disp = resolve_dispatch(_ns())
    assert isinstance(disp, RemoteDispatch)


def test_resolve_dispatch_explicit_db_wins_over_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_API_URL", "http://hub.example:8789")
    monkeypatch.setenv("MAC_QUIET_LOCAL_BANNER", "1")
    db_path = tmp_path / "mac.db"
    disp = resolve_dispatch(_ns(db=str(db_path)))
    assert isinstance(disp, LocalDispatch)


def test_resolve_dispatch_errors_when_nothing_configured(monkeypatch, capsys):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.delenv("HGMAC_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    with pytest.raises(SystemExit) as excinfo:
        resolve_dispatch(_ns())
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "no hub configured" in captured.err
    assert "--db" in captured.err
    assert "MAC_API_URL" in captured.err


def test_resolve_dispatch_emits_local_banner(tmp_path, monkeypatch, capsys):
    # Reset banner-once state so this test sees the message.
    import mac.dispatch as dispatch_mod

    dispatch_mod._LOCAL_BANNER_PRINTED = False
    monkeypatch.delenv("MAC_QUIET_LOCAL_BANNER", raising=False)
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_DB", raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    db_path = tmp_path / "mac.db"
    resolve_dispatch(_ns(db=str(db_path)))
    captured = capsys.readouterr()
    assert "LOCAL db" in captured.err
    assert str(db_path) in captured.err
