from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "fleet-prerequisite-receipts.py"
SPEC = importlib.util.spec_from_file_location("fleet_prerequisite_receipts", HELPER)
assert SPEC and SPEC.loader
receipts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(receipts)

IDENTITY = "1" * 64
AGENT = "agent_example"


def private_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def path_contract(path: Path, *, participant: str = "machine-onboarding") -> dict:
    return {
        "schema": receipts.CONTRACT_SCHEMA,
        "participant": participant,
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
        "checks": [
            {
                "name": "pinned-tool",
                "kind": "path",
                "path": str(path),
                "file_type": "executable",
                "expected_mode": 0o700,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        ],
    }


def ready_receipt(participant: str, *, observed: float = 1000.0) -> dict:
    return {
        "schema": receipts.RECEIPT_SCHEMA,
        "participant": participant,
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
        "contract_sha256": hashlib.sha256(participant.encode()).hexdigest(),
        "observed_at_epoch": observed,
        "status": "ready",
        "checks": [
            {
                "name": "ready",
                "kind": "path",
                "evidence_sha256": hashlib.sha256(
                    (participant + " evidence").encode()
                ).hexdigest(),
            }
        ],
    }


def complete_receipts(*, observed: float = 1000.0) -> list[dict]:
    return [
        ready_receipt(participant, observed=observed)
        for participant in receipts.REQUIRED_PARTICIPANTS
    ]


def expectations() -> dict:
    return {
        "schema": receipts.EXPECTATIONS_SCHEMA,
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
        "contracts": {
            receipt["participant"]: receipt["contract_sha256"]
            for receipt in complete_receipts()
        },
    }


def test_path_verifier_seals_only_secret_free_digests(tmp_path: Path) -> None:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o700)
    contract = path_contract(tool)

    receipt = receipts.verify_contract(contract, now=1000.0)

    assert receipt["participant"] == "machine-onboarding"
    assert receipt["status"] == "ready"
    assert receipt["contract_sha256"] == receipts._sha256(contract)
    assert receipt["checks"][0]["kind"] == "path"
    encoded = json.dumps(receipt, sort_keys=True)
    assert str(tool) not in encoded
    assert "#!/bin/sh" not in encoded


@pytest.mark.parametrize("change", ["mode", "digest", "symlink"])
def test_path_verifier_rejects_drift_and_indirection(
    tmp_path: Path, change: str
) -> None:
    tool = tmp_path / "tool"
    tool.write_text("content", encoding="utf-8")
    tool.chmod(0o700)
    contract = path_contract(tool)
    if change == "mode":
        tool.chmod(0o755)
    elif change == "digest":
        tool.write_text("changed", encoding="utf-8")
    else:
        real = tmp_path / "real"
        tool.rename(real)
        tool.symlink_to(real)

    with pytest.raises(receipts.PrerequisiteError):
        receipts.verify_contract(contract, now=1000.0)


def test_path_verifier_preserves_setgid_directory_mode(tmp_path: Path) -> None:
    shared = tmp_path / "shared-runtime"
    shared.mkdir()
    shared.chmod(0o2755)
    contract = {
        "schema": receipts.CONTRACT_SCHEMA,
        "participant": "service-topology",
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
        "checks": [
            {
                "name": "shared-runtime",
                "kind": "path",
                "path": str(shared),
                "file_type": "directory",
                "expected_mode": 0o2755,
                "sha256": None,
            }
        ],
    }

    receipt = receipts.verify_contract(contract, now=1000.0)

    assert receipt["status"] == "ready"


@pytest.mark.parametrize("file_type", ["file", "executable"])
def test_path_verifier_rejects_privileged_regular_file_mode(
    tmp_path: Path, file_type: str
) -> None:
    tool = tmp_path / "tool"
    tool.write_text("content", encoding="utf-8")
    contract = path_contract(tool)
    contract["checks"][0]["file_type"] = file_type
    contract["checks"][0]["expected_mode"] = 0o4755

    with pytest.raises(receipts.PrerequisiteError, match="expected_mode"):
        receipts._validate_contract(contract)


