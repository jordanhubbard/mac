from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac.models import ValidationError, json_loads
from mac.store import Store
from mac.test_support import ephemeral_store
from mac.work_package_models import compile_work_package_plan
from mac.work_package_service import RepositoryBaseAttestation, WorkPackageService
from mac.work_plan_admission import (
    GitCanonicalBaseResolver,
    ManagedWorkPlanBridge,
    CanonicalRepositoryBase,
    managed_plan_from_dashboard_accept,
)


SHA = "a" * 40


class _BaseResolver:
    def __init__(self, sha: str = SHA, *, resource_namespace=None) -> None:
        self.sha = sha
        self.resource_namespace = dict(resource_namespace or {})
        self.calls = []

    def resolve(self, repository, *, requested_ref=None):
        self.calls.append((dict(repository), requested_ref))
        return CanonicalRepositoryBase(
            repository_id=repository["id"],
            planning_base_ref=requested_ref or "refs/heads/main",
            planning_base_sha=self.sha,
            resource_namespace=self.resource_namespace,
        )


class _AdmissionAttestor:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={"status": "unresolved"},
        )


def _register_repository(
    store: Store,
    *,
    repository_id: str = "projectrepo_mac",
    name: str = "mac",
    project: str = "mac",
    source: str = "git@example.invalid:org/mac.git",
) -> None:
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            repository_id,
            name,
            "/missing/controller/checkout",
            source,
            project,
            "[]",
            1,
            60,
            "{}",
            "created",
            "updated",
        ),
    )


def _proposal() -> dict:
    return {
        "nodes": [
            {
                "node_id": "backend",
                "title": "Implement backend",
                "description": "Change the backend slice.",
                "kind": "mutation",
                "effects": {"writes": ["src/backend"]},
                "expected_outputs": ["backend-candidate"],
                "estimates": {"confidence": "high"},
                "verification": {
                    "profile": "repository-default",
                    "command": "pytest tests/backend",
                },
            },
            {
                "node_id": "frontend",
                "title": "Implement frontend",
                "description": "Change the frontend slice.",
                "kind": "mutation",
                "effects": {"writes": ["src/frontend"]},
                "expected_outputs": ["frontend-candidate"],
                "estimates": {"confidence": "high"},
                "verification": {
                    "profile": "repository-default",
                    "command": "npm test",
                },
            },
            {
                "node_id": "assemble",
                "title": "Assemble exact candidate",
                "kind": "integration",
                "depends_on": ["backend", "frontend"],
                "effects": {"reads": ["src"]},
                "expected_outputs": ["assembled-tree"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_id": "certify",
                "title": "Certify assembled candidate",
                "kind": "certification",
                "depends_on": ["assemble"],
                "effects": {"reads": ["src"]},
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
            },
        ],
        "max_in_flight": 2,
        "mutation_wip": {"max_tokens": 2},
    }


@pytest.fixture
def managed_bridge():
    store = ephemeral_store()
    _register_repository(store)
    resolver = _BaseResolver()
    packages = WorkPackageService(store, repository_verifier=_AdmissionAttestor())
    bridge = ManagedWorkPlanBridge(
        store,
        packages,
        base_resolver=resolver,
    )
    try:
        yield store, resolver, packages, bridge
    finally:
        store.close()


def test_preview_locks_repository_and_base_without_creating_work(managed_bridge) -> None:
    store, resolver, _packages, bridge = managed_bridge

    result = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship coordinated UI and API changes",
            "project": "mac",
            "package_id": "wp_dashboard",
        },
        source="model",
    ).to_dict()

    assert result["schema"] == "mac.dashboard.managed_work_plan.v1"
    assert result["mode"] == "managed"
    assert result["package_id"] == "wp_dashboard"
    assert result["repository"] == {
        "id": "projectrepo_mac",
        "name": "mac",
        "project": "mac",
    }
    assert result["planning_base_ref"] == "refs/heads/main"
    assert result["planning_base_sha"] == SHA
    assert result["plan_digest"].startswith("sha256:")
    assert result["topological_order"] == [
        "backend",
        "frontend",
        "assemble",
        "certify",
    ]
    assert result["activation"]["automatic"] is False
    assert resolver.calls[0][1] is None
    assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0
    encoded = json.dumps(result, sort_keys=True)
    assert "git@example.invalid" not in encoded
    assert "/missing/controller/checkout" not in encoded


