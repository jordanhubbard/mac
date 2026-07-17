
from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

yaml = pytest.importorskip("yaml")


ROOT = Path(__file__).resolve().parent.parent / "deploy" / "k8s"


def _load(path: Path) -> List[dict]:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    return docs


def test_mac_api_deployment_is_stateless() -> None:
    """Phase 3 mac-api must have no PVC and >= 2 replicas — that's what
    'stateless' means architecturally."""
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    spec = deploy["spec"]
    assert spec["replicas"] >= 2

    template_spec = spec["template"]["spec"]
    volumes = template_spec.get("volumes", [])
    # Only emptyDir or configMap or secret volumes allowed.
    for v in volumes:
        assert (
            "emptyDir" in v or "configMap" in v or "secret" in v
        ), f"unexpected persistent volume: {v}"
    assert "volumeClaimTemplates" not in spec, "stateless apps have no PVCs"


def test_mac_api_image_is_not_latest() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    image = deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    assert ":latest" not in image, "mac-rollout policy forbids :latest tags"


def test_mac_api_env_wires_database_url_from_secret() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    env = deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    db_url = next(e for e in env if e["name"] == "MAC_DATABASE_URL")
    assert "valueFrom" in db_url, "MAC_DATABASE_URL must come from a Secret"
    secret_ref = db_url["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == "mac-api-config"
    assert secret_ref["key"] == "MAC_DATABASE_URL"


def test_mac_api_env_wires_secret_key() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    env = deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    sk = next(e for e in env if e["name"] == "MAC_SECRET_KEY")
    assert sk["valueFrom"]["secretKeyRef"]["name"] == "mac-api-config"


def test_mac_api_work_package_actuation_is_explicitly_disabled() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    env = {
        item["name"]: item
        for item in deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    }

    assert env["MAC_WORK_PACKAGE_PIPELINE_ENABLED"]["value"] == "false"
    assert env["MAC_WORK_PACKAGE_LANDING_ENABLED"]["value"] == "false"
    assert "MAC_WORK_PACKAGE_BUNDLE_DIR" not in env


def test_mac_api_both_probes_defined() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    assert "readinessProbe" in container
    assert "livenessProbe" in container
    # Probes hit /health on the named port.
    assert container["readinessProbe"]["httpGet"]["path"] == "/health"
    assert container["livenessProbe"]["httpGet"]["path"] == "/health"


def test_mac_api_runs_as_nonroot() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    pod_sec = deploy["spec"]["template"]["spec"]["securityContext"]
    assert pod_sec["runAsNonRoot"] is True
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    csec = container["securityContext"]
    assert csec["readOnlyRootFilesystem"] is True
    assert csec["allowPrivilegeEscalation"] is False


def test_kustomization_tree_includes_expected_resources() -> None:
    api_kust = _load(ROOT / "mac-api" / "kustomization.yaml")[0]
    for res in ("namespace.yaml", "deployment.yaml", "service.yaml"):
        assert res in api_kust["resources"]
    runner_kust = _load(ROOT / "mac-runner" / "kustomization.yaml")[0]
    for res in ("serviceaccount.yaml", "rbac.yaml", "deployment.yaml"):
        assert res in runner_kust["resources"]


def test_runner_has_two_service_accounts() -> None:
    docs = _load(ROOT / "mac-runner" / "serviceaccount.yaml")
    names = {d["metadata"]["name"] for d in docs}
    assert names == {"mac-k8s-orchestrator", "mac-task-runner"}
    task_sa = next(d for d in docs if d["metadata"]["name"] == "mac-task-runner")
    assert task_sa.get("automountServiceAccountToken") is False


def test_orchestrator_rbac_is_scoped_to_namespace() -> None:
    docs = _load(ROOT / "mac-runner" / "rbac.yaml")
    kinds = [d["kind"] for d in docs]
    assert "Role" in kinds and "RoleBinding" in kinds
    # No ClusterRole / ClusterRoleBinding — orchestrator stays in its namespace.
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    role = next(d for d in docs if d["kind"] == "Role")
    # Has create+delete on batch.jobs (task/review Job dispatch + reconcile).
    job_rule = next(
        r for r in role["rules"] if "jobs" in (r.get("resources") or [])
    )
    assert "create" in job_rule["verbs"]
    assert "delete" in job_rule["verbs"]
    scale_rule = next(
        r for r in role["rules"] if "deployments/scale" in (r.get("resources") or [])
    )
    assert "patch" in scale_rule["verbs"]


def test_orchestrator_deployment_uses_orchestrator_sa_and_runs_correct_binary() -> None:
    deploy = _load(ROOT / "mac-runner" / "deployment.yaml")[0]
    pod = deploy["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "mac-k8s-orchestrator"
    assert pod["automountServiceAccountToken"] is True
    container = pod["containers"][0]
    assert container["command"] == [
        "/usr/local/bin/mac-crash-observer",
        "--supervisor",
        "kubernetes",
        "--",
        "mac-k8s-orchestrator",
    ]
    env_names = {e["name"] for e in container["env"]}
    for required in (
        "MAC_URL",
        "MAC_AGENT_ID",
        "MAC_RUNNER_NAMESPACE",
        "MAC_RUNNER_TASK_SERVICE_ACCOUNT",
        "MAC_WORKER_TOKEN",
        "MAC_WORKER_CREDENTIAL_ID",
        "MAC_WORKER_CREDENTIAL_VERSION",
        "MAC_WORKER_CREDENTIAL_AGENT_ID",
        "MAC_WORKER_CREDENTIAL_FINGERPRINT",
        "MAC_WORKER_RUNNING_DIGEST",
        "MAC_WORKER_IDENTITY_MODE",
        "MAC_RUNNER_AGENT_TOKEN_SECRETS",
    ):
        assert required in env_names
    env = {item["name"]: item for item in container["env"]}
    assert env["MAC_AGENT_ID"] == {
        "name": "MAC_AGENT_ID",
        "value": "mac-k8s-orchestrator",
    }
    token_ref = env["MAC_WORKER_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert token_ref["name"].startswith("mac-worker-mac-k8s-orchestrator-")
    assert token_ref["name"] != "mac-api-config"
    assert env["MAC_WORKER_IDENTITY_MODE"]["value"] == "bound"


def test_runner_deployment_is_replicated() -> None:
    deploy = _load(ROOT / "mac-runner" / "deployment.yaml")[0]
    assert deploy["spec"]["replicas"] >= 2


def test_runner_image_is_not_latest() -> None:
    deploy = _load(ROOT / "mac-runner" / "deployment.yaml")[0]
    image = deploy["spec"]["template"]["spec"]["containers"][0]["image"]
    assert ":latest" not in image