def test_contract_rejects_commands_and_secret_bearing_or_remote_urls() -> None:
    base = {
        "schema": receipts.CONTRACT_SCHEMA,
        "participant": "qdrant",
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
    }
    command = {
        **base,
        "checks": [{"name": "bad", "kind": "command", "argv": ["true"]}],
    }
    with pytest.raises(receipts.PrerequisiteError, match="unsupported"):
        receipts._validate_contract(command)

    for url in (
        "http://user:secret@127.0.0.1:6333/health",
        "http://127.0.0.1:6333/health?token=secret",
        "https://127.0.0.1:6333/health",
        "http://example.com:6333/health",
    ):
        contract = {
            **base,
            "checks": [
                {
                    "name": "health",
                    "kind": "http",
                    "url": url,
                    "method": "GET",
                    "expected_status": [200],
                    "body_sha256": None,
                    "timeout_seconds": 1,
                }
            ],
        }
        with pytest.raises(receipts.PrerequisiteError):
            receipts._validate_contract(contract)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"status":"ready"}\n'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_http_and_tcp_adapters_are_loopback_read_only() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        contract = {
            "schema": receipts.CONTRACT_SCHEMA,
            "participant": "qdrant",
            "agent_id": AGENT,
            "node_identity_sha256": IDENTITY,
            "checks": [
                {
                    "name": "tcp",
                    "kind": "tcp",
                    "host": "127.0.0.1",
                    "port": port,
                    "timeout_seconds": 1,
                    "network_scope": "loopback",
                },
                {
                    "name": "health",
                    "kind": "http",
                    "url": f"http://127.0.0.1:{port}/health",
                    "method": "GET",
                    "expected_status": [200],
                    "body_sha256": hashlib.sha256(b'{"status":"ready"}\n').hexdigest(),
                    "timeout_seconds": 1,
                },
            ],
        }
        receipt = receipts.verify_contract(contract, now=1000.0)
        assert [item["kind"] for item in receipt["checks"]] == ["tcp", "http"]
        assert str(port) not in json.dumps(receipt)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "host",
    [
        "100.72.16.110",
        "10.23.4.5",
        "fd7a:115c:a1e0::1",
        "hub.example.ts.net",
        "jordanh-worker1.ov-agent-farm.svc.cluster.local",
    ],
)
def test_tcp_adapter_accepts_explicit_managed_mesh_scope(host: str) -> None:
    contract = {
        "schema": receipts.CONTRACT_SCHEMA,
        "participant": "qdrant",
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
        "checks": [
            {
                "name": "mesh-service",
                "kind": "tcp",
                "host": host,
                "port": 6333,
                "timeout_seconds": 1,
                "network_scope": "managed_mesh",
            }
        ],
    }

    assert receipts._validate_contract(contract) == contract


@pytest.mark.parametrize(
    ("host", "scope"),
    [
        ("8.8.8.8", "managed_mesh"),
        ("example.com", "managed_mesh"),
        ("100.72.16.110", "loopback"),
        ("127.0.0.1", "managed_mesh"),
        ("127.0.0.1", "internet"),
    ],
)
def test_tcp_adapter_rejects_hosts_outside_declared_scope(
    host: str, scope: str
) -> None:
    contract = {
        "schema": receipts.CONTRACT_SCHEMA,
        "participant": "qdrant",
        "agent_id": AGENT,
        "node_identity_sha256": IDENTITY,
        "checks": [
            {
                "name": "mesh-service",
                "kind": "tcp",
                "host": host,
                "port": 6333,
                "timeout_seconds": 1,
                "network_scope": scope,
            }
        ],
    }

    with pytest.raises(receipts.PrerequisiteError):
        receipts._validate_contract(contract)


def test_tcp_probe_rejects_managed_mesh_dns_resolving_publicly(monkeypatch) -> None:
    check = {
        "name": "mesh-service",
        "kind": "tcp",
        "host": "hub.example.ts.net",
        "port": 6333,
        "timeout_seconds": 1,
        "network_scope": "managed_mesh",
    }
    monkeypatch.setattr(
        receipts.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (receipts.socket.AF_INET, receipts.socket.SOCK_STREAM, 6, "", ("8.8.8.8", 6333))
        ],
    )

    with pytest.raises(receipts.PrerequisiteError, match="outside the managed mesh"):
        receipts._probe_tcp(check)


