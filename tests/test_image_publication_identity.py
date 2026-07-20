from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import shlex
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "image-publication-identity.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("image_publication_identity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_sources(path: Path) -> set[str]:
    sources: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("COPY "):
            continue
        fields = shlex.split(raw)
        if any(field.startswith("--from=") for field in fields[1:]):
            continue
        fields = [field for field in fields[1:] if not field.startswith("--")]
        sources.update(fields[:-1])
    return sources


def _from_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("FROM ")
    ]


def _arg_defaults(path: Path) -> dict[str, str]:
    defaults: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r'ARG ([A-Z][A-Z0-9_]*)="?([^" ]*)"?', line)
        if match and match.group(1) != "TARGETARCH":
            defaults[match.group(1)] = match.group(2)
    return defaults


def _minimal_root(tmp_path: Path, module, kind: str) -> Path:
    root = tmp_path / kind
    root.mkdir()
    spec = module.IMAGE_SPECS[kind]
    for relative in spec["files"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"frozen:{relative}\n", encoding="utf-8")
    for relative in spec["trees"]:
        path = root / relative / "mac" / "runtime.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 1\n", encoding="utf-8")
    return root


def _verification(module, plan: dict, revision: str, image_digest: str) -> dict:
    return {
        "schema": module.VERIFY_SCHEMA,
        "status": "passed",
        "anonymous": True,
        "kind": plan["kind"],
        "repository": plan["repository"],
        "image_ref": f"{plan['repository']}@{image_digest}",
        "image_digest": image_digest,
        "build_revision": revision,
        "frozen_inputs_sha256": plan["frozen_inputs_sha256"],
        "platforms": [
            {
                "platform": platform,
                "digest": image_digest,
                "build_revision": revision,
                "smoke": "passed",
            }
            for platform in plan["platforms"]
        ],
    }


def _provenance(image_digest: str, repository: str) -> list[dict]:
    return [
        {
            "verificationResult": {
                "statement": {
                    "predicateType": "https://slsa.dev/provenance/v1",
                    "subject": [
                        {
                            "name": repository,
                            "digest": {"sha256": image_digest.removeprefix("sha256:")},
                        }
                    ],
                }
            }
        }
    ]


def test_frozen_contract_covers_every_copy_arg_and_base_digest_boundary() -> None:
    module = _load_module()
    mac = module.IMAGE_SPECS["mac"]
    runtime = module.IMAGE_SPECS["openshell-runtime"]

    assert _copy_sources(ROOT / "Dockerfile") == {
        "README.md",
        "deploy/mac-crash-observer.py",
        "pyproject.toml",
        "src",
        "uv.lock",
    }
    assert _copy_sources(ROOT / "deploy/openshell/mac-hermes.Containerfile") == {
        ".mac-openshell-build-assets",
        "README.md",
        "deploy/verify-bash-contract.sh",
        "pyproject.toml",
        "src",
        "uv.lock",
    }
    assert set(mac["files"]) | set(mac["trees"]) >= _copy_sources(ROOT / "Dockerfile")
    runtime_sources = _copy_sources(ROOT / "deploy/openshell/mac-hermes.Containerfile")
    runtime_sources.remove(".mac-openshell-build-assets")
    assert set(runtime["files"]) | set(runtime["trees"]) >= runtime_sources
    assert {
        "deploy/openshell/prepare-runtime-image-assets.sh",
        "deploy/reviewed-tool-assets.sh",
    } <= set(runtime["files"])
    assert runtime["build_args"] == _arg_defaults(
        ROOT / "deploy/openshell/mac-hermes.Containerfile"
    )
    assert mac["build_args"] == {}

    for dockerfile in (
        ROOT / "Dockerfile",
        ROOT / "deploy/openshell/mac-hermes.Containerfile",
    ):
        lines = _from_lines(dockerfile)
        assert lines
        assert all(
            re.search(r"@sha256:[0-9a-f]{64}(?: AS \w+)?$", line) for line in lines
        )


def test_plan_digest_changes_only_for_frozen_material(tmp_path: Path) -> None:
    module = _load_module()
    root = _minimal_root(tmp_path, module, "mac")
    revision = "a" * 40
    first = module.build_plan(root, "mac", revision, [])

    ignored = root / "src" / "notes.md"
    ignored.write_text("not copied\n", encoding="utf-8")
    assert (
        module.build_plan(root, "mac", revision, [])["frozen_inputs_sha256"]
        == first["frozen_inputs_sha256"]
    )

    copied = root / "src" / "mac" / "runtime.py"
    copied.write_text("value = 2\n", encoding="utf-8")
    second = module.build_plan(root, "mac", revision, [])
    assert second["frozen_inputs_sha256"] != first["frozen_inputs_sha256"]
    assert second["content_tag"].endswith(
        second["frozen_inputs_sha256"].removeprefix("sha256:")
    )


def test_plan_uses_the_validators_canonical_relative_path_order(tmp_path: Path) -> None:
    module = _load_module()
    root = _minimal_root(tmp_path, module, "mac")
    for relative in (
        "src/mac/plugins/openai/plugin.yaml",
        "src/mac/plugins/openai-codex/__init__.py",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"frozen:{relative}\n", encoding="utf-8")

    plan = module.build_plan(root, "mac", "a" * 40, [])
    serialized_paths = [entry["path"] for entry in plan["inputs"]]

    assert serialized_paths == sorted(serialized_paths)
    module._validate_plan(plan)


