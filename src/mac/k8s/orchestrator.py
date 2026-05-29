from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import List, Optional

controller_loop_failures = 0

def _run_controller_loop_forever(
    mac: "object",
    jobs: "object",
    cfg: "object",
    interval: float,
    log: logging.Logger,
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
            time.sleep(interval)
    except Exception:  # noqa: BLE001
        controller_loop_failures += 1
        log.exception(
            "controller loop terminated unexpectedly; "
            "stuck-Job reconciliation is now OFFLINE in this pod "
            "(runner loop continues). controller_loop_failures=%d",
            controller_loop_failures,
        )

def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("mac-k8s-orchestrator")

    from mac.hermes_adapter import MacApiClient
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
    token = os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_API_TOKEN", "")
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
        },
        name="mac-orchestrator-controller",
        daemon=True,
    )
    controller_thread.start()

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
