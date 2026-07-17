from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manifest = _module(
    "certifier_harness_manifest",
    ROOT / "deploy" / "certifier" / "harness_manifest.py",
)
publication = _module(
    "certifier_publication_verifier",
    ROOT / "scripts" / "verify-certifier-publication.py",
)
canary = _module(
    "work_package_canary",
    ROOT / "scripts" / "work-package-canary.py",
)
context = _module(
    "certifier_context_manifest",
    ROOT / "scripts" / "certifier-context-manifest.py",
)
selector = _module(
    "certifier_test_selector",
    ROOT / "deploy" / "certifier" / "select-tests.py",
)


def _harness_root(tmp_path: Path) -> Path:
    for tree in manifest.MANAGED_TREES:
        (tmp_path / tree).mkdir(parents=True)
    (tmp_path / "tests" / "test_baseline.py").write_text("assert True\n", encoding="utf-8")
    for name in manifest.MANAGED_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"trusted {name}\n", encoding="utf-8")
    return tmp_path


def test_frozen_harness_manifest_rejects_edits_and_inventory_growth(tmp_path: Path) -> None:
    root = _harness_root(tmp_path)
    payload = manifest.build_manifest(root, source_revision="a" * 40)

    manifest.verify_manifest(root, payload)
    assert payload["schema"] == "mac.certifier_trusted_harness.v1"
    assert payload["source_revision"] == "a" * 40

    target = root / "tests" / "test_baseline.py"
    target.write_text("assert False\n", encoding="utf-8")
    with pytest.raises(manifest.HarnessError, match="digest differs"):
        manifest.verify_manifest(root, payload)

    target.write_text("assert True\n", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text("# unreviewed\n", encoding="utf-8")
    with pytest.raises(manifest.HarnessError, match="inventory differs"):
        manifest.verify_manifest(root, payload)


def test_frozen_harness_rejects_symlinks_and_root_test_controls(tmp_path: Path) -> None:
    root = _harness_root(tmp_path)
    payload = manifest.build_manifest(root, source_revision="b" * 40)

    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    with pytest.raises(manifest.HarnessError, match="untrusted test control"):
        manifest.verify_manifest(root, payload)
    (root / "pytest.ini").unlink()

    target = root / "tests" / "test_baseline.py"
    target.unlink()
    target.symlink_to(root / "plugin" / "test_tools.py")
    with pytest.raises(manifest.HarnessError, match="symlink"):
        manifest.verify_manifest(root, payload)


def test_manifest_file_is_strict_and_deterministic(tmp_path: Path) -> None:
    root = _harness_root(tmp_path / "root")
    payload = manifest.build_manifest(root, source_revision="c" * 40)
    output = tmp_path / "manifest.json"
    manifest._write_manifest(output, payload)

    assert output.read_text(encoding="utf-8") == json.dumps(
        payload, indent=2, sort_keys=True
    ) + "\n"
    assert manifest.load_manifest(output) == payload

    payload["unknown"] = True
    output.chmod(0o600)
    output.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(manifest.HarnessError, match="invalid shape"):
        manifest.load_manifest(output)


def test_publication_verifier_requires_exact_ghcr_digest_and_exact_policy_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_bytes(b"version: 1\nnetwork_policies: {}\n")
    assert publication.policy_checksum(policy) == "sha256:" + hashlib.sha256(
        policy.read_bytes()
    ).hexdigest()

    with pytest.raises(publication.VerificationError, match="sha256"):
        publication.verify_registry_digest("ghcr.io/jordanhubbard/mac-certifier:latest")
    with pytest.raises(publication.VerificationError, match="mac-certifier"):
        publication.verify_registry_digest(
            "ghcr.io/another-owner/mac-certifier@sha256:" + "a" * 64
        )

    monkeypatch.setenv("DOCKER_CONFIG", "/credential-bearing/default")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "secret")
    anonymous = publication._anonymous_docker_environment(tmp_path / "empty-docker")
    assert anonymous["DOCKER_CONFIG"] == str(tmp_path / "empty-docker")
    assert "DOCKER_AUTH_CONFIG" not in anonymous
    assert json.loads(
        (tmp_path / "empty-docker" / "config.json").read_text(encoding="utf-8")
    ) == {"auths": {}}