def test_runtime_plan_requires_the_complete_reviewed_arg_set(tmp_path: Path) -> None:
    module = _load_module()
    root = _minimal_root(tmp_path, module, "openshell-runtime")
    values = [
        f"{key}={value}"
        for key, value in module.IMAGE_SPECS["openshell-runtime"]["build_args"].items()
    ]

    plan = module.build_plan(root, "openshell-runtime", "b" * 40, values)
    assert plan["build_args"] == module.IMAGE_SPECS["openshell-runtime"]["build_args"]
    with pytest.raises(module.IdentityError, match="reviewed contract"):
        module.build_plan(root, "openshell-runtime", "b" * 40, values[:-1])


def test_publication_receipt_separates_requested_and_original_build_revision(
    tmp_path: Path,
) -> None:
    module = _load_module()
    root = _minimal_root(tmp_path, module, "mac")
    requested = "c" * 40
    built = "d" * 40
    image_digest = "sha256:" + "e" * 64
    plan = module.build_plan(root, "mac", requested, [])
    verification = _verification(module, plan, built, image_digest)

    receipt = module.publication_receipt(
        plan,
        verification,
        _provenance(image_digest, plan["repository"]),
        "reused",
        "github-attestation-verified",
    )
    assert receipt["requested_revision"] == requested
    assert receipt["build_revision"] == built
    assert receipt["image_ref"] == f"{plan['repository']}@{image_digest}"
    assert receipt["provenance"]["subject_digest"] == image_digest

    with pytest.raises(module.IdentityError, match="controller revision"):
        module.publication_receipt(
            plan,
            verification,
            _provenance(image_digest, plan["repository"]),
            "built",
            "github-attested",
        )


def test_private_plan_validation_recomputes_the_frozen_digest(tmp_path: Path) -> None:
    module = _load_module()
    root = _minimal_root(tmp_path, module, "mac")
    plan = module.build_plan(root, "mac", "f" * 40, [])
    plan["inputs"][0]["size"] += 1

    with pytest.raises(module.IdentityError, match="does not bind"):
        module._validate_plan(plan)


def test_reuse_requires_original_github_provenance_and_repairs_a_poisoned_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    root = _minimal_root(tmp_path, module, "mac")
    plan = module.build_plan(root, "mac", "1" * 40, [])
    image_digest = "sha256:" + "2" * 64
    build_revision = "3" * 40
    calls: list[list[str]] = []

    def missing(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "attestation not found")

    monkeypatch.setattr(module, "_run", missing)
    assert module.qualify_reuse(
        plan,
        candidate_reuse="true",
        image_digest=image_digest,
        build_revision=build_revision,
        github_repository="jordanhubbard/mac",
        gh="gh",
    ) == {"reuse": "false", "digest": "", "build_revision": ""}
    assert "--source-ref" in calls[0]
    assert "--deny-self-hosted-runners" in calls[0]

    def verified(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(_provenance(image_digest, plan["repository"])),
            "",
        )

    monkeypatch.setattr(module, "_run", verified)
    assert (
        module.qualify_reuse(
            plan,
            candidate_reuse="true",
            image_digest=image_digest,
            build_revision=build_revision,
            github_repository="jordanhubbard/mac",
            gh="gh",
        )["reuse"]
        == "true"
    )


def test_atomic_receipt_is_owner_private(tmp_path: Path) -> None:
    module = _load_module()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    output = parent / "receipt.json"

    module.atomic_private_json(output, {"status": "passed"})

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "passed"}


def test_atomic_receipt_rejects_existing_output_symlink(tmp_path: Path) -> None:
    module = _load_module()
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    protected = parent / "protected.json"
    protected.write_text('{"keep":true}\n', encoding="utf-8")
    protected.chmod(0o600)
    output = parent / "receipt.json"
    output.symlink_to(protected)

    with pytest.raises(module.IdentityError, match="unsafe"):
        module.atomic_private_json(output, {"status": "passed"})
    assert json.loads(protected.read_text(encoding="utf-8")) == {"keep": True}


def test_ci_reuses_only_verified_content_identity_and_still_emits_receipts() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for job_name, next_job, directory, kind in (
        ("docker", "openshell-runtime-image", "mac-image-publication", "mac"),
        (
            "openshell-runtime-image",
            "certifier-image",
            "openshell-runtime-publication",
            "openshell-runtime",
        ),
    ):
        job = workflow.split(f"\n  {job_name}:\n", 1)[1].split(f"\n  {next_job}:\n", 1)[
            0
        ]
        assert f"--kind {kind}" in job
        assert "scripts/image-publication-identity.py probe" in job
        assert "scripts/image-publication-identity.py qualify-reuse" in job
        assert "if: steps.image-reuse-eligibility.outputs.reuse != 'true'" in job
        assert "${{ steps.image-plan.outputs.content_tag }}" in job
        assert "io.mac.frozen-inputs.sha256=" in job
        assert "scripts/image-publication-identity.py verify" in job
        assert "gh attestation verify" in job
        assert '--source-digest "$BUILD_REVISION"' in job
        assert "scripts/image-publication-identity.py receipt" in job
        assert f"{directory}/publication-receipt.json" in job
        assert "cancel-in-progress: false" in job
