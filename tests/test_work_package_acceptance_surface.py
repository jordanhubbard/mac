from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from fastapi.testclient import TestClient

from mac import cli
from mac.api import create_app
from mac.dispatch import DispatchError, LocalDispatch, RemoteDispatch
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


@dataclass(frozen=True)
class _Result:
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload)


class _HttpClient:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.calls.append((method, path, body))
        return {"method": method, "path": path, "body": body}


class _Plane:
    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.store = object()

    def accept_work_package_candidate(self, candidate_id: str, **kwargs: Any) -> _Result:
        self.calls.append(("accept", candidate_id, kwargs))
        return _Result({"candidate_id": candidate_id, "status": "accepted"})

    def reject_work_package_candidate(self, candidate_id: str, **kwargs: Any) -> _Result:
        self.calls.append(("reject", candidate_id, kwargs))
        return _Result({"candidate_id": candidate_id, "status": "rejected"})


def test_admin_candidate_acceptance_and_rejection_api(monkeypatch) -> None:
    store = ephemeral_store()
    try:
        plane = ControlPlane(
            store,
            secret_key="candidate-surface-test-secret-value-0001",
        )
        calls = []

        def accept(candidate_id: str, *, actor: str) -> _Result:
            calls.append(("accept", candidate_id, actor, None))
            return _Result({"candidate_id": candidate_id, "status": "accepted"})

        def reject(candidate_id: str, *, actor: str, reason: str) -> _Result:
            calls.append(("reject", candidate_id, actor, reason))
            return _Result({"candidate_id": candidate_id, "status": "rejected"})

        monkeypatch.setattr(plane, "accept_work_package_candidate", accept)
        monkeypatch.setattr(plane, "reject_work_package_candidate", reject)
        client = TestClient(
            create_app(
                control_plane=plane,
                auth_tokens={
                    "admin-token": {"scopes": ["admin"]},
                    "write-token": {"scopes": ["write"]},
                },
            )
        )

        denied = client.post(
            "/work-packages/candidates/candidate-0/accept",
            headers={"Authorization": "Bearer write-token"},
            json={"actor": "controller"},
        )
        assert denied.status_code == 403
        accepted = client.post(
            "/work-packages/candidates/candidate-1/accept",
            headers={"Authorization": "Bearer admin-token"},
            json={"actor": "controller"},
        )
        rejected = client.post(
            "/work-packages/candidates/candidate-2/reject",
            headers={"Authorization": "Bearer admin-token"},
            json={"actor": "operator", "reason": "failed assembly contract"},
        )

        assert accepted.status_code == 200
        assert accepted.json() == {"candidate_id": "candidate-1", "status": "accepted"}
        assert rejected.status_code == 200
        assert rejected.json() == {"candidate_id": "candidate-2", "status": "rejected"}
        assert calls == [
            ("accept", "candidate-1", "controller", None),
            ("reject", "candidate-2", "operator", "failed assembly contract"),
        ]
    finally:
        store.close()


def test_remote_and_local_dispatch_route_candidate_decisions() -> None:
    client = _HttpClient()
    remote = RemoteDispatch(client)  # type: ignore[arg-type]

    accepted = remote.accept_work_package_candidate(
        "candidate/one",
        actor="controller",
    )
    rejected = remote.reject_work_package_candidate(
        "candidate two",
        actor="operator",
        reason="does not assemble",
    )

    assert accepted.to_dict()["path"].endswith("candidate%2Fone/accept")
    assert rejected.to_dict()["path"].endswith("candidate%20two/reject")
    assert client.calls == [
        (
            "POST",
            "/work-packages/candidates/candidate%2Fone/accept",
            {"actor": "controller"},
        ),
        (
            "POST",
            "/work-packages/candidates/candidate%20two/reject",
            {"actor": "operator", "reason": "does not assemble"},
        ),
    ]

    plane = _Plane()
    local = LocalDispatch(plane)
    local.accept_work_package_candidate("candidate-1", actor="controller")
    local.reject_work_package_candidate(
        "candidate-2",
        actor="operator",
        reason="review rejected",
    )
    assert plane.calls == [
        ("accept", "candidate-1", {"actor": "controller"}),
        (
            "reject",
            "candidate-2",
            {"actor": "operator", "reason": "review rejected"},
        ),
    ]

    unconfirmed = LocalDispatch(
        _Plane(),
        db_path="/home/operator/.mac/mac.db",
        local_authority_confirmed=False,
    )
    try:
        unconfirmed.accept_work_package_candidate("candidate-1", actor="controller")
    except DispatchError:
        pass
    else:  # pragma: no cover - explicit fail-closed contract.
        raise AssertionError("candidate acceptance bypassed local-authority guard")


def test_cli_routes_accept_and_reject_candidate(monkeypatch) -> None:
    plane = _Plane()
    outputs: List[Any] = []
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_print", outputs.append)
    parser = cli.build_parser()

    accept = parser.parse_args(
        [
            "work-package",
            "accept-candidate",
            "candidate-1",
            "--actor",
            "controller",
        ]
    )
    accept.func(accept)
    reject = parser.parse_args(
        [
            "work-package",
            "reject-candidate",
            "candidate-2",
            "--actor",
            "operator",
            "--reason",
            "failed exact review",
        ]
    )
    reject.func(reject)

    assert plane.calls == [
        ("accept", "candidate-1", {"actor": "controller"}),
        (
            "reject",
            "candidate-2",
            {"actor": "operator", "reason": "failed exact review"},
        ),
    ]
    assert [item.to_dict()["status"] for item in outputs] == [
        "accepted",
        "rejected",
    ]