def test_certifier_container_is_pinned_nonroot_and_image_owned() -> None:
    containerfile = (ROOT / "deploy" / "certifier" / "Containerfile").read_text(
        encoding="utf-8"
    )
    from_lines = [line for line in containerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 2
    assert all(re.search(r"@sha256:[0-9a-f]{64}(?:\s|$)", line) for line in from_lines)
    assert "COPY . /opt/mac-certifier/trusted" in containerfile
    assert "git -C /opt/mac-certifier/trusted init -q -b main" in containerfile
    assert "git -C /opt/mac-certifier/trusted add -f -A" in containerfile
    assert "find /opt/mac-certifier/trusted -type f -exec chmod 0444" in containerfile
    assert "uv sync --frozen --extra dev --no-install-project" in containerfile
    assert "USER sandbox" in containerfile
    assert "/opt/mac-certifier/bin/run-contract-tests --image-self-test" in containerfile

    launcher = (ROOT / "deploy" / "certifier" / "run-contract-tests").read_text(
        encoding="utf-8"
    )
    verify_at = launcher.index("harness_manifest.py\" verify")
    execute_at = launcher.rindex('"$CERTIFIER_ROOT/libexec/authoritative-contract-tests"')
    assert verify_at < execute_at
    assert '"$scratch/scripts/run-contract-tests.sh" "$@"' not in launcher
    assert '"$#" -eq 2' in launcher
    assert '"$1" = "--base-sha"' in launcher
    assert "merge-base" not in launcher  # validation lives in the frozen selector
    assert 'git clone --quiet --no-hardlinks "$candidate_root" "$scratch"' in launcher
    assert 'rm -rf "$scratch/tests"' in launcher
    assert "\\( -type f -o -type l \\)" in launcher
    assert "\\( -name 'test*.py' -o -name 'conftest.py' \\) -delete" in launcher
    assert '--root "$scratch" --manifest "$MANIFEST"' in launcher
    authoritative = (
        ROOT / "deploy" / "certifier" / "authoritative-contract-tests"
    ).read_text(encoding="utf-8")
    assert '"${targets[@]}"' in authoritative
    assert '-o "pythonpath=$candidate_src"' in authoritative
    assert "/usr/bin/env -i" in authoritative
    assert "PYTHONSAFEPATH=1" in authoritative
    assert launcher.index("authoritative-contract-tests") < launcher.index("git clone --quiet")
    assert '"$CERTIFIER_ROOT/libexec/run-contract-tests"' not in launcher


def _selector_trusted_root(tmp_path: Path) -> Path:
    root = tmp_path / "trusted"
    for relative in selector.INVARIANT_TESTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_invariant(): pass\n", encoding="utf-8")
    (root / "tests" / "test_publication_lane_extra.py").write_text(
        "def test_extra(): pass\n", encoding="utf-8"
    )
    return root


@pytest.mark.parametrize(
    ("changed", "selection_mode", "authoritative", "supplemental", "full_count"),
    [
        (["docs/fast.md"], "documentation_fast_lane", "focused", "skipped", 0),
        (["src/mac/publication_lane.py"], "source_focused", "focused", "skipped", 0),
        (
            ["src/mac/publication_lane.py", "tests/test_publication_lane.py"],
            "source_focused",
            "focused",
            "skipped",
            0,
        ),
        (["deploy/worker.sh"], "supplemental_full", "focused", "full", 1),
        (["src/mac/data/runtime.json"], "authoritative_full", "full", "skipped", 1),
        (
            ["src/mac/data/runtime.json", "deploy/worker.sh"],
            "mixed_unmapped_rejected",
            "rejected",
            "skipped",
            0,
        ),
        (
            ["src/mac/publication_lane.py", "deploy/worker.sh"],
            "supplemental_full",
            "focused",
            "full",
            1,
        ),
    ],
)
def test_frozen_selector_is_proportional_and_never_runs_two_full_suites(
    tmp_path: Path,
    changed: list[str],
    selection_mode: str,
    authoritative: str,
    supplemental: str,
    full_count: int,
) -> None:
    plan = selector.plan_selection(
        changed,
        trusted_root=_selector_trusted_root(tmp_path),
        assembly_base_sha="a" * 40,
        candidate_sha="b" * 40,
        trusted_source_revision="c" * 40,
    )

    assert plan["selection_mode"] == selection_mode
    assert plan["authoritative"]["mode"] == authoritative
    assert plan["supplemental"]["mode"] == supplemental
    assert plan["full_suite_count"] == full_count
    assert sum(
        phase["mode"] == "full"
        for phase in (plan["authoritative"], plan["supplemental"])
    ) <= 1
    assert plan["manifest_digest"].startswith("sha256:")


def test_frozen_selector_proves_exact_base_object_and_ancestor(tmp_path: Path) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "base.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "docs" / "next.md").write_text("next\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "next"], cwd=repo, check=True)

    _candidate, changed = selector.inspect_candidate(repo, base)
    assert changed == ["docs/next.md"]
    with pytest.raises(selector.SelectionError, match="Git scope validation failed"):
        selector.inspect_candidate(repo, "d" * 40)


