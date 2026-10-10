"""The release manifest names every component of one exact MAC release.

task_854971fe: a matching Git HEAD did not describe the deployed system
(OpenShell, wrappers and the Python stack could differ while the checkout
agreed), and three image pins drifted apart for a day (task_b1828d67).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mac import release_manifest
from mac.release_manifest import ManifestError

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "src/mac/__init__.py",
    "src/mac/data/env_config_registry.json",
    "src/mac/data/postgres/migrations",
    "scripts/image-publication-identity.py",
    "scripts/fleet-update",
    "deploy/reviewed-tool-assets.sh",
    "deploy/hermes",
    "deploy/openshell/reviewed-cli-assets.sh",
    "deploy/openshell/bootstrap-openshell.sh",
    "deploy/openshell/mac-hermes-policy.yaml",
    "deploy/systemd",
    "deploy/bin",
    "deploy/install-macos-services.sh",
    "deploy/install-fleet-context-service.sh",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    """A committed copy of exactly the release inputs."""
    repo = tmp_path / "src"
    for rel in INPUTS:
        origin = ROOT / rel
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target)
        else:
            shutil.copy2(origin, target)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release inputs")
    return repo


def _receipt(source: Path, **overrides):
    identity = release_manifest._identity_module(source)
    receipt = {
        "schema": identity.RECEIPT_SCHEMA,
        "status": "passed",
        "kind": "openshell-runtime",
        "repository": release_manifest.RUNTIME_REPOSITORY,
        "requested_revision": _git(source, "rev-parse", "HEAD"),
        "build_revision": _git(source, "rev-parse", "HEAD"),
        "frozen_inputs_sha256": "sha256:" + "1" * 64,
        "image_digest": "sha256:" + "a" * 64,
        "platforms": ["linux/amd64", "linux/arm64"],
    }
    receipt.update(overrides)
    return receipt


def test_a_manifest_names_every_component_and_one_image_pin(source):
    manifest = release_manifest.build(source, receipt=_receipt(source))

    release_manifest.verify(manifest)
    assert manifest["complete"] is True
    assert manifest["mac"]["commit"] == _git(source, "rev-parse", "HEAD")
    assert manifest["python"]["version"] == (ROOT / ".python-version").read_text().strip()
    assert manifest["tools"]["uv"]
    assert manifest["openshell"]["version"]
    assert manifest["hermes"]["revision"]
    assert manifest["runtime_tools"]["OPENCODE_VERSION"]
    assert manifest["database"]["latest"].endswith(".sql")
    assert "deploy/systemd/mac-agent.service.in" in manifest["services"]
    assert "deploy/bin/mac-service" in manifest["services"]
    images = manifest["images"]
    assert images["coding"] == images["verification"]
    assert images["coding"] == "%s@sha256:%s" % (release_manifest.RUNTIME_REPOSITORY, "a" * 64)
    assert set(manifest["roles"]) == {"hub", "worker"}
    assert "openshell" not in manifest["roles"]["hub"]["components"]


def test_two_builds_of_one_commit_agree_and_any_input_change_shows(source):
    first = release_manifest.build(source, receipt=_receipt(source))
    assert release_manifest.build(source, receipt=_receipt(source)) == first

    unit = source / "deploy" / "systemd" / "mac-agent.service.in"
    unit.write_text(unit.read_text() + "# changed\n")
    with pytest.raises(ManifestError, match="uncommitted changes"):
        release_manifest.build(source)
    _git(source, "commit", "-qam", "change a unit")
    second = release_manifest.build(source)

    changed = release_manifest.diff(first, second)
    assert "services.deploy/systemd/mac-agent.service.in" in changed
    assert "mac.commit" in changed
    assert "images.coding" in changed  # the second build has no receipt yet


def test_an_edited_manifest_fails_verification(source):
    manifest = release_manifest.build(source, receipt=_receipt(source))
    manifest["openshell"]["version"] = "0.0.72"
    with pytest.raises(ManifestError, match="does not match its content"):
        release_manifest.verify(manifest)


def test_an_unresolved_image_pin_is_incomplete(source):
    manifest = release_manifest.build(source)
    assert manifest["complete"] is False
    with pytest.raises(ManifestError, match="incomplete"):
        release_manifest.verify(manifest)
    release_manifest.verify(manifest, require_complete=False)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"requested_revision": "0" * 40}, "receipt was produced for"),
        ({"status": "failed"}, "is not a passed"),
        ({"kind": "mac"}, "not for the worker runtime image"),
        ({"image_digest": "latest"}, "no exact image digest"),
    ],
)
def test_a_foreign_or_unfinished_receipt_is_refused(source, override, message):
    with pytest.raises(ManifestError, match=message):
        release_manifest.build(source, receipt=_receipt(source, **override))


def test_reviewed_python_must_agree_with_the_interpreter_pin(source):
    (source / ".python-version").write_text("3.13.0\n")
    _git(source, "commit", "-qam", "drift the interpreter")
    with pytest.raises(ManifestError, match="disagrees with .python-version"):
        release_manifest.build(source)


def test_cli_build_verify_and_diff(source, tmp_path, capsys):
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps(_receipt(source)))
    out = tmp_path / "manifest.json"
    assert (
        release_manifest.main(
            [
                "build",
                "--source",
                str(source),
                "--runtime-receipt",
                str(receipt),
                "--require-complete",
                "--output",
                str(out),
            ]
        )
        == 0
    )
    assert release_manifest.main(["verify", str(out)]) == 0
    assert " ok (" in capsys.readouterr().out
    assert release_manifest.main(["build", "--source", str(source), "--require-complete"]) == 2
    assert "incomplete" in capsys.readouterr().err
    assert release_manifest.main(["diff", str(out), str(out)]) == 0
    assert capsys.readouterr().out == ""
