from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import mac.cli as cli
from mac.api import create_app
from mac.dispatch import DispatchError, LocalDispatch, RemoteDispatch
from mac.landing_service import RepositoryEndpoint
from mac.models import ValidationError
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


class _Result:
    def __init__(self, **value) -> None:
        self.value = value

    def to_dict(self):
        return dict(self.value)


class _CertificationStation:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def prepare(self, batch_id: str, bundle_path: Path, *, actor: str):
        self.calls.append(("prepare", batch_id, str(bundle_path), actor))
        return {"job_id": "wpcjob_surface", "state": "queued"}

    def get(self, job_id: str):
        self.calls.append(("status", job_id))
        return {"job_id": job_id, "state": "queued"}

    def list(self, *, state=None, limit=100):
        del state, limit
        return []

    def validate_repository_contract(self, repository_id: str) -> None:
        del repository_id

    def claim(self, job_id: str, *, owner=None):
        self.calls.append(("claim", job_id, owner))
        return _Result(job_id=job_id, owner=owner or "controller", fence=4)

    def ingest(self, job_id: str, result, *, owner: str, fence: int):
        self.calls.append(("ingest", job_id, result, owner, fence))
        return _Result(job_id=job_id, certification_id="wpcert_surface")

    def run(self, job_id: str, bundle_path: Path, *, owner=None, result_path=None):
        self.calls.append(
            (
                "run",
                job_id,
                str(bundle_path),
                owner,
                str(result_path) if result_path else None,
            )
        )
        return _Result(job_id=job_id, certification_id="wpcert_surface")

    def reject_failed_certification(
        self, batch_id: str, *, certification_id: str, actor: str
    ):
        self.calls.append(("reject", batch_id, certification_id, actor))
        return {"status": "completed", "batch_id": batch_id}


class _LandingStation:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def accept_certification(
        self, batch_id: str, endpoint: RepositoryEndpoint, *, certification_id: str
    ):
        self.calls.append(("accept", batch_id, endpoint, certification_id))
        return _Result(status="accepted", batch_id=batch_id)

    def land(self, batch_id: str, endpoint: RepositoryEndpoint):
        self.calls.append(("land", batch_id, endpoint))
        return _Result(status="landed", batch_id=batch_id)


class _PublicationFinalizer:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def finalize_landed_batch(self, batch_id: str, *, actor: str, receipt_id=None):
        self.calls.append((batch_id, actor, receipt_id))
        return _Result(
            status="completed",
            batch_id=batch_id,
            finalization_id="wppubfin_surface",
        )


def _control_plane():
    store = ephemeral_store()
    control = ControlPlane(store, secret_key="certification-surface-key-value-0001")
    certification = _CertificationStation()
    landing = _LandingStation()
    finalizer = _PublicationFinalizer()
    endpoint = RepositoryEndpoint(
        repository_id="repo_surface",
        remote_url="ssh://git@example.invalid/repo.git",
        display_name="surface",
    )
    control.work_package_certifications = certification
    control.work_package_landing = landing
    control.work_package_publication_finalizer = finalizer
    control._work_package_repository_endpoint = lambda _batch_id: endpoint
    return store, control, certification, landing, finalizer, endpoint


