"""Pure-Python tests for the mac-k8s-runner Phase 4 logic.

Exercises ``build_job_spec`` and ``claim_and_launch_one`` with fake
mac-api + fake K8s clients so the unit tests don't need a live cluster.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from mac.k8s.runner import (
    DEFAULT_TASK_IMAGE,
    RunnerConfig,
    _job_name_for,
    _resolve_active_deadline,
    _resolve_task_image,
    _sanitize_dns_label,
    build_job_spec,
    claim_and_launch_one,
)


def _task(id_: str = "task-abc", **overrides: Any) -> Dict[str, Any]:
    base = {
        "id": id_,
        "title": "Build widget",
        "state": "open",
        "required_capabilities": ["python", "ops"],
        "metadata": {},
    }
    base.update(overrides)
    return base


def _lease(id_: str = "lease-xyz") -> Dict[str, Any]:
    return {"id": id_, "task_id": "task-abc", "status": "active"}


def _cfg(**overrides: Any) -> RunnerConfig:
    base = RunnerConfig(
        mac_url="http://mac-api.mac.svc:80",
        agent_id="runner-1",
        namespace="mac",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# ----------------------------------------------------------------------
# build_job_spec
# ----------------------------------------------------------------------

class TestBuildJobSpec:
    def test_basic_shape(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        assert spec["apiVersion"] == "batch/v1"
        assert spec["kind"] == "Job"
        assert spec["metadata"]["namespace"] == "mac"
        labels = spec["metadata"]["labels"]
        assert labels["app.kubernetes.io/managed-by"] == "mac-k8s-runner"
        assert labels["mac.task.id"] == "task-abc"
        assert labels["mac.lease.id"] == "lease-xyz"

    def test_job_name_is_dns_safe(self) -> None:
        spec = build_job_spec(
            _task(id_="ABC_Task!1"), _lease(id_="lease-WITH/MIXED:chars"), _cfg()
        )
        name = spec["metadata"]["name"]
        assert all(c.islower() or c.isdigit() or c == "-" for c in name)
        assert not name.startswith("-")
        assert len(name) <= 63

    def test_backoff_limit_is_zero(self) -> None:
        # mac-api owns retries via lease-expiry. K8s Job backoffLimit
        # must be 0 so the same Job is not silently retried by the
        # Job controller.
        spec = build_job_spec(_task(), _lease(), _cfg())
        assert spec["spec"]["backoffLimit"] == 0

    def test_active_deadline_default_and_override(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg(active_deadline_seconds=900))
        assert spec["spec"]["activeDeadlineSeconds"] == 900
        # Per-task override via metadata.k8s.active_deadline_seconds
        spec_override = build_job_spec(
            _task(metadata={"k8s": {"active_deadline_seconds": 60}}),
            _lease(),
            _cfg(),
        )
        assert spec_override["spec"]["activeDeadlineSeconds"] == 60

    def test_active_deadline_floor(self) -> None:
        spec = build_job_spec(
            _task(metadata={"k8s": {"active_deadline_seconds": 5}}),
            _lease(),
            _cfg(),
        )
        assert spec["spec"]["activeDeadlineSeconds"] >= 60

    def test_ttl_seconds_after_finished_set(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        assert spec["spec"]["ttlSecondsAfterFinished"] > 0

    def test_pod_security_context_hardened(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        pod = spec["spec"]["template"]["spec"]
        assert pod["restartPolicy"] == "Never"
        assert pod["automountServiceAccountToken"] is False
        sc = pod["securityContext"]
        assert sc["runAsNonRoot"] is True
        container = pod["containers"][0]
        csec = container["securityContext"]
        assert csec["readOnlyRootFilesystem"] is True
        assert csec["allowPrivilegeEscalation"] is False
        assert csec["capabilities"]["drop"] == ["ALL"]

    def test_container_env_has_required_vars(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {e["name"] for e in env}
        for required in (
            "MAC_URL",
            "MAC_TASK_ID",
            "MAC_LEASE_ID",
            "MAC_AGENT_ID",
            "MAC_WORKER_TOKEN",
            "MAC_SECRET_KEY",
        ):
            assert required in names

    def test_token_pulled_from_named_secret(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        tok = next(e for e in env if e["name"] == "MAC_WORKER_TOKEN")
        assert tok["valueFrom"]["secretKeyRef"]["name"] == "mac-api-config"

    def test_env_secret_name_overrides_both_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The home-ops case: a single ExternalSecret target `mac-secret`
        # provides both MAC_WORKER_TOKEN and MAC_SECRET_KEY. The runner
        # must pick that up via MAC_RUNNER_TASK_SECRET_NAME without the
        # operator having to set per-key vars.
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv("MAC_RUNNER_TASK_SECRET_NAME", "mac-secret")
        cfg = RunnerConfig.from_env()
        assert cfg.secret_name_for_token == "mac-secret"
        assert cfg.secret_name_for_secret_key == "mac-secret"
        spec = build_job_spec(_task(), _lease(), cfg)
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        for name in ("MAC_WORKER_TOKEN", "MAC_SECRET_KEY"):
            ref = next(e for e in env if e["name"] == name)["valueFrom"]["secretKeyRef"]
            assert ref["name"] == "mac-secret"

    def test_env_per_key_secret_name_overrides_individually(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv("MAC_RUNNER_TASK_TOKEN_SECRET_NAME", "tok-secret")
        monkeypatch.setenv("MAC_RUNNER_TASK_SECRET_KEY_SECRET_NAME", "key-secret")
        cfg = RunnerConfig.from_env()
        assert cfg.secret_name_for_token == "tok-secret"
        assert cfg.secret_name_for_secret_key == "key-secret"

    def test_image_resolution_default(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"]
            == DEFAULT_TASK_IMAGE
        )

    def test_image_resolution_task_override(self) -> None:
        task = _task(metadata={"runtime": {"image": "ghcr.io/x/y@sha256:abc"}})
        spec = build_job_spec(task, _lease(), _cfg())
        assert spec["spec"]["template"]["spec"]["containers"][0]["image"] == (
            "ghcr.io/x/y@sha256:abc"
        )

    def test_image_resolution_k8s_escape_hatch(self) -> None:
        task = _task(metadata={"k8s": {"image": "ghcr.io/x/y@sha256:def"}})
        spec = build_job_spec(task, _lease(), _cfg())
        assert spec["spec"]["template"]["spec"]["containers"][0]["image"] == (
            "ghcr.io/x/y@sha256:def"
        )

    def test_command_runs_mac_task_runner_binary(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        cmd = spec["spec"]["template"]["spec"]["containers"][0]["command"]
        assert cmd == ["mac-task-runner"]


class TestSanitizeDnsLabel:
    @pytest.mark.parametrize(
        ("inp", "expected_prefix"),
        [
            ("ABC", "abc"),
            ("Foo_Bar", "foo-bar"),
            ("123hello", "123hello"),
            ("--leading-dash", "leading-dash"),
        ],
    )
    def test_basic(self, inp: str, expected_prefix: str) -> None:
        assert _sanitize_dns_label(inp).startswith(expected_prefix)

    def test_caps_at_63(self) -> None:
        long = "x" * 200
        assert len(_sanitize_dns_label(long)) <= 63


# ----------------------------------------------------------------------
# claim_and_launch_one
# ----------------------------------------------------------------------

class _FakeMac:
    def __init__(self, claim_response: Optional[Dict[str, Any]] = None) -> None:
        self._claim = claim_response
        self.posted: List[Dict[str, Any]] = []
        self.gotten: List[str] = []

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.posted.append({"path": path, "body": body})
        if path.endswith("/claim-next"):
            return self._claim or {}
        return {}

    def get(self, path: str) -> Dict[str, Any]:
        self.gotten.append(path)
        return {}


class _FakeJobs:
    def __init__(self, *, fail: bool = False) -> None:
        self.created: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self._fail = fail
        self.last_namespace: Optional[str] = None

    def create(self, namespace: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        self.last_namespace = namespace
        if self._fail:
            raise RuntimeError("k8s API down")
        self.created.append(manifest)
        return {"metadata": {"uid": "uid-123", "name": manifest["metadata"]["name"]}}

    def list_active(self, namespace: str, label_selector: str) -> List[Dict[str, Any]]:
        return []

    def delete(self, namespace: str, name: str) -> None:
        self.deleted.append(name)


def test_claim_and_launch_returns_none_when_nothing_ready() -> None:
    mac = _FakeMac(claim_response=None)
    jobs = _FakeJobs()
    assert claim_and_launch_one(mac, jobs, _cfg()) is None


def test_claim_and_launch_happy_path() -> None:
    mac = _FakeMac(claim_response={"task": _task(), "lease": _lease()})
    jobs = _FakeJobs()
    result = claim_and_launch_one(mac, jobs, _cfg())
    assert result is not None
    assert result["status"] == "launched"
    assert result["task_id"] == "task-abc"
    assert result["lease_id"] == "lease-xyz"
    assert result["job_uid"] == "uid-123"
    assert jobs.last_namespace == "mac"
    assert len(jobs.created) == 1


def test_claim_and_launch_passes_capability_filter() -> None:
    mac = _FakeMac(claim_response=None)
    jobs = _FakeJobs()
    cfg = _cfg(capability_filter=["python", "ops"])
    claim_and_launch_one(mac, jobs, cfg)
    body = mac.posted[0]["body"]
    assert body["capabilities"] == ["python", "ops"]


def test_claim_and_launch_releases_lease_on_k8s_failure() -> None:
    mac = _FakeMac(claim_response={"task": _task(), "lease": _lease()})
    jobs = _FakeJobs(fail=True)
    result = claim_and_launch_one(mac, jobs, _cfg())
    assert result is not None
    assert result["status"] == "k8s_create_failed"
    # Best-effort release: a transition POST should have been issued.
    release_posts = [
        p for p in mac.posted if p["path"].endswith("/transition")
    ]
    assert release_posts, "lease should be released to open on k8s failure"
    assert release_posts[0]["body"]["target_state"] == "open"


def test_claim_and_launch_refuses_assignment_without_lease() -> None:
    mac = _FakeMac(claim_response={"task": _task(), "lease": {}})
    jobs = _FakeJobs()
    result = claim_and_launch_one(mac, jobs, _cfg())
    assert result is None
    assert jobs.created == []
