"""Kubernetes-native execution layer for mac.

Two binaries live in this package:

* ``mac-k8s-runner`` — long-running Deployment that polls mac-api for
  ready tasks, claims them, and creates one Kubernetes ``Job`` per
  claimed lease.
* ``mac-task-runner`` — single-shot in-Job executor invoked by the Jobs
  the runner creates. Reads ``MAC_TASK_ID`` / ``MAC_LEASE_ID`` from
  env, runs the configured executor command, records evidence, and
  transitions the task to ``needs_review`` or ``failed`` before exit.

Plus a controller layer for Phase 5:

* ``mac-k8s-controller`` — reconciles provisioning requests against
  worker pool scale and kills stuck Jobs whose lease has already
  expired.
"""

from mac.k8s.runner import (  # noqa: F401
    RunnerConfig,
    build_job_spec,
    claim_and_launch_one,
)
from mac.k8s.job_executor import (  # noqa: F401
    JobExecutionResult,
    run_one_lease,
)
from mac.k8s.controller import (  # noqa: F401
    reconcile_stuck_jobs,
    reconcile_provisioning_requests,
)

__all__ = [
    "RunnerConfig",
    "build_job_spec",
    "claim_and_launch_one",
    "JobExecutionResult",
    "run_one_lease",
    "reconcile_stuck_jobs",
    "reconcile_provisioning_requests",
]