def test_admin_api_exposes_explicit_certification_and_landing_controls() -> None:
    store, control, certification, landing, finalizer, endpoint = _control_plane()
    try:
        client = TestClient(
            create_app(
                control_plane=control,
                auth_tokens={
                    "admin-token": {"scopes": ["admin"]},
                    "write-token": {"scopes": ["write"]},
                },
            )
        )
        admin = {"Authorization": "Bearer admin-token"}
        write = {"Authorization": "Bearer write-token"}

        denied = client.post(
            "/work-package-certification-jobs/wpcjob_surface/run",
            headers=write,
            json={"bundle_path": "/tmp/candidate.bundle"},
        )
        assert denied.status_code == 403

        responses = [
            client.post(
                "/work-package-integration-batches/wpbatch_surface/certification-jobs",
                headers=admin,
                json={"bundle_path": "/tmp/candidate.bundle", "actor": "certifier"},
            ),
            client.get(
                "/work-package-certification-jobs/wpcjob_surface", headers=admin
            ),
            client.post(
                "/work-package-certification-jobs/wpcjob_surface/claim",
                headers=admin,
                json={"owner": "certifier"},
            ),
            client.post(
                "/work-package-certification-jobs/wpcjob_surface/ingest",
                headers=admin,
                json={"result": {"schema": "fixture"}, "owner": "certifier", "fence": 4},
            ),
            client.post(
                "/work-package-certification-jobs/wpcjob_surface/run",
                headers=admin,
                json={
                    "bundle_path": "/tmp/candidate.bundle",
                    "owner": "certifier",
                    "result_path": "/tmp/result.json",
                },
            ),
            client.post(
                "/work-package-integration-batches/wpbatch_surface/reject-failed-certification",
                headers=admin,
                json={"certification_id": "wpcert_surface", "actor": "certifier"},
            ),
            client.post(
                "/work-package-integration-batches/wpbatch_surface/accept-certification",
                headers=admin,
                json={"certification_id": "wpcert_surface"},
            ),
            client.post(
                "/work-package-integration-batches/wpbatch_surface/land",
                headers=admin,
            ),
            client.post(
                "/work-package-integration-batches/wpbatch_surface/finalize-publication",
                headers=admin,
                json={"actor": "finalizer", "receipt_id": "receipt_surface"},
            ),
        ]
        assert all(response.status_code == 200 for response in responses), [
            response.text for response in responses
        ]
        assert certification.calls == [
            ("prepare", "wpbatch_surface", "/tmp/candidate.bundle", "certifier"),
            ("status", "wpcjob_surface"),
            ("claim", "wpcjob_surface", "certifier"),
            ("ingest", "wpcjob_surface", {"schema": "fixture"}, "certifier", 4),
            (
                "run",
                "wpcjob_surface",
                "/tmp/candidate.bundle",
                "certifier",
                "/tmp/result.json",
            ),
            ("reject", "wpbatch_surface", "wpcert_surface", "certifier"),
        ]
        assert landing.calls == [
            ("accept", "wpbatch_surface", endpoint, "wpcert_surface"),
            ("land", "wpbatch_surface", endpoint),
        ]
        assert finalizer.calls == [
            ("wpbatch_surface", "finalizer", "receipt_surface")
        ]
    finally:
        store.close()


def test_landing_endpoint_prefers_secret_free_contract_canonical_remote() -> None:
    class _Store:
        def __init__(self, row):
            self.row = row

        def query_one(self, _sql, _params):
            return self.row

    control = object.__new__(ControlPlane)
    control.store = _Store(
        {
            "repository_id": "repo_surface",
            "source": "ssh://git@stale.invalid/repo.git",
            "metadata": json.dumps(
                {
                    "repository_contract": {
                        "canonical_remote_url": "ssh://git@canonical.invalid/repo.git"
                    }
                }
            ),
            "name": "surface",
            "enabled": 1,
        }
    )
    endpoint = control._work_package_repository_endpoint("batch_surface")
    assert endpoint.remote_url == "ssh://git@canonical.invalid/repo.git"

    control.store.row["metadata"] = json.dumps(
        {
            "repository_contract": {
                "canonical_remote_url": "https://token@canonical.invalid/repo.git"
            }
        }
    )
    with pytest.raises(
        ValidationError, match="work-package canonical remote is invalid"
    ):
        control._work_package_repository_endpoint("batch_surface")


class _RecordingClient:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        return {"ok": True}


