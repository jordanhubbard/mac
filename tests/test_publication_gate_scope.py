"""Publication runs no tests on the hub.

Publication used to re-run a repository contract gate on the projected
current-main merge, inside a sandbox, under MAC_HUB_VERIFY_TIMEOUT. That gate
ran the WHOLE suite -- about 45 minutes on this repository -- and approved work
sat in REVIEWING behind it (task_de42aa6c: approved 19:36:51, publication
failed 20:02:44 and again 20:23:43).

The serial land loop removed it. One test gate decides each landing: the
repository's required checks where it has them, otherwise the worker's own
verifier run. When the canonical tip has moved past the base the worker
verified, the task goes back to its worker to rebase and retest; the hub does
not run the contract itself.
"""

from __future__ import annotations

import inspect

from mac import services


def test_the_land_step_runs_no_contract_gate():
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)
    source += inspect.getsource(services.ControlPlane._publish_via_pull_request)

    assert "run_repository_contract_test_in_openshell" not in source
    assert "validate_projected_merge_contract" not in source
    assert not hasattr(services.ControlPlane, "_run_contract_gate")


def test_the_worker_verifier_timeout_can_cover_the_work_it_gates():
    """A cap the work cannot meet is not a gate, it is an outage that reports
    itself as a gate failure. The worker's verifier runs in the same sandbox
    runner, and a scoped run alone takes ~15 minutes before clone, upload and
    dependency bootstrap."""
    source = inspect.getsource(services.run_repository_contract_test_in_openshell)

    assert '"2400"' in source, (
        "MAC_HUB_VERIFY_TIMEOUT's default must cover a scoped gate plus its setup; 1200s did not"
    )
