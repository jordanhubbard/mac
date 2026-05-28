"""Console-script entry points for the K8s Phase 4/5 binaries.

* ``mac-k8s-runner`` -> ``runner_main``: long-running claim+launch loop.
* ``mac-k8s-controller`` -> ``controller_main``: stuck-Job +
  provisioning-request reconciler.

``mac-task-runner`` is the single-shot Job-side executor; its main lives
in ``mac.k8s.job_executor.main`` and is exposed directly in
pyproject.toml.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import List, Optional


def runner_main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("mac-k8s-runner")

    from mac.hermes_adapter import MacApiClient
    from mac.k8s.k8s_client import K8sJobsClient, load_in_cluster_config
    from mac.k8s.runner import RunnerConfig, runner_loop

    cfg = RunnerConfig.from_env()
    if not cfg.mac_url:
        log.error("MAC_URL is required")
        return 2
    token = os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_API_TOKEN", "")
    if not token:
        log.error("MAC_WORKER_TOKEN (or MAC_API_TOKEN) is required")
        return 2

    load_in_cluster_config()
    mac = MacApiClient(cfg.mac_url, token=token)
    k8s = K8sJobsClient()

    log.info(
        "mac-k8s-runner starting: agent=%s namespace=%s mac_url=%s capabilities=%s",
        cfg.agent_id,
        cfg.namespace,
        cfg.mac_url,
        cfg.capability_filter or "<any>",
    )
    try:
        runner_loop(mac, k8s, cfg)
    except KeyboardInterrupt:
        log.info("interrupted; exiting cleanly")
    return 0


def controller_main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("mac-k8s-controller")

    from mac.hermes_adapter import MacApiClient
    from mac.k8s.controller import (
        ControllerConfig,
        K8sDeploymentScaler,
        reconcile_provisioning_requests,
        reconcile_stuck_jobs,
    )
    from mac.k8s.k8s_client import (
        K8sDeploymentsClient,
        K8sJobsClient,
        load_in_cluster_config,
    )

    mac_url = os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL", "")
    token = os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_API_TOKEN", "")
    if not mac_url or not token:
        log.error("MAC_URL and MAC_WORKER_TOKEN are required")
        return 2

    namespace = os.environ.get("MAC_RUNNER_NAMESPACE", "mac")
    interval = float(os.environ.get("MAC_CONTROLLER_INTERVAL_SECONDS", "30"))
    enable_scaler = os.environ.get("MAC_CONTROLLER_SCALER_ENABLED", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    load_in_cluster_config()
    mac = MacApiClient(mac_url, token=token)
    jobs = K8sJobsClient()
    deploys = K8sDeploymentsClient()
    cfg = ControllerConfig(namespace=namespace, reconcile_interval_seconds=interval)
    scaler = K8sDeploymentScaler(deploys, namespace=namespace)

    log.info(
        "mac-k8s-controller starting: namespace=%s interval=%.1fs scaler=%s",
        namespace,
        interval,
        "enabled" if enable_scaler else "disabled",
    )
    try:
        while True:
            stuck = reconcile_stuck_jobs(mac, jobs, cfg)
            for s in stuck:
                if s.get("status") in ("deleted", "delete-failed"):
                    log.info("stuck-job: %s", s)
            if enable_scaler:
                prov = reconcile_provisioning_requests(mac, cfg, scaler)
                for p in prov:
                    if p.get("status") in ("scaled", "scaler-error"):
                        log.info("provisioning: %s", p)
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted; exiting cleanly")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(runner_main(sys.argv[1:]))
