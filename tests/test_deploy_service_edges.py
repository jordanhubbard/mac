"""Edge coverage for runtime-delta and artifact deployment validation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac.models import NotFoundError, ValidationError
from mac.services import ControlPlane


def _delta(**extra):
    values = {
        "package_manager": "pip",
        "commands": [".venv/bin/python -m pip install one==1"],
        "added_dependencies": ["one==1"],
        "base_runtime_id": "runtime",
        "base_runtime_digest": "sha256:" + "a" * 64,
        "lockfile_path": "requirements.lock",
        "lockfile_digest": "sha256:" + "b" * 64,
        "reason": "needed",
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_runtime_delta_pinning_matrix() -> None:
    deploy = ControlPlane.in_memory().deploy
    accepted = [
        ("one==1", "pip"),
        ("one===1", "uv"),
        ("one @ https://example.test/one.whl", "pip"),
        ("one#sha256=" + "a" * 64, "pip"),
        ("one --hash=sha256:" + "a" * 64, "uv"),
        ("one@1", "npm"),
        ("@scope/one@1", "pnpm"),
    ]
    for dependency, manager in accepted:
        assert deploy._runtime_delta_dependency_pinned(dependency, manager) is True
    for dependency, manager in [
        ("", "pip"), ("one*", "pip"), ("latest", "npm"),
        ("one@latest", "npm"), ("@scope/one", "npm"), ("one", "other"),
    ]:
        assert deploy._runtime_delta_dependency_pinned(dependency, manager) is False


def test_runtime_delta_dependency_shape_problems() -> None:
    deploy = ControlPlane.in_memory().deploy
    delta = _delta(added_dependencies=[
        {"requirement": "one"},
        {"name": "two"},
        {"name": "three", "version": "latest"},
        {"name": "four", "specifier": "4.0"},
        "five",
        "six==6",
    ])
    problems = deploy._runtime_delta_dependency_problems(delta)
    assert any("one" in item for item in problems)
    assert any("name and version" in item for item in problems)
    assert any("three" in item for item in problems)
    assert any("five" in item for item in problems)
    assert not any("six" in item for item in problems)


def test_runtime_delta_command_problem_matrix() -> None:
    deploy = ControlPlane.in_memory().deploy
    delta = _delta(commands=[
        "sudo apt-get install curl",
        "tool token=value",
        "npm install -g package",
        "python -m pip install one==1",
        ".venv/bin/python -m pip install safe==1",
    ])
    problems = deploy._runtime_delta_command_problems(delta)
    assert any("host/shared" in item for item in problems)
    assert any("secret" in item for item in problems)
    assert any("globally" in item for item in problems)
    assert any("virtualenv" in item for item in problems)


def test_runtime_delta_validation_accumulates_contract_problems(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    deploy = cp.deploy
    delta = _delta(
        package_manager="unknown",
        commands=[],
        added_dependencies=[],
        base_runtime_id=None,
        base_runtime_digest=None,
        lockfile_path="/absolute/../lock",
        lockfile_digest="bad",
    )
    monkeypatch.setattr(
        deploy,
        "_scan_runtime_manifest",
        lambda *_a: (_ for _ in ()).throw(ValidationError("manifest invalid")),
    )
    problems = deploy._runtime_delta_validation_problems(delta)
    assert len(problems) >= 7
    assert "manifest invalid" in problems


def test_runtime_delta_validation_base_runtime_mismatch_and_lookup(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    deploy = cp.deploy
    runtime = SimpleNamespace(id="runtime", digest="sha256:" + "c" * 64)
    monkeypatch.setattr(deploy, "get_runtime", lambda *_a: runtime)
    monkeypatch.setattr(deploy, "_scan_runtime_manifest", lambda *_a: None)
    problems = deploy._runtime_delta_validation_problems(_delta())
    assert "base_runtime_digest does not match base_runtime_id" in problems
    monkeypatch.setattr(deploy, "get_runtime", lambda *_a: (_ for _ in ()).throw(NotFoundError()))
    problems = deploy._runtime_delta_validation_problems(_delta())
    assert "base_runtime_id does not reference a registered runtime" in problems
    monkeypatch.setattr(deploy.store, "query_one", lambda *_a, **_k: None)
    problems = deploy._runtime_delta_validation_problems(
        _delta(base_runtime_id=None, base_runtime_digest="sha256:" + "d" * 64)
    )
    assert "base_runtime_digest is not registered" in problems


def test_runtime_for_delta_uses_id_digest_and_rejects_missing(monkeypatch) -> None:
    deploy = ControlPlane.in_memory().deploy
    runtime = SimpleNamespace(id="runtime")
    monkeypatch.setattr(deploy, "get_runtime", lambda *_a: runtime)
    assert deploy._runtime_for_delta(_delta()) is runtime
    monkeypatch.setattr(deploy.store, "query_one", lambda *_a, **_k: {"id": "row"})
    monkeypatch.setattr(deploy, "_runtime_from_row", lambda row: ("runtime", row))
    assert deploy._runtime_for_delta(_delta(base_runtime_id=None))[0] == "runtime"
    monkeypatch.setattr(deploy.store, "query_one", lambda *_a, **_k: None)
    with pytest.raises(ValidationError, match="no registered base"):
        deploy._runtime_for_delta(_delta(base_runtime_id=None, base_runtime_digest=None))


def test_runtime_delta_list_filters_and_invalid_status(monkeypatch) -> None:
    deploy = ControlPlane.in_memory().deploy
    with pytest.raises(ValidationError, match="unsupported runtime delta status"):
        deploy.list_runtime_deltas(status="unknown")
    captured = []
    monkeypatch.setattr(deploy.store, "query_all", lambda sql, params: captured.append((sql, params)) or [])
    assert deploy.list_runtime_deltas(
        status="proposed", task_id="task", project="project", limit=5000
    ) == []
    assert "status = ?" in captured[0][0]
    assert captured[0][1][-1] == 1000


def test_artifact_digest_local_path_boundaries(tmp_path: Path) -> None:
    deploy = ControlPlane.in_memory().deploy
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"payload")
    digest = "sha256:" + hashlib.sha256(b"payload").hexdigest()
    deploy._verify_artifact_digest_if_local(str(source), digest)
    deploy._verify_artifact_digest_if_local(source.as_uri(), digest)
    with pytest.raises(ValidationError, match="does not match"):
        deploy._verify_artifact_digest_if_local(str(source), "sha256:" + "0" * 64)
    deploy._verify_artifact_digest_if_local(str(tmp_path / "missing"), digest)
    deploy._verify_artifact_digest_if_local("https://example.test/artifact", digest)
