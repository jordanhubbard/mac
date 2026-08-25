from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from mac.k8s.runner import (
    DEFAULT_TASK_IMAGE,
    MAC_CONTAINER_GID,
    MAC_CONTAINER_UID,
    READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON,
    RunnerConfig,
    _job_is_terminal,
    _job_name_for,
    _lease_renewal_loop,
    _resolve_active_deadline,
    _resolve_agent_id_for_role,
    _resolve_attestation_key_secret_for_role,
    _resolve_executor_for_role,
    _resolve_task_image,
    _resolve_task_role,
    _sanitize_dns_label,
    build_job_spec,
    build_review_job_spec,
    check_dispatcher_capabilities,
    claim_and_launch_one,
    claim_and_launch_review_one,
)

# imports relocated from test_k8s_runner_edges.py
from types import SimpleNamespace
from mac.k8s import runner


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


def _dispatcher_yaml() -> Dict[str, Any]:
    return {
        "machine": {
            "machine_id": "mac-runner",
            "hostname": "mac-runner.ai.svc.cluster.local",
            "labels": {"kind": "k8s-deployment"},
        },
        "agent": {
            "agent_id": "mac-runner",
            "name": "mac-runner",
            "capabilities": ["ops", "python", "review"],
        },
    }


def _empty_config_yaml() -> Dict[str, Any]:
    return {
        "mac_url": "http://mac-api.mac.svc:80",
        "dispatcher": _dispatcher_yaml(),
        "role_machines": [],
        "roles": {},
        "capability_role_aliases": {},
    }


def _roles_config_yaml() -> Dict[str, Any]:
    """YAML doc carrying two roles + alias map."""
    return {
        "mac_url": "http://mac-api.mac.svc:80",
        "dispatcher": _dispatcher_yaml(),
        "role_machines": [
            {
                "machine_id": "mac-worker-machine",
                "hostname": "mac-worker.svc",
                "labels": {"kind": "virtual"},
            }
        ],
        "roles": {
            "python-coder": {
                "agent_id": "mac-worker-python-coder",
                "name": "mac-worker-python-coder",
                "machine_id": "mac-worker-machine",
                "capabilities": ["python", "ops"],
                "image": "ghcr.io/x/coder:latest",
                "executor": "/usr/local/bin/mac-task-executor-codex",
                "attestation_key_secret": {
                    "name": "mac-agent-keys",
                    "key": "coder.attestation",
                },
            },
            "python-reviewer": {
                "agent_id": "mac-worker-python-reviewer",
                "name": "mac-worker-python-reviewer",
                "machine_id": "mac-worker-machine",
                "capabilities": ["review", "python"],
                "image": "ghcr.io/x/reviewer:latest",
                "executor": "/usr/local/bin/mac-task-executor-codex-review",
                "attestation_key_secret": {
                    "name": "mac-agent-keys",
                    "key": "reviewer.attestation",
                },
            },
        },
        "capability_role_aliases": {
            "python": "python-coder",
            "review": "python-reviewer",
        },
    }


def _write_config_yaml(tmp_path: Path, doc: Dict[str, Any]) -> Path:
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump(doc))
    return f