def test_preview_uses_controller_attested_paths_for_real_parallel_mutations() -> None:
    store = ephemeral_store()
    try:
        _register_repository(store)
        namespace = {
            "status": "resolved",
            "case_sensitive": False,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        }
        bridge = ManagedWorkPlanBridge(
            store,
            WorkPackageService(store, repository_verifier=_AdmissionAttestor()),
            base_resolver=_BaseResolver(resource_namespace=namespace),
        )
        preview = bridge.preview(
            _proposal(),
            request={
                "goal": "Ship coordinated UI and API changes",
                "project": "mac",
                "package_id": "wp_parallel",
            },
        ).to_dict()

        assert preview["resource_namespace"] == {
            "case_sensitive": False,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
        }
        compiled = compile_work_package_plan(preview["plan"])
        mutations = [
            node for node in compiled.task_specs if node.node_type == "mutation"
        ]
        assert mutations
        assert all("repo:*" not in node.effects.exclusive for node in mutations)
    finally:
        store.close()


def test_accept_recompiles_and_atomically_admits_held_then_activation_is_explicit(
    managed_bridge,
) -> None:
    store, resolver, packages, bridge = managed_bridge
    preview = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship coordinated UI and API changes",
            "project": "mac",
            "package_id": "wp_dashboard",
        },
    )
    plan = preview.to_dict()["plan"]
    plan["nodes"][0]["title"] = "Implement edited backend"

    accepted = bridge.accept(
        plan,
        actor="operator",
        reason="operator accepted edited managed plan",
    ).to_dict()

    assert accepted["schema"] == "mac.dashboard.managed_work_plan_accept.v1"
    assert accepted["package"]["state"] == "admitted"
    assert accepted["held"] is True
    assert accepted["activation"] == {
        "required": True,
        "automatic": False,
        "expected_plan_version": 1,
        "expected_epoch": 1,
        "endpoint": "/work-packages/wp_dashboard/activate",
    }
    assert [call[1] for call in resolver.calls] == [None, "refs/heads/main"]
    rows = store.query_all("SELECT title, metadata FROM tasks ORDER BY title")
    assert any(row["title"] == "Implement edited backend" for row in rows)
    assert all(json_loads(row["metadata"], {})["no_dispatch"] is True for row in rows)

    active = packages.activate(
        "wp_dashboard",
        expected_plan_version=1,
        expected_epoch=1,
        actor="operator",
    )
    assert active.state == "active"
    backend = store.query_one("SELECT metadata FROM tasks WHERE title = ?", ("Implement edited backend",))
    assert "no_dispatch" not in json_loads(backend["metadata"], {})


def test_flat_dashboard_preview_projects_back_to_closed_compiler_plan(
    managed_bridge,
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    preview = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship through the flat dashboard form",
            "project": "mac",
            "package_id": "wp_flat_form",
        },
    ).to_dict()
    preview.pop("plan")

    plan = managed_plan_from_dashboard_accept(preview)

    assert plan["schema"] == "mac.work_package.plan.v1"
    assert plan["package_id"] == "wp_flat_form"
    assert plan["repository_id"] == "projectrepo_mac"
    assert "activation" not in plan
    assert "repository" not in plan
    accepted = bridge.accept(
        plan,
        actor="operator",
        reason="accepted flat dashboard form",
    )
    assert accepted.admission.package.state == "admitted"


def test_accept_fails_closed_when_canonical_base_moved(managed_bridge) -> None:
    store, resolver, _packages, bridge = managed_bridge
    plan = bridge.preview(
        _proposal(),
        request={
            "goal": "Ship safely",
            "project": "mac",
            "package_id": "wp_stale",
        },
    ).plan
    resolver.sha = "b" * 40

    with pytest.raises(ValidationError, match="canonical planning ref moved"):
        bridge.accept(plan, actor="operator", reason="stale acceptance")

    assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0


