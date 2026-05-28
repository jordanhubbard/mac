"""Structural sanity checks for the K8s manifests under deploy/k8s/.

These tests are intentionally cheap: they parse the YAML and assert
invariants we care about (image not :latest, replicas > 1, both probes
present, env wired to the right secret keys, etc.). They are NOT a
substitute for `kubectl apply --dry-run` against a real cluster — that
belongs to the deploy pipeline, not the unit suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

yaml = pytest.importorskip("yaml")


ROOT = Path(__file__).resolve().parent.parent / "deploy" / "k8s"


def _load(path: Path) -> List[dict]:
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d]
    return docs


def test_cnpg_cluster_uses_postgres17_image() -> None:
    docs = _load(ROOT / "cnpg" / "cluster.yaml")
    assert len(docs) == 1
    spec = docs[0]
    assert spec["kind"] == "Cluster"
    assert spec["apiVersion"].startswith("postgresql.cnpg.io")
    image = spec["spec"]["imageName"]
    assert ":17" in image, "CNPG cluster must be Postgres 17 per K8s Phase 3 goal"


def test_cnpg_cluster_has_three_instances() -> None:
    spec = _load(ROOT / "cnpg" / "cluster.yaml")[0]
    assert spec["spec"]["instances"] >= 3, "HA needs at least 3 replicas"


def test_cnpg_cluster_bootstraps_mac_database() -> None:
    spec = _load(ROOT / "cnpg" / "cluster.yaml")[0]
    initdb = spec["spec"]["bootstrap"]["initdb"]
    assert initdb["database"] == "mac"
    assert initdb["owner"] == "mac"


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
    # CNPG creates `mac-pg-app` from its bootstrap.initdb.secret config.
    assert secret_ref["name"] == "mac-pg-app"


def test_mac_api_env_wires_secret_key() -> None:
    deploy = _load(ROOT / "mac-api" / "deployment.yaml")[0]
    env = deploy["spec"]["template"]["spec"]["containers"][0]["env"]
    sk = next(e for e in env if e["name"] == "MAC_SECRET_KEY")
    assert sk["valueFrom"]["secretKeyRef"]["name"] == "mac-api-config"


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
    cnpg_kust = _load(ROOT / "cnpg" / "kustomization.yaml")[0]
    assert "cluster.yaml" in cnpg_kust["resources"]

    api_kust = _load(ROOT / "mac-api" / "kustomization.yaml")[0]
    for res in ("namespace.yaml", "deployment.yaml", "service.yaml"):
        assert res in api_kust["resources"]


def test_argocd_application_targets_repo_paths() -> None:
    docs = _load(ROOT / "argocd" / "application.yaml")
    # Two Applications: one for CNPG, one for mac-api.
    assert len(docs) == 2
    paths = [d["spec"]["source"]["path"] for d in docs]
    assert "deploy/k8s/cnpg" in paths
    assert "deploy/k8s/mac-api" in paths
