"""Pure-Python tests for the mac-k8s-runner Phase 4 logic.

Exercises ``build_job_spec`` and ``claim_and_launch_one`` with fake
mac-api + fake K8s clients so the unit tests don't need a live cluster.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from mac.k8s.runner import (
    DEFAULT_TASK_IMAGE,
    RunnerConfig,
    _job_is_terminal,
    _job_name_for,
    _lease_renewal_loop,
    _resolve_active_deadline,
    _resolve_agent_id_for_role,
    _resolve_executor_for_role,
    _resolve_task_image,
    _resolve_task_role,
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
    def __init__(
        self,
        *,
        fail: bool = False,
        read_responses: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.created: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self._fail = fail
        self.last_namespace: Optional[str] = None
        # If a list is provided, read() returns successive entries and
        # latches on the last one. Default = report immediate terminal
        # status so the renewal thread (spawned by claim_and_launch_one)
        # exits on its first tick.
        self._read_responses = read_responses or [
            {"status": {"succeeded": 1, "failed": 0}}
        ]
        self.read_calls: List[Dict[str, str]] = []

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

    def read(self, namespace: str, name: str) -> Dict[str, Any]:
        self.read_calls.append({"namespace": namespace, "name": name})
        if not self._read_responses:
            return {"status": {"succeeded": 1, "failed": 0}}
        if len(self._read_responses) == 1:
            return self._read_responses[0]
        return self._read_responses.pop(0)


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


# ----------------------------------------------------------------------
# PR2c: lease delegation. claim_and_launch_one delegates the lease to
# the resolved role agent so the Job pod's start_task /
# submit_for_review calls satisfy the hub's authorisation check.
# ----------------------------------------------------------------------

def _role_runner_cfg() -> Any:
    """Variant of _cfg with role maps populated so the resolved role
    agent differs from the dispatcher (cfg.agent_id == 'runner-1')."""
    base = _cfg()
    base.role_images = {"python-coder": "ghcr.io/x/coder:latest"}
    base.role_agent_ids = {"python-coder": "mac-worker-python-coder"}
    base.role_executors = {
        "python-coder": "/usr/local/bin/mac-task-executor-codex"
    }
    base.capability_role_aliases = {"python": "python-coder"}
    return base


def test_claim_and_launch_delegates_lease_when_role_agent_differs() -> None:
    mac = _FakeMac(
        claim_response={
            "task": _task(required_capabilities=["python"]),
            "lease": _lease(),
        }
    )
    jobs = _FakeJobs()
    cfg = _role_runner_cfg()

    result = claim_and_launch_one(mac, jobs, cfg)
    assert result is not None
    assert result["status"] == "launched"

    delegate_posts = [
        p for p in mac.posted if p["path"].endswith("/delegate")
    ]
    assert len(delegate_posts) == 1, "exactly one delegate call expected"
    body = delegate_posts[0]["body"]
    assert body == {
        "agent_id": cfg.agent_id,
        "to_agent_id": "mac-worker-python-coder",
    }
    # Path includes the lease id.
    assert delegate_posts[0]["path"] == "/leases/lease-xyz/delegate"


def test_claim_and_launch_skips_delegate_when_role_agent_is_dispatcher() -> None:
    # Unaliased capability ⇒ no role hit ⇒ job_agent_id == cfg.agent_id
    # ⇒ no delegation needed (and none should be issued).
    mac = _FakeMac(
        claim_response={
            "task": _task(required_capabilities=["unknown-cap"]),
            "lease": _lease(),
        }
    )
    jobs = _FakeJobs()
    cfg = _role_runner_cfg()  # has aliases but not for "unknown-cap"

    result = claim_and_launch_one(mac, jobs, cfg)
    assert result is not None
    assert result["status"] == "launched"
    delegate_posts = [
        p for p in mac.posted if p["path"].endswith("/delegate")
    ]
    assert delegate_posts == []


def test_claim_and_launch_proceeds_when_delegate_fails() -> None:
    """Delegation failure must NOT crash the runner; the Job is still
    launched and the hub-side lease-expiry path handles cleanup."""

    class _FailDelegateMac(_FakeMac):
        def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
            if path.endswith("/delegate"):
                self.posted.append({"path": path, "body": body})
                raise RuntimeError("simulated hub 500")
            return super().post(path, body)

    mac = _FailDelegateMac(
        claim_response={
            "task": _task(required_capabilities=["python"]),
            "lease": _lease(),
        }
    )
    jobs = _FakeJobs()
    cfg = _role_runner_cfg()

    result = claim_and_launch_one(mac, jobs, cfg)
    assert result is not None
    assert result["status"] == "launched"
    assert len(jobs.created) == 1  # Job still created
    delegate_posts = [
        p for p in mac.posted if p["path"].endswith("/delegate")
    ]
    assert len(delegate_posts) == 1  # delegation was attempted


# ----------------------------------------------------------------------
# Role specialisation: _resolve_task_role / _resolve_agent_id_for_role /
# _resolve_executor_for_role / role-aware build_job_spec.
# ----------------------------------------------------------------------

def _env_value(env: List[Dict[str, Any]], name: str) -> Optional[Any]:
    for e in env:
        if e.get("name") == name:
            return e.get("value")
    return None


def _env_names(env: List[Dict[str, Any]]) -> List[str]:
    return [e["name"] for e in env]


def _role_cfg(**overrides: Any) -> RunnerConfig:
    """Variant of _cfg with all four role maps populated."""
    base = _cfg()
    base.role_images = {
        "python-coder": "ghcr.io/x/coder:latest",
        "python-reviewer": "ghcr.io/x/reviewer:latest",
    }
    base.role_agent_ids = {
        "python-coder": "mac-worker-python-coder",
        "python-reviewer": "mac-worker-python-reviewer",
    }
    base.role_executors = {
        "python-coder": "/usr/local/bin/mac-task-executor-codex",
        "python-reviewer": "/usr/local/bin/mac-task-executor-codex-review",
    }
    base.capability_role_aliases = {
        "python": "python-coder",
        "review": "python-reviewer",
    }
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestResolveTaskRole:
    def test_explicit_required_role_wins(self) -> None:
        task = _task(metadata={"required_role": "python-coder"})
        assert _resolve_task_role(task, _role_cfg()) == "python-coder"

    def test_capability_alias_first_match(self) -> None:
        task = _task(required_capabilities=["python", "ops"])
        assert _resolve_task_role(task, _role_cfg()) == "python-coder"

    def test_capability_alias_declared_order(self) -> None:
        # Both caps in the alias map; the first one wins.
        task = _task(required_capabilities=["review", "python"])
        assert _resolve_task_role(task, _role_cfg()) == "python-reviewer"

    def test_unaliased_capability_returns_none(self) -> None:
        # Codex review M1: no naked first-capability fallback.
        task = _task(required_capabilities=["unknown-cap"])
        cfg = _cfg()  # empty alias map
        assert _resolve_task_role(task, cfg) is None

    def test_empty_alias_map_returns_none_even_for_known_capability(self) -> None:
        task = _task(required_capabilities=["python", "ops"])
        cfg = _cfg()  # default _cfg has no aliases set
        assert _resolve_task_role(task, cfg) is None

    def test_explicit_role_overrides_alias(self) -> None:
        task = _task(
            required_capabilities=["review"],
            metadata={"required_role": "python-coder"},
        )
        assert _resolve_task_role(task, _role_cfg()) == "python-coder"


class TestResolveAgentIdForRole:
    def test_role_hit_returns_role_agent(self) -> None:
        cfg = _role_cfg()
        assert (
            _resolve_agent_id_for_role("python-coder", cfg)
            == "mac-worker-python-coder"
        )

    def test_no_role_returns_dispatcher(self) -> None:
        cfg = _role_cfg()
        assert _resolve_agent_id_for_role(None, cfg) == cfg.agent_id

    def test_unmapped_role_falls_back_to_dispatcher(self) -> None:
        cfg = _role_cfg()
        assert _resolve_agent_id_for_role("ghost-role", cfg) == cfg.agent_id


class TestResolveExecutorForRole:
    def test_role_hit_returns_executor(self) -> None:
        cfg = _role_cfg()
        assert (
            _resolve_executor_for_role("python-coder", cfg)
            == "/usr/local/bin/mac-task-executor-codex"
        )

    def test_no_role_returns_none(self) -> None:
        assert _resolve_executor_for_role(None, _role_cfg()) is None

    def test_unmapped_role_returns_none(self) -> None:
        # Lets the Job pod's executor pick up MAC_TASK_EXECUTOR_COMMAND
        # from some other source (or use its built-in default).
        assert _resolve_executor_for_role("ghost-role", _role_cfg()) is None


class TestRoleAwareBuildJobSpec:
    def test_no_role_preserves_default_behaviour(self) -> None:
        # Default _cfg has empty role maps; behaviour must be
        # bit-for-bit identical to today.
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "runner-1"
        # MAC_AGENT_ROLE is now always emitted, value "" when no role.
        assert _env_value(env, "MAC_AGENT_ROLE") == ""
        # MAC_TASK_EXECUTOR_COMMAND is NOT emitted when no role mapping.
        assert "MAC_TASK_EXECUTOR_COMMAND" not in _env_names(env)
        # Labels carry the default-role + dispatcher agent_id.
        labels = spec["metadata"]["labels"]
        assert labels["mac.role"] == "default"
        assert labels["mac.agent.id"] == _sanitize_dns_label("runner-1")
        # Image stays as the cfg default.
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"]
            == DEFAULT_TASK_IMAGE
        )

    def test_explicit_required_role_populates_everything(self) -> None:
        task = _task(metadata={"required_role": "python-coder"})
        spec = build_job_spec(task, _lease(), _role_cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "mac-worker-python-coder"
        assert _env_value(env, "MAC_AGENT_ROLE") == "python-coder"
        assert (
            _env_value(env, "MAC_TASK_EXECUTOR_COMMAND")
            == "/usr/local/bin/mac-task-executor-codex"
        )
        labels = spec["metadata"]["labels"]
        assert labels["mac.role"] == "python-coder"
        assert labels["mac.agent.id"] == "mac-worker-python-coder"
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"]
            == "ghcr.io/x/coder:latest"
        )

    def test_capability_alias_routes_correctly(self) -> None:
        task = _task(required_capabilities=["python"])
        spec = build_job_spec(task, _lease(), _role_cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "mac-worker-python-coder"
        assert _env_value(env, "MAC_AGENT_ROLE") == "python-coder"
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"]
            == "ghcr.io/x/coder:latest"
        )

    def test_unknown_capability_and_empty_alias_map_falls_through(self) -> None:
        # Codex M1: a capability not in the alias map must NOT mint a
        # role. Behaviour collapses to today's defaults.
        cfg = _cfg()  # NB: empty alias/role maps
        task = _task(required_capabilities=["unknown-cap"])
        spec = build_job_spec(task, _lease(), cfg)
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == cfg.agent_id
        assert _env_value(env, "MAC_AGENT_ROLE") == ""
        assert "MAC_TASK_EXECUTOR_COMMAND" not in _env_names(env)
        labels = spec["metadata"]["labels"]
        assert labels["mac.role"] == "default"
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"]
            == cfg.default_image
        )

    def test_per_task_runtime_image_still_overrides_role_image(self) -> None:
        task = _task(
            metadata={
                "required_role": "python-coder",
                "runtime": {"image": "ghcr.io/x/y@sha256:override"},
            }
        )
        spec = build_job_spec(task, _lease(), _role_cfg())
        # runtime.image wins over role_images.
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"]
            == "ghcr.io/x/y@sha256:override"
        )
        # But agent_id + role env still reflect the role.
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "mac-worker-python-coder"
        assert _env_value(env, "MAC_AGENT_ROLE") == "python-coder"


class TestRunnerConfigFromEnvRoles:
    def test_json_envs_decoded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv(
            "MAC_RUNNER_ROLE_IMAGES",
            '{"python-coder": "img-a", "python-reviewer": "img-b"}',
        )
        monkeypatch.setenv(
            "MAC_RUNNER_ROLE_AGENT_IDS",
            '{"python-coder": "agent-a"}',
        )
        monkeypatch.setenv(
            "MAC_RUNNER_ROLE_EXECUTORS",
            '{"python-coder": "/bin/codex"}',
        )
        monkeypatch.setenv(
            "MAC_RUNNER_CAPABILITY_ROLE_ALIASES",
            '{"python": "python-coder"}',
        )
        cfg = RunnerConfig.from_env()
        assert cfg.role_images == {
            "python-coder": "img-a",
            "python-reviewer": "img-b",
        }
        assert cfg.role_agent_ids == {"python-coder": "agent-a"}
        assert cfg.role_executors == {"python-coder": "/bin/codex"}
        assert cfg.capability_role_aliases == {"python": "python-coder"}

    def test_malformed_json_falls_back_to_empty(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv("MAC_RUNNER_ROLE_IMAGES", "{not-json")
        cfg = RunnerConfig.from_env()
        assert cfg.role_images == {}

    def test_unset_envs_default_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        cfg = RunnerConfig.from_env()
        assert cfg.role_images == {}
        assert cfg.role_agent_ids == {}
        assert cfg.role_executors == {}
        assert cfg.capability_role_aliases == {}


# ----------------------------------------------------------------------
# Runner-side lease renewal loop (spec §6.3).
# ----------------------------------------------------------------------

class TestJobIsTerminal:
    def test_succeeded_one_is_terminal(self) -> None:
        assert _job_is_terminal({"status": {"succeeded": 1}}) is True

    def test_failed_one_is_terminal(self) -> None:
        assert _job_is_terminal({"status": {"failed": 1}}) is True

    def test_zero_counts_are_not_terminal(self) -> None:
        assert _job_is_terminal({"status": {"succeeded": 0, "failed": 0}}) is False

    def test_missing_status_is_not_terminal(self) -> None:
        assert _job_is_terminal({}) is False
        assert _job_is_terminal({"status": None}) is False


class _RenewalFakeMac:
    """A minimal fake just for the renewal loop tests."""

    def __init__(self, *, fail_post: bool = False) -> None:
        self.posts: List[Dict[str, Any]] = []
        self._fail = fail_post

    def post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        self.posts.append({"path": path, "body": body})
        if self._fail:
            raise RuntimeError("renew API down")
        return {"ok": True}

    def get(self, path: str) -> Dict[str, Any]:
        return {}


class _RenewalFakeJobs:
    """A fake for the renewal loop that streams Job statuses."""

    def __init__(self, statuses: List[Dict[str, Any]]) -> None:
        self._statuses = list(statuses)
        self.read_calls = 0

    def create(self, namespace: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_active(self, namespace: str, label_selector: str) -> List[Dict[str, Any]]:
        return []

    def delete(self, namespace: str, name: str) -> None:
        return None

    def read(self, namespace: str, name: str) -> Dict[str, Any]:
        self.read_calls += 1
        if len(self._statuses) == 1:
            return self._statuses[0]
        return self._statuses.pop(0)


def test_renewal_loop_stops_on_succeeded_status() -> None:
    """Loop runs through a few iterations and exits when status.succeeded≥1."""
    mac = _RenewalFakeMac()
    jobs = _RenewalFakeJobs(
        statuses=[
            {"status": {"succeeded": 0, "failed": 0}},
            {"status": {"succeeded": 0, "failed": 0}},
            {"status": {"succeeded": 1, "failed": 0}},
        ]
    )
    cfg = _cfg(lease_renew_interval_seconds=0.0, job_poll_interval_seconds=0.001)
    stop = threading.Event()
    _lease_renewal_loop(
        mac,
        jobs,
        cfg,
        namespace="mac",
        job_name="mac-task-x",
        lease_id="lease-abc",
        agent_id=cfg.agent_id,
        stop_event=stop,
        sleeper=lambda _s: None,
    )
    # Reached terminal on third read.
    assert jobs.read_calls == 3
    # Renewed under the dispatcher identity.
    assert mac.posts, "renewal loop should renew at least once"
    for p in mac.posts:
        assert p["path"].endswith("/renew")
        assert p["body"] == {"agent_id": cfg.agent_id}


def test_renewal_loop_stops_on_failed_status() -> None:
    mac = _RenewalFakeMac()
    jobs = _RenewalFakeJobs(statuses=[{"status": {"succeeded": 0, "failed": 1}}])
    cfg = _cfg(lease_renew_interval_seconds=0.0, job_poll_interval_seconds=0.001)
    stop = threading.Event()
    _lease_renewal_loop(
        mac,
        jobs,
        cfg,
        namespace="mac",
        job_name="mac-task-y",
        lease_id="lease-fff",
        agent_id=cfg.agent_id,
        stop_event=stop,
        sleeper=lambda _s: None,
    )
    # Loop returns BEFORE renewing because the very first read is
    # terminal — by design, terminal short-circuits both renew + sleep.
    assert jobs.read_calls == 1
    assert mac.posts == []


def test_renewal_loop_tolerates_transient_post_failure() -> None:
    """A failing /renew POST must NOT crash the goroutine."""
    mac = _RenewalFakeMac(fail_post=True)
    jobs = _RenewalFakeJobs(
        statuses=[
            {"status": {"succeeded": 0, "failed": 0}},
            {"status": {"succeeded": 0, "failed": 0}},
            {"status": {"succeeded": 1, "failed": 0}},
        ]
    )
    cfg = _cfg(lease_renew_interval_seconds=0.0, job_poll_interval_seconds=0.001)
    stop = threading.Event()
    _lease_renewal_loop(
        mac,
        jobs,
        cfg,
        namespace="mac",
        job_name="mac-task-z",
        lease_id="lease-zzz",
        agent_id=cfg.agent_id,
        stop_event=stop,
        sleeper=lambda _s: None,
    )
    # Still saw all three reads and attempted renews on the first two.
    assert jobs.read_calls == 3
    # We attempted at least one renew even though they failed.
    assert mac.posts, "renewal POST should have been attempted"


def test_renewal_loop_tolerates_read_failure() -> None:
    """A failing read_namespaced_job must NOT crash the goroutine."""

    class _FlakyJobs:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, namespace: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
            raise NotImplementedError

        def list_active(
            self, namespace: str, label_selector: str
        ) -> List[Dict[str, Any]]:
            return []

        def delete(self, namespace: str, name: str) -> None:
            return None

        def read(self, namespace: str, name: str) -> Dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient kube error")
            return {"status": {"succeeded": 1, "failed": 0}}

    mac = _RenewalFakeMac()
    jobs = _FlakyJobs()
    cfg = _cfg(lease_renew_interval_seconds=0.0, job_poll_interval_seconds=0.001)
    stop = threading.Event()
    _lease_renewal_loop(
        mac,
        jobs,
        cfg,
        namespace="mac",
        job_name="mac-task-f",
        lease_id="lease-fl",
        agent_id=cfg.agent_id,
        stop_event=stop,
        sleeper=lambda _s: None,
    )
    # Second read was terminal; loop exited cleanly.
    assert jobs.calls == 2


def test_renewal_loop_honours_stop_event() -> None:
    """Cancelling via stop_event exits promptly."""

    mac = _RenewalFakeMac()
    # Never terminal — only stop_event can break this loop.
    jobs = _RenewalFakeJobs(statuses=[{"status": {"succeeded": 0, "failed": 0}}])
    cfg = _cfg(lease_renew_interval_seconds=0.0, job_poll_interval_seconds=0.05)
    stop = threading.Event()

    thread = threading.Thread(
        target=_lease_renewal_loop,
        kwargs={
            "mac": mac,
            "k8s": jobs,
            "cfg": cfg,
            "namespace": "mac",
            "job_name": "mac-task-s",
            "lease_id": "lease-s",
            "agent_id": cfg.agent_id,
            "stop_event": stop,
            "sleeper": time.sleep,
        },
        daemon=True,
    )
    thread.start()
    # Let the loop tick a few times then cancel.
    time.sleep(0.15)
    stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive(), "stop_event must terminate the loop"
    assert jobs.read_calls >= 1
    assert mac.posts, "at least one renew should have fired before stop"


def test_claim_and_launch_spawns_renewal_thread() -> None:
    """End-to-end: a successful claim_and_launch_one starts a renewal
    thread that POSTs /leases/{id}/renew with the dispatcher's
    agent_id (matching today's renewal-body semantics)."""
    mac = _FakeMac(claim_response={"task": _task(), "lease": _lease()})
    # Use an always-non-terminal status with renew-interval 0 and a
    # tiny poll interval so the thread issues at least one renew
    # before we cancel.
    jobs = _FakeJobs(
        read_responses=[{"status": {"succeeded": 0, "failed": 0}}]
    )
    cfg = _cfg(lease_renew_interval_seconds=0.0, job_poll_interval_seconds=0.01)

    # Track threads started during the call.
    pre_threads = {t.ident for t in threading.enumerate()}
    result = claim_and_launch_one(mac, jobs, cfg)
    assert result is not None and result["status"] == "launched"

    # Give the renewal thread a chance to tick at least once.
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        renew_posts = [p for p in mac.posted if p["path"].endswith("/renew")]
        if renew_posts:
            break
        time.sleep(0.02)
    renew_posts = [p for p in mac.posted if p["path"].endswith("/renew")]
    assert renew_posts, "renewal thread must POST /leases/{id}/renew"
    # Renewal body MUST use the dispatcher's agent_id so mac-api sees
    # the same identity as today's in-Job renewal for unspecialised
    # tasks.
    assert renew_posts[0]["body"] == {"agent_id": cfg.agent_id}
    # Path encodes the lease id we claimed.
    assert renew_posts[0]["path"] == "/leases/lease-xyz/renew"

    # Stop any lingering renewal threads (best-effort).
    for t in threading.enumerate():
        if t.ident in pre_threads:
            continue
        ev = getattr(t, "stop_event", None)
        if ev is not None:
            ev.set()
