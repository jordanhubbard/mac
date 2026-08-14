"""An approved task has to be able to publish.

Publication re-runs a repository contract gate on the projected current-main
merge, inside a sandbox, under MAC_HUB_VERIFY_TIMEOUT. That gate ran the WHOLE
suite -- about 45 minutes on this repository -- against a 1200-second cap. It
could not finish, so no approved task could publish at all: the gate was
killed, publication failed, it retried ~1200s later and failed again, and the
task sat in REVIEWING, approved and unpublished.

Measured on task_de42aa6c: review approved at 19:36:51, publication failed at
20:02:44, and again at 20:23:43.

The failure surfaced as a truncated CalledProcessError naming the openshell
argv, which reads like a sandbox fault. It is not one: `openshell sandbox
create ... -- <cmd>` works on that host. A day went into the wrong suspect.

The projected tree differs from the tree review already gated only by however
far main moved, so the changed-file selection is the honest question to ask of
it -- and it is the same question the review verifier asks, through the same
helper.
"""

from __future__ import annotations

import inspect

from mac import services


def test_publication_scopes_its_gate_the_way_review_does():
    """Hardcoding the full command is what made the cap unmeetable."""
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "_hub_review_test_command" in source, (
        "the projected-merge gate must scope its command like the review "
        "verifier; running the whole suite cannot finish inside "
        "MAC_HUB_VERIFY_TIMEOUT"
    )


def test_an_unreadable_diff_still_runs_everything():
    """Fail closed. If the projected diff cannot be read, the scoped question
    is unanswerable and the whole suite is the only honest gate."""
    source = inspect.getsource(services.ControlPlane._publish_git_target_attempt)

    assert "projected_changed = []" in source
    assert "except Exception" in source


def test_the_timeout_can_cover_the_work_it_gates():
    """A cap the work cannot meet is not a gate, it is an outage that reports
    itself as a gate failure. The scoped run alone takes ~15 minutes before
    clone, upload and dependency bootstrap."""
    source = inspect.getsource(services.ControlPlane._hub_verify_run_contract_test)

    assert '"2400"' in source, (
        "MAC_HUB_VERIFY_TIMEOUT's default must cover a scoped gate plus its "
        "setup; 1200s did not"
    )