def test_remote_dispatch_serializes_certification_and_landing_operations() -> None:
    client = _RecordingClient()
    dispatch = RemoteDispatch(client)
    dispatch.prepare_work_package_certification_job("batch/a", "/tmp/a.bundle", actor="c")
    dispatch.work_package_certification_status("job/a")
    dispatch.claim_work_package_certification_job("job/a", owner="c")
    dispatch.ingest_work_package_certification_result(
        "job/a", {"schema": "result"}, owner="c", fence=4
    )
    dispatch.run_work_package_certification_job(
        "job/a", "/tmp/a.bundle", owner="c", result_path="/tmp/result.json"
    )
    dispatch.reject_failed_work_package_certification("batch/a", "cert/a", actor="c")
    dispatch.accept_work_package_certification("batch/a", "cert/a")
    dispatch.land_work_package("batch/a")
    dispatch.finalize_work_package_publication(
        "batch/a", actor="f", receipt_id="receipt/a"
    )

    assert client.calls == [
        (
            "POST",
            "/work-package-integration-batches/batch%2Fa/certification-jobs",
            {"bundle_path": "/tmp/a.bundle", "actor": "c"},
        ),
        ("GET", "/work-package-certification-jobs/job%2Fa", None),
        ("POST", "/work-package-certification-jobs/job%2Fa/claim", {"owner": "c"}),
        (
            "POST",
            "/work-package-certification-jobs/job%2Fa/ingest",
            {"result": {"schema": "result"}, "owner": "c", "fence": 4},
        ),
        (
            "POST",
            "/work-package-certification-jobs/job%2Fa/run",
            {
                "bundle_path": "/tmp/a.bundle",
                "owner": "c",
                "result_path": "/tmp/result.json",
            },
        ),
        (
            "POST",
            "/work-package-integration-batches/batch%2Fa/reject-failed-certification",
            {"certification_id": "cert/a", "actor": "c"},
        ),
        (
            "POST",
            "/work-package-integration-batches/batch%2Fa/accept-certification",
            {"certification_id": "cert/a"},
        ),
        ("POST", "/work-package-integration-batches/batch%2Fa/land", {}),
        (
            "POST",
            "/work-package-integration-batches/batch%2Fa/finalize-publication",
            {"actor": "f", "receipt_id": "receipt/a"},
        ),
    ]


def test_local_replica_cannot_mutate_certification_or_landing() -> None:
    class _Plane:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: {"ok": True}

    blocked = LocalDispatch(
        _Plane(),
        db_path="/tmp/replica.db",
        local_authority_confirmed=False,
        remote_authority="http://hub:8789",
    )
    for name, args, kwargs in [
        ("prepare_work_package_certification_job", ("batch", "/tmp/a"), {}),
        ("claim_work_package_certification_job", ("job",), {}),
        (
            "ingest_work_package_certification_result",
            ("job", {}),
            {"owner": "c", "fence": 1},
        ),
        ("land_work_package", ("batch",), {}),
        ("finalize_work_package_publication", ("batch",), {}),
    ]:
        try:
            getattr(blocked, name)(*args, **kwargs)
        except DispatchError:
            pass
        else:
            raise AssertionError("replica operation was not authority-gated: %s" % name)


class _CliPlane:
    def __init__(self) -> None:
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"ok": True}

        return call


def test_cli_routes_explicit_certification_and_landing_controls(
    monkeypatch, tmp_path: Path
) -> None:
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps({"schema": "fixture"}), encoding="utf-8")
    plane = _CliPlane()
    outputs = []
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_print", outputs.append)
    parser = cli.build_parser()
    commands = [
        ["work-package", "certification-prepare", "batch", "/tmp/a", "--actor", "c"],
        ["work-package", "certification-status", "job"],
        ["work-package", "certification-claim", "job", "--owner", "c"],
        [
            "work-package",
            "certification-ingest",
            "job",
            "--result-file",
            str(result_path),
            "--owner",
            "c",
            "--fence",
            "4",
        ],
        ["work-package", "certification-run", "job", "/tmp/a", "--owner", "c"],
        ["work-package", "reject-failed-certification", "batch", "cert", "--actor", "c"],
        ["work-package", "accept-certification", "batch", "cert"],
        ["work-package", "land", "batch"],
        [
            "work-package",
            "finalize-publication",
            "batch",
            "--actor",
            "f",
            "--receipt-id",
            "receipt",
        ],
    ]
    for command in commands:
        args = parser.parse_args(command)
        args.func(args)

    assert [item[0] for item in plane.calls] == [
        "prepare_work_package_certification_job",
        "work_package_certification_status",
        "claim_work_package_certification_job",
        "ingest_work_package_certification_result",
        "run_work_package_certification_job",
        "reject_failed_work_package_certification",
        "accept_work_package_certification",
        "land_work_package",
        "finalize_work_package_publication",
    ]
    assert len(outputs) == 9
