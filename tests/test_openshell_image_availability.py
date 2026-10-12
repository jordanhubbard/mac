from __future__ import annotations

import subprocess

from mac.executor_sandbox import ensure_pinned_openshell_images


def test_missing_pinned_images_are_pulled(tmp_path, monkeypatch) -> None:
    gateway = tmp_path / "gateway.toml"
    gateway.write_text(
        '[openshell.drivers.docker]\nsupervisor_image = "registry/supervisor@sha256:aaa"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "mac.executor_sandbox.verifier_runtime_image",
        lambda: ("registry/runtime@sha256:bbb", "test"),
    )
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        rc = 1 if argv[1:3] == ["image", "inspect"] else 0
        return subprocess.CompletedProcess(argv, rc, "", "")

    report = ensure_pinned_openshell_images(gateway_config=gateway, run=run)

    assert report["pulled"] == ["supervisor", "runtime"]
    assert report["messages"] == [
        "pulled missing supervisor image",
        "pulled missing runtime image",
    ]
    assert [call for call in calls if call[1] == "pull"] == [
        ["docker", "pull", "registry/supervisor@sha256:aaa"],
        ["docker", "pull", "registry/runtime@sha256:bbb"],
    ]


def test_present_pinned_images_are_not_pulled(tmp_path, monkeypatch) -> None:
    gateway = tmp_path / "gateway.toml"
    gateway.write_text(
        '[openshell.drivers.docker]\nsupervisor_image = "registry/supervisor@sha256:aaa"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("mac.executor_sandbox.verifier_runtime_image", lambda: ("", "test"))
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    report = ensure_pinned_openshell_images(gateway_config=gateway, run=run)
    assert report["pulled"] == []
    assert calls == [["docker", "image", "inspect", "registry/supervisor@sha256:aaa"]]