def test_bundle_requires_exact_participant_agent_and_identity_set() -> None:
    bundle = receipts.build_bundle(
        complete_receipts(), agent_id=AGENT, node_identity_sha256=IDENTITY, now=1001.0
    )
    assert [item["participant"] for item in bundle["receipts"]] == list(
        receipts.REQUIRED_PARTICIPANTS
    )
    assert (
        receipts.validate_bundle(
            bundle,
            agent_id=AGENT,
            node_identity_sha256=IDENTITY,
            max_age_seconds=10,
            now=1002.0,
        )
        == bundle
    )

    for invalid in (
        complete_receipts()[:-1],
        complete_receipts() + [complete_receipts()[0]],
    ):
        with pytest.raises(receipts.PrerequisiteError):
            receipts.build_bundle(
                invalid, agent_id=AGENT, node_identity_sha256=IDENTITY, now=1001.0
            )

    wrong_agent = complete_receipts()
    wrong_agent[0]["agent_id"] = "agent_other"
    with pytest.raises(receipts.PrerequisiteError, match="agent differs"):
        receipts.build_bundle(
            wrong_agent, agent_id=AGENT, node_identity_sha256=IDENTITY, now=1001.0
        )


def test_bundle_rejects_stale_future_and_noncanonical_receipts() -> None:
    bundle = receipts.build_bundle(
        complete_receipts(), agent_id=AGENT, node_identity_sha256=IDENTITY, now=1001.0
    )
    with pytest.raises(receipts.PrerequisiteError, match="stale"):
        receipts.validate_bundle(
            bundle,
            agent_id=AGENT,
            node_identity_sha256=IDENTITY,
            max_age_seconds=10,
            now=1012.0,
        )
    future = json.loads(json.dumps(bundle))
    future["created_at_epoch"] = 1010.0
    with pytest.raises(receipts.PrerequisiteError, match="future"):
        receipts.validate_bundle(
            future,
            agent_id=AGENT,
            node_identity_sha256=IDENTITY,
            max_age_seconds=10,
            now=1000.0,
        )
    reordered = json.loads(json.dumps(bundle))
    reordered["receipts"].reverse()
    with pytest.raises(receipts.PrerequisiteError, match="canonical"):
        receipts.validate_bundle(
            reordered,
            agent_id=AGENT,
            node_identity_sha256=IDENTITY,
            max_age_seconds=10,
            now=1002.0,
        )


def test_bundle_binds_the_exact_expected_contract_for_every_participant() -> None:
    bundle = receipts.build_bundle(
        complete_receipts(), agent_id=AGENT, node_identity_sha256=IDENTITY, now=1001.0
    )
    assert (
        receipts.validate_bundle(
            bundle,
            agent_id=AGENT,
            node_identity_sha256=IDENTITY,
            max_age_seconds=10,
            expectations=expectations(),
            now=1002.0,
        )
        == bundle
    )

    changed = expectations()
    changed["contracts"]["openshell"] = "f" * 64
    with pytest.raises(receipts.PrerequisiteError, match="unexpected contract"):
        receipts.validate_bundle(
            bundle,
            agent_id=AGENT,
            node_identity_sha256=IDENTITY,
            max_age_seconds=10,
            expectations=changed,
            now=1002.0,
        )

    missing = expectations()
    del missing["contracts"]["webdav"]
    with pytest.raises(receipts.PrerequisiteError, match="incomplete"):
        receipts.validate_expectations(missing)


def test_cli_requires_private_single_link_inputs_and_writes_private_output(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    tool = private / "tool"
    tool.write_text("tool", encoding="utf-8")
    tool.chmod(0o700)
    contract = private_json(private / "contract.json", path_contract(tool))
    output = private / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "verify",
            "--contract",
            str(contract),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert output.stat().st_mode & 0o777 == 0o600

    contract.chmod(0o644)
    rejected = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "verify",
            "--contract",
            str(contract),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 4
    assert "owner-private" in rejected.stderr

    contract.chmod(0o600)
    linked = private / "contract-linked.json"
    os.link(contract, linked)
    rejected = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "verify",
            "--contract",
            str(contract),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 4
    assert "hard links" in rejected.stderr


def test_bundle_summary_contains_only_digests_and_participant_names() -> None:
    bundle = receipts.build_bundle(
        complete_receipts(),
        agent_id=AGENT,
        node_identity_sha256=IDENTITY,
        now=time.time(),
    )
    summary = receipts.bundle_summary(bundle)
    assert summary["schema"] == receipts.SUMMARY_SCHEMA
    assert len(summary["participants"]) == len(receipts.REQUIRED_PARTICIPANTS)
    assert set(summary["participants"][0]) == {
        "participant",
        "receipt_sha256",
        "contract_sha256",
    }
    assert "checks" not in json.dumps(summary)
