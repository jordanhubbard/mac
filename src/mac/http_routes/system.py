"""Health and autonomous-controller HTTP routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel


class RepositoryRefReconcileRequest(BaseModel):
    mode: Optional[str] = None
    actor: str = "operator"


@dataclass(frozen=True)
class SystemRouteServices:
    repository_ref_reconciler: Any
    github_ingestor: Any
    backlog_groomer: Any
    nap_ticker: Any
    curiosity_reviewer: Any
    self_healing_sentinel: Any
    model_selection_service: Any


def build_system_router(
    services: SystemRouteServices,
    *,
    get_principal: Callable[..., Any],
) -> APIRouter:
    """Build routes using explicit controller dependencies."""

    router = APIRouter()

    def require_global_fleet(principal: Any) -> None:
        principal.require_global_fleet()

    @router.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @router.get("/repository-refs/reconciler")
    def repository_ref_reconciler_status() -> Dict[str, Any]:
        return services.repository_ref_reconciler.status()

    @router.post("/repository-refs/reconcile")
    def reconcile_repository_refs(
        body: RepositoryRefReconcileRequest,
        principal: Any = Depends(get_principal),
    ) -> Dict[str, Any]:
        require_global_fleet(principal)
        return services.repository_ref_reconciler.run_once(
            mode=body.mode,
            actor=body.actor,
            trigger="operator",
        )

    def controller_routes(prefix: str, controller: Any) -> None:
        @router.get("/%s/status" % prefix, name="%s_status" % prefix.replace("-", "_"))
        def status() -> Dict[str, Any]:
            return controller.status()

        @router.post("/%s/run" % prefix, name="%s_run" % prefix.replace("-", "_"))
        def run(principal: Any = Depends(get_principal)) -> Dict[str, Any]:
            require_global_fleet(principal)
            return controller.run_once(trigger="operator")

    controller_routes("github-ingest", services.github_ingestor)
    controller_routes("backlog-groom", services.backlog_groomer)
    controller_routes("nap-tick", services.nap_ticker)
    controller_routes("curiosity-review", services.curiosity_reviewer)
    controller_routes("self-heal", services.self_healing_sentinel)

    @router.get("/model-selection/status")
    def model_selection_status() -> Dict[str, Any]:
        return services.model_selection_service.status()

    @router.post("/model-selection/refresh")
    def model_selection_refresh(
        principal: Any = Depends(get_principal),
    ) -> Dict[str, Any]:
        require_global_fleet(principal)
        return services.model_selection_service.run_once(trigger="operator")

    @router.post("/model-selection/promote")
    def model_selection_promote(
        principal: Any = Depends(get_principal),
    ) -> Dict[str, Any]:
        require_global_fleet(principal)
        return services.model_selection_service.promote(actor="operator")

    @router.get("/.well-known/acp")
    def acp_manifest_route() -> Dict[str, Any]:
        from mac.acp.capabilities import acp_manifest

        return acp_manifest()

    return router