def test_green_ci_publishes_both_images_without_certifier_digest_loop() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ghcr.io/jordanhubbard/mac:git-${{ github.sha }}" in workflow
    assert "ghcr.io/jordanhubbard/mac-certifier:git-${{ github.sha }}" in workflow
    assert workflow.count("platforms: linux/amd64,linux/arm64") >= 2
    assert workflow.count("provenance: mode=max") >= 2
    assert workflow.count("sbom: true") >= 2
    assert "packages: write" in workflow
    assert "attestations: write" in workflow
    assert "needs.publication-scope.outputs.certifier_changed == 'true'" in workflow
    assert ".mac/project.yaml" not in workflow
    certifier_job = workflow.split("\n  certifier-image:\n", 1)[1]
    assert "/user/packages/container/mac-certifier/visibility" not in certifier_job
    assert "gh api --method PATCH" not in certifier_job
    assert "gh api /user/packages/container/mac-certifier" in certifier_job
    assert 'test "$visibility" = public' in certifier_job
    assert 'DOCKER_CONFIG="$empty_config" docker pull' in certifier_job
    assert "for platform in linux/amd64 linux/arm64" in certifier_job
    assert '--platform "$platform" "$exact_ref"' in certifier_job
    assert "org.opencontainers.image.revision" in certifier_job
    assert "/opt/mac-certifier/bin/run-contract-tests --image-self-test" in certifier_job

    build_script = (ROOT / "scripts" / "build-certifier-image.sh").read_text(
        encoding="utf-8"
    )
    assert "--load" in build_script
    assert "--push" not in build_script


def test_cutover_canary_plans_compile_and_encode_opposite_git_expectations() -> None:
    from mac.work_package_models import (
        compile_work_package_plan,
        validate_executable_work_package_effects,
        validate_supported_work_package_topology,
    )

    plans = {
        case: canary._plan(
            case,
            run_id="pilot_deadbeef1234",
            repository_id="projectrepo_mac",
            base_sha="d" * 40,
            target_ref="refs/heads/main",
        )
        for case in ("negative", "positive")
    }
    for plan in plans.values():
        assert {node["priority"] for node in plan["nodes"]} == {1_000_000}
        compiled = compile_work_package_plan(plan)
        assert {node["priority"] for node in compiled.definition["nodes"]} == {
            1_000_000
        }
        validate_executable_work_package_effects(compiled)
        validate_supported_work_package_topology(compiled.definition)

    negative = plans["negative"]
    positive = plans["positive"]
    assert negative["metadata"]["expected_canonical_movement"] is False
    assert positive["metadata"]["expected_canonical_movement"] is True
    assert "candidate-owned pre-push suite to pass" in negative["nodes"][0]["instructions"]
    assert positive["nodes"][0]["effects"]["writes"] == [
        "docs/canaries/pilot_deadbeef1234-managed-positive.md"
    ]


def test_cutover_canary_is_plan_only_without_three_live_authorizations(capsys) -> None:
    common = [
        "--repository-id",
        "projectrepo_mac",
        "--canonical-remote",
        "git@example.invalid:owner/repo.git",
        "--base-sha",
        "e" * 40,
        "--run-id",
        "pilot_guard",
        "--case",
        "negative",
    ]
    assert canary.main(common) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "plan"
    assert "receipts" not in output

    assert canary.main(common + ["--execute"]) == 1
    error = capsys.readouterr().err
    assert "--confirm-live" in error
    assert "--confirm-exclusive-main-window" in error


def test_repository_discovery_passes_read_token_without_recording_it(monkeypatch) -> None:
    calls = []

    def fake_request(hub_url, path, **kwargs):
        calls.append((hub_url, path, kwargs))
        return [{"id": "repo", "name": "mac", "project": "mac"}]

    monkeypatch.setattr(canary, "_request", fake_request)
    assert canary._registered_repository(
        "https://hub.invalid", "mac", token="admin-read-token"
    )["id"] == "repo"
    assert calls == [
        (
            "https://hub.invalid",
            "/bridge/repositories?enabled=true",
            {"token": "admin-read-token"},
        )
    ]


def test_context_manifest_rejects_secret_shapes_and_symlinks(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    safe = tmp_path / "safe.txt"
    safe.write_text("safe\n", encoding="utf-8")
    first = context.context_manifest(tmp_path)
    second = context.context_manifest(tmp_path)
    assert first["digest"] == second["digest"]

    secret = tmp_path / ".env.local"
    secret.write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
    with pytest.raises(context.ContextError, match="secret-shaped"):
        context.context_manifest(tmp_path)
    secret.unlink()

    (tmp_path / "link.txt").symlink_to(safe)
    with pytest.raises(context.ContextError, match="regular file"):
        context.context_manifest(tmp_path)


def test_context_materialization_is_allowlisted_and_mode_exact(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored-local-token\n", encoding="utf-8")
    executable = root / "scripts" / "gate"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (root / "ignored-local-token").write_text("not-a-real-secret\n", encoding="utf-8")

    payload = context.context_manifest(root)
    destination = tmp_path / "materialized"
    context.materialize_context(root, destination, payload)

    assert (destination / ".gitignore").is_file()
    assert (destination / "scripts" / "gate").stat().st_mode & 0o777 == 0o755
    assert not (destination / "ignored-local-token").exists()
    assert not (destination / ".git").exists()
    assert {item["path"] for item in payload["files"]} == {
        ".gitignore",
        "scripts/gate",
    }
