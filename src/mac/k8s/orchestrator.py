"""Kubernetes orchestration loops for the MAC runner.

Drives the long-running controller and review loops that schedule and reconcile
MAC task execution on Kubernetes, tracking loop failures and pacing iterations.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

controller_loop_failures = 0
review_loop_failures = 0
review_tick_loop_failures = 0

# Keep the sleep seam local to this module. Patching ``time.sleep`` mutates the
# process-wide time module and lets unrelated background threads consume test
# counters or terminate controller loops nondeterministically.
_sleep = time.sleep


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() not in {"", "0", "false", "no", "off"}


def _run_review_tick_loop_forever(
    mac: "object",
    interval: float,
    limit: int,
    actor: str,
    log: logging.Logger,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Periodically OPEN reviews for tasks in needs_review/reviewing.

    The review-dispatch loop only *claims* existing reviewer nudges; it
    does not *open* the first review. The control plane's heartbeat-driven
    tick requires a configured hub agent that actually heartbeats — which
    the K8s deployment does not provide. Without an explicit tick a task
    that reaches needs_review opens no review, emits no reviewer nudge, and
    stalls forever. This loop drives ``POST /reviews/default/tick`` so the
    default-review workflow advances autonomously.
    """
    global review_tick_loop_failures
    from urllib.parse import quote  # noqa: WPS433

    path = "/reviews/default/tick?limit=%d&actor=%s" % (
        max(1, int(limit)),
        quote(actor, safe=""),
    )
    try:
        while True:
            try:
                result = mac.post(path, {})
                if isinstance(result, dict):
                    processed = result.get("processed")
                    if processed:
                        log.info("review-tick: processed=%s", processed)
            except Exception:  # noqa: BLE001
                review_tick_loop_failures += 1
                log.warning(
                    "review-tick iteration failed (will retry): "
                    "review_tick_loop_failures=%d",
                    review_tick_loop_failures,
                )
            sleep_fn(interval)
    except Exception:  # noqa: BLE001
        review_tick_loop_failures += 1
        log.exception(
            "review-tick loop terminated unexpectedly; needs_review tasks "
            "will NOT auto-advance in this pod until restart. "
            "review_tick_loop_failures=%d",
            review_tick_loop_failures,
        )