def test_single_task_retry_reuses_original_plan_after_canonical_base_moves(
    managed_bridge,
) -> None:
    store, resolver, _packages, bridge = managed_bridge
    task_id = "task_" + ("c" * 32)
    request = {
        "task_id": task_id,
        "title": "One exact change",
        "description": "Keep the admitted base stable across a retry.",
        "project": "mac",
        "repository_id": "projectrepo_mac",
        "priority": 1,
        "required_capabilities": ["python"],
        "metadata": {"no_decompose": True},
        "max_attempts": 3,
        "actor": "single-task-controller",
        "reason": "ordinary atomic admission",
    }

    first = bridge.admit_single_task(**request)
    original_calls = len(resolver.calls)
    resolver.sha = "b" * 40
    second = bridge.admit_single_task(**request)

    assert second.admission.created is False
    assert second.admission.plan_digest == first.admission.plan_digest
    assert second.admission.task_ids == first.admission.task_ids
    assert second.admission.base_attestation.planning_base_sha == SHA
    assert len(resolver.calls) == original_calls
    package = store.query_one(
        "SELECT root_task_id FROM work_packages WHERE id = ?",
        ("wp_fast_%s" % ("c" * 32),),
    )
    assert package["root_task_id"] == task_id

    changed = dict(request)
    changed["description"] = "Different work must not reuse the package."
    with pytest.raises(ValidationError, match="different intent"):
        bridge.admit_single_task(**changed)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda proposal: proposal["nodes"].pop(),
            "exactly one integration station and one certification station",
        ),
        (
            lambda proposal: proposal["nodes"][3].update({"depends_on": ["backend"]}),
            "certification station must depend directly",
        ),
        (
            lambda proposal: proposal["nodes"][2].update({"depends_on": ["backend"]}),
            "integration fan-in node",
        ),
        (
            lambda proposal: proposal["nodes"][0].pop("effects"),
            "must explicitly declare effects",
        ),
    ],
)
def test_managed_topology_and_contracts_fail_closed(
    managed_bridge,
    mutate,
    message: str,
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    proposal = _proposal()
    mutate(proposal)
    with pytest.raises(ValidationError, match=message):
        bridge.preview(
            proposal,
            request={"goal": "Ship safely", "project": "mac"},
        )


def test_model_cannot_choose_locked_identity_or_emit_secret_fields(managed_bridge) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    locked = _proposal()
    locked["repository_id"] = "model-selected"
    with pytest.raises(ValidationError, match="controller-owned fields"):
        bridge.preview(locked, request={"goal": "Ship", "project": "mac"})

    secret = _proposal()
    secret["metadata"] = {"api_token": "should-never-be-persisted"}
    with pytest.raises(ValidationError, match="secret-like field"):
        bridge.preview(secret, request={"goal": "Ship", "project": "mac"})


def test_repository_selection_is_fail_closed_when_project_is_ambiguous(
    managed_bridge,
) -> None:
    store, _resolver, _packages, bridge = managed_bridge
    _register_repository(
        store,
        repository_id="projectrepo_mac_two",
        name="mac-two",
        project="mac",
        source="git@example.invalid:org/mac-two.git",
    )
    with pytest.raises(ValidationError, match="ambiguous"):
        bridge.preview(
            _proposal(),
            request={"goal": "Ship", "project": "mac"},
        )


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def test_git_base_resolver_uses_remote_head_and_exact_requested_ref(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "planner@example.invalid")
    _git(work, "config", "user.name", "Planner Test")
    (work / "README.md").write_text("managed planning\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:refs/heads/trunk")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
    repository = {"id": "repo_local", "source": str(remote), "path": str(work)}
    resolver = GitCanonicalBaseResolver()

    default = resolver.resolve(repository)
    exact = resolver.resolve(repository, requested_ref="refs/heads/trunk")

    assert default == CanonicalRepositoryBase(
        repository_id="repo_local",
        planning_base_ref="refs/heads/trunk",
        planning_base_sha=sha,
        resource_namespace={
            "status": "resolved",
            "case_sensitive": False,
            "unicode_normalization": "NFC",
            "symlink_resolution": "resolved",
            "conflict_policy": "exact",
            "attestor": "git-tree-namespace-v1",
            "planning_base_sha": sha,
        },
    )
    assert exact == default
    contract_selected = resolver.resolve(
        {
            "id": "repo_contract",
            "source": "git@example.invalid:obsolete/repository.git",
            "path": str(work),
            "metadata": json.dumps(
                {
                    "repository_contract": {
                        "canonical_remote_url": str(remote),
                    }
                }
            ),
        },
        requested_ref="refs/heads/trunk",
    )
    assert contract_selected.planning_base_sha == sha
    with pytest.raises(ValidationError, match="resolution failed"):
        resolver.resolve(repository, requested_ref="refs/heads/missing")
    with pytest.raises(ValidationError, match="valid canonical repository"):
        resolver.resolve(
            {
                "id": "repo_credential",
                "source": "https://raw-token@example.invalid/org/repo.git",
            }
        )


def test_real_git_preview_and_admission_independently_attest_path_parallelism(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    _git(work, "config", "user.email", "planner@example.invalid")
    _git(work, "config", "user.name", "Planner Test")
    (work / "README.md").write_text("managed planning\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:refs/heads/main")

    store = ephemeral_store()
    try:
        _register_repository(store, source=str(remote))
        store.execute(
            "UPDATE project_repositories SET path = ? WHERE id = ?",
            (str(work), "projectrepo_mac"),
        )
        packages = WorkPackageService(store)
        bridge = ManagedWorkPlanBridge(store, packages)

        preview = bridge.preview(
            _proposal(),
            request={
                "goal": "Ship coordinated UI and API changes",
                "project": "mac",
                "planning_base_ref": "refs/heads/main",
                "package_id": "wp_real_namespace",
            },
        ).to_dict()
        accepted = bridge.accept(
            preview["plan"],
            actor="operator",
            reason="accept independently attested path namespace",
        ).to_dict()

        assert preview["resource_namespace"]["symlink_resolution"] == "resolved"
        assert accepted["package"]["state"] == "admitted"
        stored = store.query_one(
            "SELECT metadata FROM work_packages WHERE id = ?",
            ("wp_real_namespace",),
        )
        attestation = json_loads(stored["metadata"], {})["base_attestation"]
        assert attestation["resource_namespace"]["status"] == "resolved"
        assert attestation["resource_namespace"]["planning_base_sha"] == preview[
            "planning_base_sha"
        ]
    finally:
        store.close()


# --- Broadened secret scanning -------------------------------------------

_GHP_TOKEN = "ghp_" + "A" * 36
_GITHUB_PAT = "github_pat_" + "1" * 22 + "_" + "B" * 30
_GLPAT = "glpat-" + "C" * 20
_AWS_KEY = "AKIA" + "Q" * 16
_GOOGLE_KEY = "AIza" + "d" * 35
_SLACK_TOKEN = "xoxb-" + "1" * 12 + "-aBcDeF012345"
_STRIPE_KEY = "sk_live_" + "e" * 24
_PROVIDER_KEY = "sk-proj-" + "f" * 40
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123456"
_RSA_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0\n"
    "-----END RSA PRIVATE KEY-----"
)
_OPENSSH_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r\n"
_PKCS8_PRIVATE_KEY = "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBg\n"
_AUTH_URL = "https://octocat:s3cr3tpw@github.com/org/repo.git"
_XACCESS_URL = "https://x-access-token:" + _GHP_TOKEN + "@github.com/org/repo.git"
_NETRC = "machine github.com login octocat password s3cr3tpw"
_NETRC_LINE = "\nmachine github.com login octocat password s3cr3tpw\n"
_AUTHZ_HEADER = "Authorization: Bearer sometoken.value-1234567890"


_RAW_SECRET_CASES = [
    ("github_ghp", _GHP_TOKEN, "GitHub token"),
    ("github_pat", _GITHUB_PAT, "GitHub token"),
    ("gitlab_pat", _GLPAT, "GitLab token"),
    ("aws_key", _AWS_KEY, "AWS access key"),
    ("google_key", _GOOGLE_KEY, "Google API key"),
    ("slack_token", _SLACK_TOKEN, "Slack token"),
    ("stripe_key", _STRIPE_KEY, "Stripe secret key"),
    ("provider_key", _PROVIDER_KEY, "provider secret key"),
    ("jwt", _JWT, "JSON Web Token"),
    ("rsa_private_key", _RSA_PRIVATE_KEY, "private-key block"),
    ("openssh_private_key", _OPENSSH_PRIVATE_KEY, "private-key block"),
    ("pkcs8_private_key", _PKCS8_PRIVATE_KEY, "private-key block"),
    ("authenticated_url", _AUTH_URL, "authenticated git URL"),
    ("x_access_token_url", _XACCESS_URL, "authenticated git URL"),
    ("netrc", _NETRC, "netrc credential"),
    ("authorization_header", _AUTHZ_HEADER, "authorization header"),
]


@pytest.mark.parametrize(
    ("label", "secret", "_kind"),
    _RAW_SECRET_CASES,
    ids=[case[0] for case in _RAW_SECRET_CASES],
)
def test_preview_rejects_raw_secret_material_in_free_text(
    managed_bridge, label, secret, _kind
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    proposal = _proposal()
    # Embed inside ordinary planner prose to exercise the deterministic scan.
    injected = _NETRC_LINE if label == "netrc" else secret
    proposal["nodes"][0]["description"] = (
        "Change the backend slice.\n%s\nMore detail." % injected
    )
    with pytest.raises(ValidationError) as excinfo:
        bridge.preview(proposal, request={"goal": "Ship", "project": "mac"})
    # The redaction-safe error must never echo the matched secret span.
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize(
    ("label", "secret", "_kind"),
    _RAW_SECRET_CASES,
    ids=[case[0] for case in _RAW_SECRET_CASES],
)
def test_preview_rejects_raw_secret_material_deeply_nested(
    managed_bridge, label, secret, _kind
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    proposal = _proposal()
    injected = _NETRC_LINE if label == "netrc" else secret
    proposal["metadata"] = {
        "notes": [
            {"context": {"deep": ["harmless", {"leaked": injected}]}},
        ]
    }
    with pytest.raises(ValidationError) as excinfo:
        bridge.preview(proposal, request={"goal": "Ship", "project": "mac"})
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize(
    ("label", "secret", "_kind"),
    _RAW_SECRET_CASES,
    ids=[case[0] for case in _RAW_SECRET_CASES],
)
def test_accept_rejects_raw_secret_material_injected_after_preview(
    managed_bridge, label, secret, _kind
) -> None:
    store, _resolver, _packages, bridge = managed_bridge
    plan = bridge.preview(
        _proposal(),
        request={"goal": "Ship", "project": "mac", "package_id": "wp_secret"},
    ).plan
    # Simulate an operator edit that smuggles a raw secret into the held plan.
    injected = _NETRC_LINE if label == "netrc" else secret
    plan["nodes"][0]["description"] = "Edited backend note.\n%s\nDone." % injected
    with pytest.raises(ValidationError) as excinfo:
        bridge.accept(plan, actor="operator", reason="edited plan")
    assert secret not in str(excinfo.value)
    # Nothing may be materialized when acceptance fails closed.
    assert store.query_one("SELECT COUNT(*) AS n FROM work_packages")["n"] == 0
    assert store.query_one("SELECT COUNT(*) AS n FROM tasks")["n"] == 0


_BENIGN_STRINGS = [
    "Implement the backend slice under src/backend and run pytest tests/backend.",
    "Coordinate UI and API changes; see https://github.com/org/repo for context.",
    "The mutation_wip token bucket allows max_tokens of 2 concurrent mutations.",
    "Rotate the deploy credential via the secret-reference handle, not inline.",
    "Reference public docs at https://example.com/guide?page=2&tab=overview.",
    "Follow the AGPL-3.0 license header and keep authorization checks intact.",
    "Public certificate bundle -----BEGIN CERTIFICATE----- lives in deploy/.",
    "Short ids like ghp_short or AKIASHORT are not vendor-shaped secrets.",
    "Machine learning pipeline design notes and password reset UX copy.",
    "Clone with git clone https://github.com/org/repo.git before building.",
]


@pytest.mark.parametrize("benign", _BENIGN_STRINGS)
def test_preview_admits_benign_prose_without_false_positive(
    managed_bridge, benign
) -> None:
    _store, _resolver, _packages, bridge = managed_bridge
    proposal = _proposal()
    proposal["nodes"][0]["description"] = benign
    proposal["metadata"] = {"analyst_notes": [benign, {"detail": benign}]}
    # Must compile and preview cleanly: benign prose is not secret material.
    result = bridge.preview(
        proposal, request={"goal": "Ship", "project": "mac"}
    ).to_dict()
    assert result["mode"] == "managed"


def test_secret_scan_is_identical_at_preview_and_acceptance(managed_bridge) -> None:
    """The same scalar must be rejected on both paths with the same contract."""

    _store, _resolver, _packages, bridge = managed_bridge
    proposal = _proposal()
    proposal["nodes"][0]["description"] = "auth via %s" % _GHP_TOKEN
    with pytest.raises(ValidationError) as preview_err:
        bridge.preview(proposal, request={"goal": "Ship", "project": "mac"})

    clean = bridge.preview(
        _proposal(),
        request={"goal": "Ship", "project": "mac", "package_id": "wp_parity"},
    ).plan
    clean["nodes"][0]["description"] = "auth via %s" % _GHP_TOKEN
    with pytest.raises(ValidationError) as accept_err:
        bridge.accept(clean, actor="operator", reason="edited plan")

    assert str(preview_err.value) == str(accept_err.value)
    assert _GHP_TOKEN not in str(preview_err.value)


def test_secret_scan_never_echoes_matched_value_across_families() -> None:
    from mac.work_plan_admission import _reject_secret_material

    for label, secret, _kind in _RAW_SECRET_CASES:
        injected = _NETRC_LINE if label == "netrc" else secret
        payload = {"nodes": [{"description": "prefix\n%s\nsuffix" % injected}]}
        with pytest.raises(ValidationError) as excinfo:
            _reject_secret_material(payload)
        message = str(excinfo.value)
        assert secret not in message
        # The contract message is a fixed redaction-safe string.
        assert message.startswith("managed work plan may not contain")