@pytest.fixture()
def empty_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp_path config.yaml with no roles + $MAC_CONFIG_FILE set."""
    f = _write_config_yaml(tmp_path, _empty_config_yaml())
    monkeypatch.setenv("MAC_CONFIG_FILE", str(f))
    return f


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

    def test_read_only_report_fails_before_secret_env_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        projected: List[bool] = []

        def record_projection(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
            projected.append(True)
            return []

        monkeypatch.setattr("mac.k8s.runner._build_executor_container_env", record_projection)
        task = _task(
            metadata={
                "deliverable": "report",
                "report_repository_access": {
                    "schema": "mac.report_repository_access.v1",
                    "mode": "read_only",
                },
            }
        )

        with pytest.raises(ValueError, match=READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON):
            build_job_spec(task, _lease(), _cfg())

        assert projected == []

    def test_job_name_is_dns_safe(self) -> None:
        spec = build_job_spec(_task(id_="ABC_Task!1"), _lease(id_="lease-WITH/MIXED:chars"), _cfg())
        name = spec["metadata"]["name"]
        assert all(c.islower() or c.isdigit() or c == "-" for c in name)
        assert not name.startswith("-")
        assert len(name) <= 63

    def test_backoff_limit_is_zero(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        assert spec["spec"]["backoffLimit"] == 0

    def test_active_deadline_default_and_override(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg(active_deadline_seconds=900))
        assert spec["spec"]["activeDeadlineSeconds"] == 900
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
        assert sc["runAsUser"] == MAC_CONTAINER_UID == 10001
        assert sc["runAsGroup"] == MAC_CONTAINER_GID == 10001
        assert sc["fsGroup"] == MAC_CONTAINER_GID
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

    def test_legacy_shared_secret_is_explicitly_compatibility_only(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        by_name = {item["name"]: item for item in env}

        assert by_name["MAC_WORKER_IDENTITY_MODE"]["value"] == "compatibility"
        assert by_name["MAC_WORKER_TOKEN"]["valueFrom"]["secretKeyRef"] == {
            "name": "mac-api-config",
            "key": "MAC_WORKER_TOKEN",
        }
        assert "MAC_WORKER_CREDENTIAL_AGENT_ID" not in by_name

    def test_role_job_uses_its_exact_per_agent_credential_secret(self) -> None:
        cfg = _cfg(
            role_agent_ids={"python-coder": "agent_python_coder"},
            agent_token_secrets={"agent_python_coder": "mac-worker-agent-python-coder-deadbeef"},
        )
        task = _task(metadata={"required_role": "python-coder"})
        spec = build_job_spec(task, _lease(), cfg)
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        by_name = {item["name"]: item for item in env}

        assert by_name["MAC_AGENT_ID"]["value"] == "agent_python_coder"
        assert by_name["MAC_WORKER_IDENTITY_MODE"]["value"] == "bound"
        for key in (
            "MAC_WORKER_TOKEN",
            "MAC_WORKER_CREDENTIAL_ID",
            "MAC_WORKER_CREDENTIAL_VERSION",
            "MAC_WORKER_CREDENTIAL_AGENT_ID",
            "MAC_WORKER_CREDENTIAL_FINGERPRINT",
            "MAC_WORKER_RUNNING_DIGEST",
        ):
            assert by_name[key]["valueFrom"]["secretKeyRef"] == {
                "name": "mac-worker-agent-python-coder-deadbeef",
                "key": key,
            }

    def test_executor_timeout_forwarded_to_job_env(self) -> None:
        """MAC_TASK_EXECUTOR_TIMEOUT_SECONDS must reach the task Job pod.

        The executor reads this env to bound its subprocess; if it isn't
        forwarded into the job container env, raising it on the runner
        deployment has no effect and tasks keep dying at the 1500s
        default. Forward it from RunnerConfig.executor_timeout_seconds."""
        spec = build_job_spec(_task(), _lease(), _cfg(executor_timeout_seconds=2700))
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        timeout_var = next(
            (e for e in env if e["name"] == "MAC_TASK_EXECUTOR_TIMEOUT_SECONDS"),
            None,
        )
        assert timeout_var is not None, "timeout env not forwarded to job"
        assert timeout_var["value"] == "2700"

    def test_executor_timeout_not_set_when_unconfigured(self) -> None:
        """When no executor timeout is configured, don't emit the env var
        so the executor's own default applies."""
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        names = {e["name"] for e in env}
        assert "MAC_TASK_EXECUTOR_TIMEOUT_SECONDS" not in names

    def test_token_pulled_from_named_secret(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        tok = next(e for e in env if e["name"] == "MAC_WORKER_TOKEN")
        assert tok["valueFrom"]["secretKeyRef"]["name"] == "mac-api-config"

    def test_env_secret_name_overrides_both_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_config_file: Path,
    ) -> None:
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_config_file: Path,
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv("MAC_RUNNER_TASK_TOKEN_SECRET_NAME", "tok-secret")
        monkeypatch.setenv("MAC_RUNNER_TASK_SECRET_KEY_SECRET_NAME", "key-secret")
        cfg = RunnerConfig.from_env()
        assert cfg.secret_name_for_token == "tok-secret"
        assert cfg.secret_name_for_secret_key == "key-secret"

    def test_image_resolution_default(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        assert spec["spec"]["template"]["spec"]["containers"][0]["image"] == DEFAULT_TASK_IMAGE

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


class TestOpencodeConfigMapMount:
    def test_build_job_spec_mounts_opencode_configmap_by_default(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        pod_spec = spec["spec"]["template"]["spec"]
        # Volume present on pod.
        volumes = pod_spec["volumes"]
        opencode_vol = next((v for v in volumes if v.get("name") == "opencode-config"), None)
        assert opencode_vol is not None, "opencode-config volume must be on the pod by default"
        assert opencode_vol["configMap"] == {"name": "mac-opencode-config"}
        # VolumeMount present on container.
        mounts = pod_spec["containers"][0]["volumeMounts"]
        opencode_mount = next((m for m in mounts if m.get("name") == "opencode-config"), None)
        assert opencode_mount is not None
        assert opencode_mount["mountPath"] == "/etc/opencode"
        assert opencode_mount.get("readOnly") is True

    def test_build_job_spec_skips_opencode_mount_when_env_is_empty(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_config_file: Path,
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv("MAC_RUNNER_OPENCODE_CONFIGMAP_NAME", "")
        cfg = RunnerConfig.from_env()
        assert cfg.opencode_configmap_name == ""
        spec = build_job_spec(_task(), _lease(), cfg)
        pod_spec = spec["spec"]["template"]["spec"]
        vol_names = {v.get("name") for v in pod_spec["volumes"]}
        mount_names = {m.get("name") for m in pod_spec["containers"][0]["volumeMounts"]}
        assert "opencode-config" not in vol_names
        assert "opencode-config" not in mount_names


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
        self._read_responses = read_responses or [{"status": {"succeeded": 1, "failed": 0}}]
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


def test_claim_and_launch_blocks_read_only_report_without_creating_job() -> None:
    task = _task(
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        }
    )
    mac = _FakeMac(claim_response={"task": task, "lease": _lease()})
    jobs = _FakeJobs()

    result = claim_and_launch_one(mac, jobs, _cfg())

    assert result == {
        "status": "blocked",
        "reason": READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON,
        "task_id": "task-abc",
        "lease_id": "lease-xyz",
        "required_execution_boundary": "openshell",
    }
    assert jobs.created == []
    assert not any(post["path"].endswith("/delegate") for post in mac.posted)
    transition = next(post for post in mac.posted if post["path"].endswith("/transition"))
    assert transition["body"]["target_state"] == "blocked"
    assert transition["body"]["lease_id"] == "lease-xyz"
    assert transition["body"]["detail"] == {
        "reason": READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON,
        "manual_repair_required": True,
        "required_execution_boundary": "openshell",
        "rejected_execution_boundary": "kubernetes_job",
    }


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
    release_posts = [p for p in mac.posted if p["path"].endswith("/transition")]
    assert release_posts, "lease should be released to open on k8s failure"
    assert release_posts[0]["body"]["target_state"] == "open"


def test_claim_and_launch_refuses_assignment_without_lease() -> None:
    mac = _FakeMac(claim_response={"task": _task(), "lease": {}})
    jobs = _FakeJobs()
    result = claim_and_launch_one(mac, jobs, _cfg())
    assert result is None
    assert jobs.created == []


def _role_runner_cfg() -> Any:
    base = _cfg()
    base.role_images = {"python-coder": "ghcr.io/x/coder:latest"}
    base.role_agent_ids = {"python-coder": "mac-worker-python-coder"}
    base.role_executors = {"python-coder": "/usr/local/bin/mac-task-executor-codex"}
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

    delegate_posts = [p for p in mac.posted if p["path"].endswith("/delegate")]
    assert len(delegate_posts) == 1, "exactly one delegate call expected"
    body = delegate_posts[0]["body"]
    assert body == {
        "agent_id": cfg.agent_id,
        "to_agent_id": "mac-worker-python-coder",
    }
    # Path includes the lease id.
    assert delegate_posts[0]["path"] == "/leases/lease-xyz/delegate"


def test_claim_and_launch_skips_delegate_when_role_agent_is_dispatcher() -> None:
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
    delegate_posts = [p for p in mac.posted if p["path"].endswith("/delegate")]
    assert delegate_posts == []


def test_claim_and_launch_proceeds_when_delegate_fails() -> None:
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
    assert result["status"] == "lease_delegation_failed"
    assert jobs.created == []
    delegate_posts = [p for p in mac.posted if p["path"].endswith("/delegate")]
    assert len(delegate_posts) == 1  # delegation was attempted
    release_posts = [p for p in mac.posted if p["path"].endswith("/transition")]
    assert release_posts, "lease should be released to open on delegation failure"
    assert release_posts[0]["body"]["target_state"] == "open"
    assert release_posts[0]["body"]["detail"]["reason"] == "lease_delegation_failed"


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
        assert _resolve_agent_id_for_role("python-coder", cfg) == "mac-worker-python-coder"

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
        assert _resolve_executor_for_role("ghost-role", _role_cfg()) is None


def _role_cfg_with_attestation_secrets(**overrides: Any) -> RunnerConfig:
    """Role config with the PR3 attestation key secrets map populated."""
    base = _role_cfg()
    base.role_attestation_key_secrets = {
        "python-coder": {
            "name": "mac-agent-keys",
            "key": "python-coder.attestation",
        },
        "python-reviewer": {
            "name": "mac-agent-keys",
            "key": "python-reviewer.attestation",
        },
    }
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestResolveAttestationKeySecretForRole:
    def test_role_hit_returns_secret_ref(self) -> None:
        cfg = _role_cfg_with_attestation_secrets()
        spec = _resolve_attestation_key_secret_for_role("python-coder", cfg)
        assert spec == {
            "name": "mac-agent-keys",
            "key": "python-coder.attestation",
        }

    def test_no_role_returns_none(self) -> None:
        cfg = _role_cfg_with_attestation_secrets()
        assert _resolve_attestation_key_secret_for_role(None, cfg) is None

    def test_unmapped_role_returns_none(self) -> None:
        cfg = _role_cfg()  # no role_attestation_key_secrets entries
        assert _resolve_attestation_key_secret_for_role("python-coder", cfg) is None

    def test_returns_copy_so_caller_mutation_does_not_leak(self) -> None:
        cfg = _role_cfg_with_attestation_secrets()
        spec = _resolve_attestation_key_secret_for_role("python-coder", cfg)
        assert spec is not None
        spec["name"] = "hijacked"
        # Original config unchanged.
        assert cfg.role_attestation_key_secrets["python-coder"]["name"] == "mac-agent-keys"


class TestRoleAwareBuildJobSpec:
    def test_no_role_preserves_default_behaviour(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "runner-1"
        assert _env_value(env, "MAC_AGENT_ROLE") == ""
        assert "MAC_TASK_EXECUTOR_COMMAND" not in _env_names(env)
        # Labels carry the default-role + dispatcher agent_id.
        labels = spec["metadata"]["labels"]
        assert labels["mac.role"] == "default"
        assert labels["mac.agent.id"] == _sanitize_dns_label("runner-1")
        # Image stays as the cfg default.
        assert spec["spec"]["template"]["spec"]["containers"][0]["image"] == DEFAULT_TASK_IMAGE

    def test_explicit_required_role_populates_everything(self) -> None:
        task = _task(metadata={"required_role": "python-coder"})
        spec = build_job_spec(task, _lease(), _role_cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "mac-worker-python-coder"
        assert _env_value(env, "MAC_AGENT_ROLE") == "python-coder"
        assert (
            _env_value(env, "MAC_TASK_EXECUTOR_COMMAND") == "/usr/local/bin/mac-task-executor-codex"
        )
        labels = spec["metadata"]["labels"]
        assert labels["mac.role"] == "python-coder"
        assert labels["mac.agent.id"] == "mac-worker-python-coder"
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/x/coder:latest"
        )

    def test_capability_alias_routes_correctly(self) -> None:
        task = _task(required_capabilities=["python"])
        spec = build_job_spec(task, _lease(), _role_cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == "mac-worker-python-coder"
        assert _env_value(env, "MAC_AGENT_ROLE") == "python-coder"
        assert (
            spec["spec"]["template"]["spec"]["containers"][0]["image"] == "ghcr.io/x/coder:latest"
        )

    def test_unknown_capability_and_empty_alias_map_falls_through(self) -> None:
        cfg = _cfg()  # NB: empty alias/role maps
        task = _task(required_capabilities=["unknown-cap"])
        spec = build_job_spec(task, _lease(), cfg)
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert _env_value(env, "MAC_AGENT_ID") == cfg.agent_id
        assert _env_value(env, "MAC_AGENT_ROLE") == ""
        assert "MAC_TASK_EXECUTOR_COMMAND" not in _env_names(env)
        labels = spec["metadata"]["labels"]
        assert labels["mac.role"] == "default"
        assert spec["spec"]["template"]["spec"]["containers"][0]["image"] == cfg.default_image

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


def _env_entry(env: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for e in env:
        if e.get("name") == name:
            return e
    return None


class TestAttestationKeyJobEnv:
    def test_no_role_does_not_emit_attestation_env(self) -> None:
        spec = build_job_spec(_task(), _lease(), _cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert "MAC_AGENT_ATTESTATION_KEY" not in _env_names(env)

    def test_role_without_attestation_secret_does_not_emit_env(self) -> None:
        task = _task(metadata={"required_role": "python-coder"})
        spec = build_job_spec(task, _lease(), _role_cfg())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        assert "MAC_AGENT_ATTESTATION_KEY" not in _env_names(env)

    def test_role_with_attestation_secret_emits_secret_ref_env(self) -> None:
        task = _task(metadata={"required_role": "python-coder"})
        spec = build_job_spec(task, _lease(), _role_cfg_with_attestation_secrets())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        entry = _env_entry(env, "MAC_AGENT_ATTESTATION_KEY")
        assert entry is not None
        assert "value" not in entry, "key must be sourced from a Secret, never embedded"
        assert entry["valueFrom"]["secretKeyRef"] == {
            "name": "mac-agent-keys",
            "key": "python-coder.attestation",
        }

    def test_capability_alias_routes_to_correct_attestation_key(self) -> None:
        task = _task(required_capabilities=["review"])
        spec = build_job_spec(task, _lease(), _role_cfg_with_attestation_secrets())
        env = spec["spec"]["template"]["spec"]["containers"][0]["env"]
        entry = _env_entry(env, "MAC_AGENT_ATTESTATION_KEY")
        assert entry is not None
        assert entry["valueFrom"]["secretKeyRef"]["key"] == "python-reviewer.attestation"


class TestRunnerConfigFromEnvRoles:
    def test_roles_derived_from_yaml(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        f = _write_config_yaml(tmp_path, _roles_config_yaml())
        monkeypatch.setenv("MAC_CONFIG_FILE", str(f))
        cfg = RunnerConfig.from_env()
        assert cfg.role_images == {
            "python-coder": "ghcr.io/x/coder:latest",
            "python-reviewer": "ghcr.io/x/reviewer:latest",
        }
        assert cfg.role_agent_ids == {
            "python-coder": "mac-worker-python-coder",
            "python-reviewer": "mac-worker-python-reviewer",
        }
        assert cfg.role_executors == {
            "python-coder": "/usr/local/bin/mac-task-executor-codex",
            "python-reviewer": "/usr/local/bin/mac-task-executor-codex-review",
        }
        assert cfg.capability_role_aliases == {
            "python": "python-coder",
            "review": "python-reviewer",
        }
        assert cfg.role_attestation_key_secrets == {
            "python-coder": {
                "name": "mac-agent-keys",
                "key": "coder.attestation",
            },
            "python-reviewer": {
                "name": "mac-agent-keys",
                "key": "reviewer.attestation",
            },
        }

    def test_empty_roles_yields_empty_maps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        empty_config_file: Path,
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        cfg = RunnerConfig.from_env()
        assert cfg.role_images == {}
        assert cfg.role_agent_ids == {}
        assert cfg.role_executors == {}
        assert cfg.capability_role_aliases == {}
        assert cfg.role_attestation_key_secrets == {}

    def test_missing_config_file_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        monkeypatch.setenv("MAC_CONFIG_FILE", str(tmp_path / "does-not-exist.yaml"))
        with pytest.raises(SystemExit, match="is missing"):
            RunnerConfig.from_env()

    def test_malformed_yaml_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        f = tmp_path / "config.yaml"
        f.write_text("mac_url: [bad\n")
        monkeypatch.setenv("MAC_CONFIG_FILE", str(f))
        with pytest.raises(SystemExit, match="not valid YAML"):
            RunnerConfig.from_env()

    def test_role_missing_image_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("MAC_URL", "http://x")
        doc = _roles_config_yaml()
        doc["roles"]["python-coder"].pop("image")
        f = _write_config_yaml(tmp_path, doc)
        monkeypatch.setenv("MAC_CONFIG_FILE", str(f))
        with pytest.raises(SystemExit, match="roles.python-coder.*image"):
            RunnerConfig.from_env()


class TestJobIsTerminal:
    def test_succeeded_one_is_terminal(self) -> None:
        assert _job_is_terminal({"status": {"succeeded": 1}}) is True

    def test_failed_one_is_terminal(self) -> None:
        assert _job_is_terminal({"status": {"failed": 1}}) is True

    def test_zero_counts_are_not_terminal(self) -> None:
        assert _job_is_terminal({"status": {"succeeded": 0, "failed": 0}}) is False

    def test_missing_job_is_terminal(self) -> None:
        assert _job_is_terminal({}) is True

    def test_missing_status_is_not_terminal(self) -> None:
        assert _job_is_terminal({"status": None}) is False


class _RenewalFakeMac:
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
    assert jobs.read_calls == 3
    # We attempted at least one renew even though they failed.
    assert mac.posts, "renewal POST should have been attempted"


def test_renewal_loop_tolerates_read_failure() -> None:
    class _FlakyJobs:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, namespace: str, manifest: Dict[str, Any]) -> Dict[str, Any]:
            raise NotImplementedError

        def list_active(self, namespace: str, label_selector: str) -> List[Dict[str, Any]]:
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
    mac = _FakeMac(claim_response={"task": _task(), "lease": _lease()})
    jobs = _FakeJobs(read_responses=[{"status": {"succeeded": 0, "failed": 0}}])
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


class _AgentsMac(_FakeMac):
    def __init__(
        self,
        agents: Dict[str, Any],
        *,
        fetch_error: Optional[Exception] = None,
    ) -> None:
        super().__init__(claim_response=None)
        self._agents = agents
        self._fetch_error = fetch_error

    def get(self, path: str) -> Dict[str, Any]:
        self.gotten.append(path)
        if self._fetch_error is not None:
            raise self._fetch_error
        # Expect "/agents/{id}".
        prefix = "/agents/"
        if not path.startswith(prefix):
            raise RuntimeError("unexpected GET path: %s" % path)
        agent_id = path[len(prefix) :]
        if agent_id not in self._agents:
            raise RuntimeError("agent not found: %s" % agent_id)
        return self._agents[agent_id]


def test_check_dispatcher_capabilities_returns_empty_when_no_roles_configured() -> None:
    cfg = _cfg()  # role_agent_ids defaults to {}
    mac = _AgentsMac(agents={})
    assert check_dispatcher_capabilities(cfg, mac) == []
    # Probe should short-circuit without any GETs.
    assert mac.gotten == []


def test_check_dispatcher_capabilities_returns_empty_when_dispatcher_covers_all() -> None:
    cfg = _cfg(
        role_agent_ids={
            "python-coder": "mac-worker-python-coder",
            "python-reviewer": "mac-worker-python-reviewer",
        },
    )
    mac = _AgentsMac(
        agents={
            "runner-1": {"id": "runner-1", "capabilities": ["python", "review", "ops"]},
            "mac-worker-python-coder": {
                "id": "mac-worker-python-coder",
                "capabilities": ["python"],
            },
            "mac-worker-python-reviewer": {
                "id": "mac-worker-python-reviewer",
                "capabilities": ["review"],
            },
        }
    )
    assert check_dispatcher_capabilities(cfg, mac) == []


def test_check_dispatcher_capabilities_returns_missing_when_dispatcher_underprovisioned() -> None:
    cfg = _cfg(
        role_agent_ids={
            "python-coder": "mac-worker-python-coder",
            "python-reviewer": "mac-worker-python-reviewer",
        },
    )
    mac = _AgentsMac(
        agents={
            "runner-1": {"id": "runner-1", "capabilities": ["python"]},
            "mac-worker-python-coder": {
                "id": "mac-worker-python-coder",
                "capabilities": ["python", "ops"],
            },
            "mac-worker-python-reviewer": {
                "id": "mac-worker-python-reviewer",
                "capabilities": ["review"],
            },
        }
    )
    missing = check_dispatcher_capabilities(cfg, mac)
    assert missing == ["ops", "review"]


def test_check_dispatcher_capabilities_warns_but_does_not_raise_on_404(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _cfg(
        role_agent_ids={
            "python-coder": "mac-worker-python-coder",
            "python-reviewer": "mac-worker-python-reviewer",
        },
    )
    mac = _AgentsMac(
        agents={
            "runner-1": {"id": "runner-1", "capabilities": []},
            "mac-worker-python-coder": {
                "id": "mac-worker-python-coder",
                "capabilities": ["python"],
            },
            # No mac-worker-python-reviewer entry → simulated 404.
        }
    )
    with caplog.at_level("WARNING", logger="mac.k8s.runner"):
        missing = check_dispatcher_capabilities(cfg, mac)
    assert missing == ["python"]
    # And it logged a warning about the failed fetch.
    assert any("mac-worker-python-reviewer" in rec.getMessage() for rec in caplog.records), (
        "expected a warning naming the un-fetchable role agent"
    )


def test_check_dispatcher_capabilities_tolerates_dispatcher_fetch_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _cfg(
        role_agent_ids={"python-coder": "mac-worker-python-coder"},
    )
    mac = _AgentsMac(agents={}, fetch_error=RuntimeError("hub unreachable"))
    with caplog.at_level("WARNING", logger="mac.k8s.runner"):
        missing = check_dispatcher_capabilities(cfg, mac)
    assert missing == []
    assert any(
        "dispatcher" in rec.getMessage().lower() and "hub unreachable" in rec.getMessage()
        for rec in caplog.records
    ), "expected a warning naming the dispatcher fetch failure"


def _review_cfg(**overrides: Any) -> RunnerConfig:
    base = _cfg()
    base.role_images = {"python-reviewer": "ghcr.io/x/reviewer:latest"}
    base.role_executors = {
        "python-reviewer": "/usr/local/bin/mac-task-executor-opencode-review",
    }
    base.role_attestation_key_secrets = {
        "python-reviewer": {"name": "mac-attn", "key": "python-reviewer"},
    }
    base.reviewer_agent_ids = {"python-reviewer": "mac-worker-python-reviewer"}
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_build_review_job_spec_sets_required_env() -> None:
    cfg = _review_cfg(
        role_executors={"python-reviewer": "/usr/local/bin/mac-task-executor-codex-review"}
    )
    spec = build_review_job_spec(
        "review-1",
        "task-abc",
        "mac-worker-python-reviewer",
        "ev-target",
        cfg,
        canonical_task={"id": "task-abc", "metadata": {}},
    )
    envs = {e["name"]: e for e in spec["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert envs["MAC_REVIEW_ID"]["value"] == "review-1"
    assert envs["MAC_REVIEW_TARGET_EVIDENCE_ID"]["value"] == "ev-target"
    assert envs["MAC_TASK_ID"]["value"] == "task-abc"
    assert envs["MAC_AGENT_ID"]["value"] == "mac-worker-python-reviewer"
    assert envs["MAC_AGENT_ROLE"]["value"] == "python-reviewer"
    assert (
        envs["MAC_TASK_EXECUTOR_COMMAND"]["value"]
        == "/usr/local/bin/mac-task-executor-opencode-review"
    )
    assert "MAC_LEASE_ID" not in envs, "review Jobs are not lease-bound"
    assert "GH_TOKEN" in envs
    assert "GITHUB_TOKEN" in envs
    assert "GITEA_TOKEN" in envs
    assert "MAC_SECRET_KEY" not in envs
    assert spec["metadata"]["labels"]["mac.review.id"]
    assert spec["metadata"]["labels"]["app.kubernetes.io/component"] == "review-executor"


def test_build_review_job_spec_uses_reviewer_image() -> None:
    spec = build_review_job_spec(
        "r1",
        "t1",
        "mac-worker-python-reviewer",
        "ev1",
        _review_cfg(),
        canonical_task={"id": "t1", "metadata": {}},
    )
    image = spec["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == "ghcr.io/x/reviewer:latest"


def test_build_review_job_spec_rejects_read_only_report_before_secret_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projected: List[bool] = []

    def record_projection(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
        projected.append(True)
        return []

    monkeypatch.setattr("mac.k8s.runner._build_executor_container_env", record_projection)
    with pytest.raises(ValueError, match=READ_ONLY_REPORT_REQUIRES_OPENSHELL_REASON):
        build_review_job_spec(
            "r1",
            "t1",
            "mac-worker-python-reviewer",
            "ev1",
            _review_cfg(),
            canonical_task={
                "id": "t1",
                "metadata": {
                    "deliverable": "report",
                    "report_repository_access": {
                        "schema": "mac.report_repository_access.v1",
                        "mode": "read_only",
                    },
                },
            },
        )

    assert projected == []


class _FakeMacForReview:
    def __init__(
        self,
        deliver_responses: Dict[str, List[Dict[str, Any]]],
        claim_response: Optional[Dict[str, Any]] = None,
        task_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._deliver = deliver_responses
        self._claim = claim_response or {"status": "claimed"}
        self._task = task_response or {"id": "task-abc", "metadata": {}}
        self.posted: List[Dict[str, Any]] = []
        self.gotten: List[str] = []

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        self.posted.append({"path": path, "body": body})
        if "/messages/deliver" in path:
            for agent_id, msgs in self._deliver.items():
                if "/agents/%s/" % agent_id in path:
                    return msgs
            return []
        if "/reviews/" in path and path.endswith("/claim"):
            return self._claim
        return {}

    def get(self, path: str) -> Dict[str, Any]:
        self.gotten.append(path)
        return self._task


def test_claim_and_launch_review_returns_none_when_no_reviewers() -> None:
    cfg = _cfg()  # no reviewer_agent_ids
    mac = _FakeMacForReview({})
    jobs = _FakeJobs()
    assert claim_and_launch_review_one(mac, jobs, cfg) is None
    assert jobs.created == []


def test_claim_and_launch_review_returns_none_when_no_nudges() -> None:
    cfg = _review_cfg()
    mac = _FakeMacForReview({"mac-worker-python-reviewer": []})
    jobs = _FakeJobs()
    assert claim_and_launch_review_one(mac, jobs, cfg) is None
    assert jobs.created == []


def test_claim_and_launch_review_happy_path() -> None:
    cfg = _review_cfg()
    nudge = {
        "id": "msg-1",
        "message_type": "nudge",
        "payload": {
            "reason": "produce_review_verdict",
            "task_id": "task-abc",
            "review_id": "review-1",
            "executor_evidence_id": "ev-target",
        },
    }
    mac = _FakeMacForReview({"mac-worker-python-reviewer": [nudge]})
    jobs = _FakeJobs()
    result = claim_and_launch_review_one(mac, jobs, cfg)
    assert result is not None
    assert result["status"] == "launched"
    assert result["review_id"] == "review-1"
    assert result["task_id"] == "task-abc"
    assert result["reviewer_agent_id"] == "mac-worker-python-reviewer"
    assert result["role"] == "python-reviewer"
    assert len(jobs.created) == 1
    claim_posts = [p for p in mac.posted if p["path"].endswith("/claim")]
    assert (
        claim_posts and claim_posts[0]["body"]["reviewer_agent_id"] == "mac-worker-python-reviewer"
    )


def test_claim_and_launch_review_leaves_read_only_report_unclaimed_without_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _review_cfg()
    nudge = {
        "id": "msg-1",
        "message_type": "nudge",
        "payload": {
            "reason": "produce_review_verdict",
            "task_id": "task-abc",
            "review_id": "review-1",
            "executor_evidence_id": "ev-target",
        },
    }
    task = {
        "id": "task-abc",
        "metadata": {
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
        },
    }
    projected: List[bool] = []

    def record_projection(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
        projected.append(True)
        return []

    monkeypatch.setattr("mac.k8s.runner._build_executor_container_env", record_projection)
    mac = _FakeMacForReview({"mac-worker-python-reviewer": [nudge]}, task_response=task)
    jobs = _FakeJobs()

    assert claim_and_launch_review_one(mac, jobs, cfg) is None
    assert mac.gotten == ["/tasks/task-abc"]
    assert not any(post["path"].endswith("/claim") for post in mac.posted)
    assert jobs.created == []
    assert projected == []


def test_claim_and_launch_review_skips_when_claim_rejected() -> None:
    cfg = _review_cfg()
    nudge = {
        "id": "msg-1",
        "message_type": "nudge",
        "payload": {
            "reason": "produce_review_verdict",
            "task_id": "task-abc",
            "review_id": "review-1",
            "executor_evidence_id": "ev-target",
        },
    }
    mac = _FakeMacForReview(
        {"mac-worker-python-reviewer": [nudge]},
        claim_response={"status": "not_claimable", "reason": "already_claimed"},
    )
    jobs = _FakeJobs()
    assert claim_and_launch_review_one(mac, jobs, cfg) is None
    assert jobs.created == []


def test_claim_and_launch_review_ignores_non_verdict_messages() -> None:
    cfg = _review_cfg()
    other = {"id": "m1", "message_type": "status_update", "payload": {}}
    mac = _FakeMacForReview({"mac-worker-python-reviewer": [other]})
    jobs = _FakeJobs()
    assert claim_and_launch_review_one(mac, jobs, cfg) is None
    assert jobs.created == []


# --- relocated from test_k8s_runner_edges.py (coverage companion folded in) ---


class _Mac:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def post(self, path, body):
        self.calls.append(("post", path, body))
        value = self.responses.pop(0) if self.responses else {}
        if isinstance(value, BaseException):
            raise value
        return value

    def get(self, path):
        self.calls.append(("get", path, None))
        value = self.responses.pop(0) if self.responses else {}
        if isinstance(value, BaseException):
            raise value
        return value


class _Jobs:
    def __init__(self, result=None):
        self.result = result if result is not None else {"metadata": {"uid": "uid"}}
        self.created = []

    def create(self, namespace, manifest):
        self.created.append((namespace, manifest))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _cfg_edges(**extra):
    values = {"mac_url": "http://mac", "agent_id": "dispatcher", "poll_interval_seconds": 0}
    values.update(extra)
    return runner.RunnerConfig(**values)


def _assignment(role=None):
    metadata = {"required_role": role} if role else {}
    return {
        "task": {"id": "task", "metadata": metadata, "required_capabilities": []},
        "lease": {"id": "lease"},
    }


def test_optional_int_sanitize_deadline_and_terminal_edges(monkeypatch) -> None:
    monkeypatch.delenv("OPTIONAL", raising=False)
    assert runner._optional_int_env("OPTIONAL") is None
    monkeypatch.setenv("OPTIONAL", "bad")
    assert runner._optional_int_env("OPTIONAL") is None
    monkeypatch.setenv("OPTIONAL", "0")
    assert runner._optional_int_env("OPTIONAL") is None
    monkeypatch.setenv("OPTIONAL", "12")
    assert runner._optional_int_env("OPTIONAL") == 12
    assert runner._sanitize_dns_label("---") == "mac-task"
    assert (
        runner._resolve_active_deadline(
            {"metadata": {"k8s": {"active_deadline_seconds": "bad"}}},
            _cfg_edges(active_deadline_seconds=99),
        )
        == 99
    )
    assert (
        runner._resolve_active_deadline(
            {"metadata": {"k8s": {"active_deadline_seconds": 1}}}, _cfg_edges()
        )
        == 60
    )
    assert runner._job_is_terminal(None) is False
    assert runner._job_is_terminal({"status": {"succeeded": "bad", "failed": "bad"}}) is False


def test_agent_token_secret_map_is_reference_only_and_fail_closed() -> None:
    assert runner._agent_token_secret_map("") == {}
    assert runner._agent_token_secret_map(
        '{"agent_a":"mac-worker-agent-a","agent_b":"mac-worker-agent-b"}'
    ) == {"agent_a": "mac-worker-agent-a", "agent_b": "mac-worker-agent-b"}
    with pytest.raises(ValueError, match="JSON object"):
        runner._agent_token_secret_map("[]")
    with pytest.raises(ValueError, match="non-empty"):
        runner._agent_token_secret_map('{"agent_a":""}')


def test_dispatcher_capability_probe_shape_and_role_failures() -> None:
    cfg = _cfg_edges(role_agent_ids={"worker": "worker"}, reviewer_agent_ids={"review": "reviewer"})
    assert runner.check_dispatcher_capabilities(cfg, _Mac([RuntimeError("offline")])) == []
    assert runner.check_dispatcher_capabilities(cfg, _Mac([[]])) == []
    mac = _Mac(
        [{"capabilities": []}, RuntimeError("review missing"), RuntimeError("worker missing")]
    )
    assert runner.check_dispatcher_capabilities(cfg, mac) == []
    mac = _Mac([{"capabilities": []}, {"capabilities": ["review", "shared"]}, []])
    assert runner.check_dispatcher_capabilities(cfg, mac) == []


def test_claim_next_failures_and_missing_lease() -> None:
    assert (
        runner.claim_and_launch_one(_Mac([RuntimeError("offline")]), _Jobs(), _cfg_edges()) is None
    )
    assert runner.claim_and_launch_one(_Mac([{}]), _Jobs(), _cfg_edges()) is None
    result = runner.claim_and_launch_one(
        _Mac([{"task": {"id": "task"}, "lease": {}}]), _Jobs(), _cfg_edges()
    )
    assert result is None


def test_claim_delegation_failure_reopens_best_effort() -> None:
    cfg = _cfg_edges(role_agent_ids={"coder": "coder"})
    mac = _Mac(
        [_assignment("coder"), RuntimeError("delegate failed"), RuntimeError("reopen failed")]
    )
    result = runner.claim_and_launch_one(mac, _Jobs(), cfg)
    assert result["status"] == "lease_delegation_failed"
    assert result["to_agent_id"] == "coder"


def test_claim_job_create_failure_and_renewal_start_failure(monkeypatch) -> None:
    mac = _Mac([_assignment(), RuntimeError("reopen failed")])
    result = runner.claim_and_launch_one(mac, _Jobs(RuntimeError("create failed")), _cfg_edges())
    assert result["status"] == "k8s_create_failed"
    monkeypatch.setattr(
        runner,
        "_start_lease_renewal_thread",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("thread failed")),
    )
    result = runner.claim_and_launch_one(_Mac([_assignment()]), _Jobs(), _cfg_edges())
    assert result["status"] == "launched"
    assert result["job_uid"] == "uid"


def _nudge(**extra):
    payload = {
        "reason": "produce_review_verdict",
        "review_id": "review",
        "task_id": "task",
        "executor_evidence_id": "evidence",
    }
    payload.update(extra)
    return {"message_type": "nudge", "payload": payload}


def test_review_claim_filters_delivery_and_malformed_messages() -> None:
    cfg = _cfg_edges(reviewer_agent_ids={"reviewer": "reviewer-agent"})
    assert runner.claim_and_launch_review_one(_Mac([RuntimeError("offline")]), _Jobs(), cfg) is None
    assert runner.claim_and_launch_review_one(_Mac([{}]), _Jobs(), cfg) is None
    messages = [
        "bad",
        {"message_type": "other"},
        {"message_type": "nudge", "payload": {"reason": "other"}},
        _nudge(review_id=""),
    ]
    assert runner.claim_and_launch_review_one(_Mac([messages]), _Jobs(), cfg) is None


def test_review_claim_and_create_failures_then_success() -> None:
    cfg = _cfg_edges(reviewer_agent_ids={"reviewer": "reviewer-agent"})
    assert (
        runner.claim_and_launch_review_one(
            _Mac([[_nudge()], {"id": "task", "metadata": {}}, RuntimeError("claim failed")]),
            _Jobs(),
            cfg,
        )
        is None
    )
    assert (
        runner.claim_and_launch_review_one(
            _Mac(
                [
                    [_nudge()],
                    {"id": "task", "metadata": {}},
                    {"status": "skipped", "reason": "busy"},
                ]
            ),
            _Jobs(),
            cfg,
        )
        is None
    )
    assert (
        runner.claim_and_launch_review_one(
            _Mac([[_nudge()], {"id": "task", "metadata": {}}, {"status": "claimed"}]),
            _Jobs(RuntimeError("create failed")),
            cfg,
        )
        is None
    )
    result = runner.claim_and_launch_review_one(
        _Mac([[_nudge()], {"id": "task", "metadata": {}}, {"status": "claimed"}]), _Jobs(), cfg
    )
    assert result["status"] == "launched"
    assert result["role"] == "reviewer"


def test_runner_and_review_loops_count_launches_and_sleep(monkeypatch) -> None:
    outcomes = iter(
        [None, {"status": "launched", "task_id": "t", "lease_id": "l", "job_name": "j"}]
    )
    monkeypatch.setattr(runner, "claim_and_launch_one", lambda *_a: next(outcomes))
    sleeps = []
    assert runner.runner_loop(_Mac(), _Jobs(), _cfg_edges(), iterations=2, sleep=sleeps.append) == 1
    assert sleeps == [0]
    outcomes = iter([{"status": "failed"}, {"status": "launched"}])
    monkeypatch.setattr(runner, "claim_and_launch_review_one", lambda *_a: next(outcomes))
    sleeps = []
    assert runner.review_loop(_Mac(), _Jobs(), _cfg_edges(), iterations=2, sleep=sleeps.append) == 1
    assert sleeps == [0]