def _run_controller_loop_forever(
    mac: "object",
    jobs: "object",
    cfg: "object",
    interval: float,
    log: logging.Logger,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    global controller_loop_failures

    from mac.k8s.controller import reconcile_stuck_jobs  # noqa: WPS433

    try:
        while True:
            try:
                stuck = reconcile_stuck_jobs(mac, jobs, cfg)
                for s in stuck:
                    if s.get("status") in ("deleted", "delete-failed"):
                        log.info("stuck-job: %s", s)
            except Exception:  # noqa: BLE001
                controller_loop_failures += 1
                log.exception("controller reconcile iteration failed")
            sleep_fn(interval)
    except Exception:  # noqa: BLE001
        controller_loop_failures += 1
        log.exception(
            "controller loop terminated unexpectedly; "
            "stuck-Job reconciliation is now OFFLINE in this pod "
            "(runner loop continues). controller_loop_failures=%d",
            controller_loop_failures,
        )


def _run_review_loop_forever(
    mac: "object",
    jobs: "object",
    cfg: "object",
    log: logging.Logger,
) -> None:
    global review_loop_failures
    from mac.k8s.runner import review_loop  # noqa: WPS433
    try:
        review_loop(mac, jobs, cfg)
    except Exception:  # noqa: BLE001
        review_loop_failures += 1
        log.exception(
            "review-dispatch loop crashed; review nudges will NOT be "
            "consumed in this pod until restart. review_loop_failures=%d",
            review_loop_failures,
        )


def _report_bound_worker_credential(mac: Any, agent_id: str, log: logging.Logger) -> bool:
    """Publish secret-free proof using the already-authenticated bound token."""

    from mac.worker_credentials import credential_resource_from_env

    proof = credential_resource_from_env(agent_id)
    if not proof or proof.get("mode") != "bound":
        return False
    try:
        current = mac.get("/agents/%s" % agent_id)
        resources = dict((current or {}).get("resources") or {})
        resources["worker_credential"] = proof
        source_commit = (
            os.environ.get("MAC_WORKER_CREDENTIAL_SOURCE_COMMIT") or ""
        ).strip()
        if source_commit:
            resources["source_state"] = {
                "schema": "mac.worker_source_state.v1",
                "commit_sha": source_commit,
                "tree_sha": "",
                "dirty": False,
                "source": "pinned_k8s_credential",
                "observed_at": proof["observed_at"],
            }
        payload: Dict[str, Any] = {"resources": resources}
        runtime_digest = (
            os.environ.get("MAC_WORKER_CREDENTIAL_RUNTIME_DIGEST") or ""
        ).strip()
        if runtime_digest:
            payload["running_digest"] = runtime_digest
        mac.post("/agents/%s/heartbeat" % agent_id, payload)
        return True
    except Exception as exc:  # noqa: BLE001 - readiness remains fail-closed
        log.warning("bound worker credential heartbeat failed for %s: %s", agent_id, exc)
        return False


def _credential_heartbeat_loop(
    mac: Any,
    agent_id: str,
    *,
    interval: float,
    log: logging.Logger,
) -> None:
    while True:
        _report_bound_worker_credential(mac, agent_id, log)
        _sleep(interval)


def main(argv: Optional[List[str]] = None) -> int:
    """Run the Kubernetes orchestrator entry point and return its exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("mac-k8s-orchestrator")

    from mac.api_client import MacApiClient
    from mac.k8s.controller import ControllerConfig
    from mac.k8s.k8s_client import K8sJobsClient, load_in_cluster_config
    from mac.k8s.runner import (
        RunnerConfig,
        check_dispatcher_capabilities,
        runner_loop,
    )

    runner_cfg = RunnerConfig.from_env()
    if not runner_cfg.mac_url:
        log.error("MAC_URL is required")
        return 2
    # Resolve fleet-aware so a legacy flat MAC_WORKER_TOKEN/MAC_API_TOKEN can't
    # shadow the scoped MAC_*__<FLEET> form. RunnerConfig has no fleet field, so
    # derive the active fleet from MAC_FLEET (mac-g55y).
    from mac.fleet_env import resolve_first

    token = resolve_first(
        ["MAC_WORKER_TOKEN", "MAC_API_TOKEN"],
        fleet=os.environ.get("MAC_FLEET"),
    ) or ""
    if not token:
        log.error("MAC_WORKER_TOKEN (or MAC_API_TOKEN) is required")
        return 2

    load_in_cluster_config()
    mac = MacApiClient(runner_cfg.mac_url, token=token)
    jobs_client = K8sJobsClient()

    log.info(
        "mac-k8s-orchestrator starting: agent=%s namespace=%s mac_url=%s capabilities=%s",
        runner_cfg.agent_id,
        runner_cfg.namespace,
        runner_cfg.mac_url,
        runner_cfg.capability_filter or "<any>",
    )

    if os.environ.get("MAC_WORKER_CREDENTIAL_ID") and os.environ.get(
        "MAC_WORKER_CREDENTIAL_AGENT_ID"
    ):
        # Report once synchronously so rollout inventory need not wait a full
        # interval, then keep liveness/proof fresh. Compatibility pods omit the
        # metadata and deliberately skip this path.
        _report_bound_worker_credential(mac, runner_cfg.agent_id, log)
        credential_thread = threading.Thread(
            target=_credential_heartbeat_loop,
            kwargs={
                "mac": mac,
                "agent_id": runner_cfg.agent_id,
                "interval": float(
                    os.environ.get("MAC_WORKER_HEARTBEAT_INTERVAL", "30")
                ),
                "log": logging.getLogger("mac-k8s-orchestrator.credential"),
            },
            name="mac-orchestrator-credential-heartbeat",
            daemon=True,
        )
        credential_thread.start()

    missing = check_dispatcher_capabilities(runner_cfg, mac)
    if missing:
        log.warning(
            "dispatcher capability drift: dispatcher %s is missing %s. "
            "Tasks requiring these capabilities will never claim. "
            "Update AGENT_CAPABILITIES on register-mac-agent.",
            runner_cfg.agent_id,
            missing,
        )

    namespace = os.environ.get("MAC_RUNNER_NAMESPACE", runner_cfg.namespace)
    interval = float(os.environ.get("MAC_CONTROLLER_INTERVAL_SECONDS", "30"))
    controller_cfg = ControllerConfig(
        namespace=namespace, reconcile_interval_seconds=interval
    )

    log.info(
        "controller loop: namespace=%s interval=%.1fs",
        namespace,
        interval,
    )

    controller_thread = threading.Thread(
        target=_run_controller_loop_forever,
        kwargs={
            "mac": mac,
            "jobs": jobs_client,
            "cfg": controller_cfg,
            "interval": interval,
            "log": logging.getLogger("mac-k8s-orchestrator.controller"),
            "sleep_fn": _sleep,
        },
        name="mac-orchestrator-controller",
        daemon=True,
    )
    controller_thread.start()

    if runner_cfg.reviewer_agent_ids:
        log.info(
            "review-dispatch loop: reviewers=%s",
            sorted(runner_cfg.reviewer_agent_ids.values()),
        )
        review_thread = threading.Thread(
            target=_run_review_loop_forever,
            kwargs={
                "mac": mac,
                "jobs": jobs_client,
                "cfg": runner_cfg,
                "log": logging.getLogger("mac-k8s-orchestrator.review"),
            },
            name="mac-orchestrator-review",
            daemon=True,
        )
        review_thread.start()
    else:
        log.info(
            "review-dispatch loop: no reviewer roles configured "
            "(no role has the 'review' capability) — skipping"
        )

    # Review-tick loop: opens reviews for needs_review tasks. Enabled by
    # default; disable with MAC_REVIEW_TICK_LOOP_ENABLED=0.
    if _truthy(os.environ.get("MAC_REVIEW_TICK_LOOP_ENABLED", "1")):
        tick_interval = float(
            os.environ.get("MAC_REVIEW_TICK_INTERVAL_SECONDS", "30")
        )
        try:
            tick_limit = int(os.environ.get("MAC_REVIEW_TICK_LIMIT", "25"))
        except ValueError:
            tick_limit = 25
        log.info(
            "review-tick loop: interval=%.1fs limit=%d actor=%s",
            tick_interval,
            tick_limit,
            runner_cfg.agent_id,
        )
        review_tick_thread = threading.Thread(
            target=_run_review_tick_loop_forever,
            kwargs={
                "mac": mac,
                "interval": tick_interval,
                "limit": tick_limit,
                "actor": runner_cfg.agent_id,
                "log": logging.getLogger("mac-k8s-orchestrator.review-tick"),
                "sleep_fn": _sleep,
            },
            name="mac-orchestrator-review-tick",
            daemon=True,
        )
        review_tick_thread.start()
    else:
        log.info("review-tick loop: disabled by MAC_REVIEW_TICK_LOOP_ENABLED=0")

    try:
        runner_loop(mac, jobs_client, runner_cfg)
    except KeyboardInterrupt:
        log.info("interrupted; exiting cleanly")
        return 0
    except Exception:  # noqa: BLE001
        log.exception("runner loop crashed; exiting non-zero for pod restart")
        return 1
    return 0

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
