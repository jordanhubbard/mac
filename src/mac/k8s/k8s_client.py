
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    from kubernetes import client as k8s_client, config as k8s_config
    from kubernetes.client.rest import ApiException
except ImportError as exc:  # pragma: no cover - exercised by env probe
    raise ImportError(
        "k8s_client requires the kubernetes package. "
        "Install via 'pip install \"mac[k8s]\"'."
    ) from exc

JsonDict = Dict[str, Any]


def load_in_cluster_config() -> None:
    """Load in-cluster Kubernetes configuration, falling back to kubeconfig."""
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


class K8sJobsClient:
    """Adapter satisfying ``K8sJobsProtocol``."""

    def __init__(self) -> None:
        self._batch = k8s_client.BatchV1Api()

    def create(self, namespace: str, manifest: JsonDict) -> JsonDict:
        result = self._batch.create_namespaced_job(
            namespace=namespace, body=manifest
        )
        # Return a slim dict matching the protocol's contract.
        return _to_dict(result)

    def list_active(self, namespace: str, label_selector: str) -> List[JsonDict]:
        result = self._batch.list_namespaced_job(
            namespace=namespace, label_selector=label_selector
        )
        out: List[JsonDict] = []
        for item in result.items:
            j = _to_dict(item)
            status = j.get("status") or {}
            conditions = status.get("conditions") or []
            terminal = any(
                (c.get("type") in ("Complete", "Failed") and c.get("status") == "True")
                for c in conditions
            )
            if terminal:
                continue
            out.append(j)
        return out

    def delete(self, namespace: str, name: str) -> None:
        try:
            self._batch.delete_namespaced_job(
                name=name,
                namespace=namespace,
                propagation_policy="Background",
            )
        except ApiException as exc:
            if exc.status == 404:
                return
            raise

    def read(self, namespace: str, name: str) -> JsonDict:
        try:
            result = self._batch.read_namespaced_job(
                name=name, namespace=namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return {}
            raise
        return _to_dict(result)


class K8sDeploymentsClient:
    """Adapter satisfying ``K8sDeploymentsProtocol``."""

    def __init__(self) -> None:
        self._apps = k8s_client.AppsV1Api()

    def get_deployment(self, namespace: str, name: str) -> Optional[JsonDict]:
        try:
            result = self._apps.read_namespaced_deployment(
                name=name, namespace=namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        return _to_dict(result)

    def scale_deployment(self, namespace: str, name: str, replicas: int) -> None:
        body = {"spec": {"replicas": int(replicas)}}
        self._apps.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body=body
        )


def _to_dict(obj: Any) -> JsonDict:
    if hasattr(obj, "to_dict"):
        return _strip_none(obj.to_dict())
    if isinstance(obj, dict):
        return _strip_none(obj)
    return {"value": obj}


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_none(v) for v in value]
    return value
