"""Hub-mediated curiosity access, so adjudication is possible at all.

The quarantine ledger lives at ``<state_dir>/mac-curiosity`` INSIDE the
``mac-openclaw-<agent>`` sandbox, reachable only through
``/usr/local/bin/curiosity`` in that same sandbox. A dispatched task runs in a
freshly created ``mac-task-*`` sandbox: different namespace, neither the CLI
nor the store, and no route to the host's ``openshell`` to get them.

So every adjudication task ever filed against the quarantine was unsatisfiable.
``curiosity_reviewer`` pins its tasks to the owning agent, which fixes the HOST
and not the NAMESPACE -- on 2026-08-05 three attempts failed for exactly this,
including one that ran on the correct host and still could not see the ledger
(task_3a4503f0).

The hub sits in the one place that works: on the agent's host, able to invoke
``~/.mac/bin/curiosity``, and reachable over HTTP from every task sandbox.

A read-only copy mounted into the task sandbox would have been a trap: it makes
enumeration work while approve/reject stays broken, which looks like a fix and
is half of one. These tests therefore cover the WRITE path as carefully as the
read path.

The subprocess runner is injected, so these exercise this module's argument
construction, validation, parsing and error mapping rather than a stub CLI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac.curiosity_service import (
    CURIOSITY_DECISIONS,
    CURIOSITY_STATUSES,
    CuriosityCommandError,
    CuriosityConfig,
    CuriosityService,
    CuriosityUnavailable,
)


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _wrapper(tmp_path: Path) -> Path:
    path = tmp_path / "curiosity"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _service(tmp_path: Path, result=None, calls=None, raises=None):
    def runner(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return result if result is not None else _Result(stdout="[]")

    return CuriosityService(CuriosityConfig(wrapper_path=_wrapper(tmp_path)), runner=runner)


# -- availability ----------------------------------------------------------


def test_a_host_without_the_wrapper_is_unavailable_not_broken(tmp_path):
    """An agent host that never ran an OpenClaw gateway has no ledger.

    That is a fact about the host, not a fault to retry, so it must be
    distinguishable from a command that ran and failed.
    """
    service = CuriosityService(CuriosityConfig(wrapper_path=tmp_path / "absent"))
    assert service.available() is False
    with pytest.raises(CuriosityUnavailable):
        service.list_candidates()


def test_a_non_executable_wrapper_is_unavailable(tmp_path):
    path = tmp_path / "curiosity"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o644)
    service = CuriosityService(CuriosityConfig(wrapper_path=path))
    assert service.available() is False


# -- read ------------------------------------------------------------------


def test_listing_returns_the_candidates_verbatim(tmp_path):
    payload = [
        {"id": "cur_1", "confidence": "high", "hypothesis": "h1"},
        {"id": "cur_2", "confidence": "low", "hypothesis": "h2"},
    ]
    calls = []
    service = _service(tmp_path, result=_Result(stdout=json.dumps(payload)), calls=calls)

    out = service.list_candidates("quarantined")

    assert out["count"] == 2
    assert out["candidates"] == payload, "candidate records must not be reshaped"
    assert out["status"] == "quarantined"
    argv = calls[0][0]
    assert argv[1:] == ["list", "--status", "quarantined"]


def test_listing_without_a_status_does_not_pass_the_flag(tmp_path):
    calls = []
    service = _service(tmp_path, result=_Result(stdout="[]"), calls=calls)
    service.list_candidates()
    assert calls[0][0][1:] == ["list"]


def test_an_unknown_status_is_refused_before_invoking_anything(tmp_path):
    calls = []
    service = _service(tmp_path, calls=calls)
    with pytest.raises(ValueError):
        service.list_candidates("bogus")
    assert not calls, "a bad status must not reach a sandbox exec"


def test_empty_output_is_an_empty_list_not_a_parse_error(tmp_path):
    service = _service(tmp_path, result=_Result(stdout="   "))
    assert service.list_candidates()["candidates"] == []


def test_non_json_output_is_reported_as_such(tmp_path):
    service = _service(tmp_path, result=_Result(stdout="Error: sandbox gone"))
    with pytest.raises(CuriosityCommandError) as excinfo:
        service.list_candidates()
    assert "not JSON" in str(excinfo.value)


# -- write -----------------------------------------------------------------


@pytest.mark.parametrize("decision", CURIOSITY_DECISIONS)
def test_a_decision_passes_the_full_audit_trail(tmp_path, decision):
    """actor/reason/approval_id are the point of external judgment."""
    calls = []
    service = _service(tmp_path, result=_Result(stdout=""), calls=calls)

    out = service.decide(
        decision,
        "cur_abc",
        actor="agent_rocky",
        reason="reproducible and useful",
        approval_id="task_123",
    )

    argv = calls[0][0]
    assert argv[1] == decision
    assert argv[2] == "cur_abc"
    assert argv[3:] == [
        "--actor",
        "agent_rocky",
        "--reason",
        "reproducible and useful",
        "--approval-id",
        "task_123",
    ]
    assert out["decision"] == decision
    assert out["approval_id"] == "task_123"


@pytest.mark.parametrize("missing", ["actor", "reason", "approval_id"])
def test_a_decision_missing_any_audit_field_is_refused(tmp_path, missing):
    """Dropping any of the three would defeat the external-judgment design."""
    calls = []
    service = _service(tmp_path, calls=calls)
    fields = {
        "actor": "agent_rocky",
        "reason": "because",
        "approval_id": "task_123",
    }
    fields[missing] = "   "
    with pytest.raises(ValueError) as excinfo:
        service.decide("approve", "cur_abc", **fields)
    assert missing in str(excinfo.value)
    assert not calls, "an incomplete decision must not reach the ledger"


def test_an_unknown_decision_is_refused(tmp_path):
    calls = []
    service = _service(tmp_path, calls=calls)
    with pytest.raises(ValueError):
        service.decide("delete", "cur_abc", actor="a", reason="r", approval_id="t")
    assert not calls, "only approve/reject may be proxied"


def test_submission_is_not_proxied():
    """The sidecar withholds approve/reject from the submitter on purpose.

    This service exists to supply the missing external judgment, not to widen
    the submission path, so 'submit' must not be reachable through it.
    """
    assert "submit" not in CURIOSITY_DECISIONS
    assert set(CURIOSITY_DECISIONS) == {"approve", "reject"}


def test_an_empty_candidate_id_is_refused(tmp_path):
    calls = []
    service = _service(tmp_path, calls=calls)
    with pytest.raises(ValueError):
        service.decide("approve", "  ", actor="a", reason="r", approval_id="t")
    assert not calls


# -- failure mapping -------------------------------------------------------


def test_a_failing_command_carries_the_cli_stderr(tmp_path):
    service = _service(tmp_path, result=_Result(returncode=2, stderr="candidate not found"))
    with pytest.raises(CuriosityCommandError) as excinfo:
        service.decide("approve", "cur_missing", actor="a", reason="r", approval_id="t")
    assert "candidate not found" in str(excinfo.value)


def test_a_hung_sandbox_exec_times_out_rather_than_pinning_the_hub(tmp_path):
    service = _service(tmp_path, raises=subprocess.TimeoutExpired(cmd="curiosity", timeout=60))
    with pytest.raises(CuriosityCommandError) as excinfo:
        service.list_candidates()
    assert "timed out" in str(excinfo.value)


def test_the_hub_environment_is_not_handed_to_the_sandbox(tmp_path):
    """The wrapper execs into a sandbox; hub credentials have no business there."""
    calls = []
    service = _service(tmp_path, result=_Result(stdout="[]"), calls=calls)
    service.list_candidates()
    env = calls[0][1]["env"]
    assert set(env) <= {"PATH", "HOME"}, (
        "only PATH/HOME may cross into the sandbox exec, got %s" % sorted(env)
    )


# -- config ----------------------------------------------------------------


def test_the_wrapper_path_is_configurable(tmp_path):
    config = CuriosityConfig.from_env({"MAC_CURIOSITY_WRAPPER": str(tmp_path / "c")})
    assert config.wrapper_path == tmp_path / "c"


def test_the_default_wrapper_follows_mac_home(tmp_path):
    config = CuriosityConfig.from_env({"MAC_HOME": str(tmp_path)})
    assert config.wrapper_path == tmp_path / "bin" / "curiosity"


def test_the_timeout_is_clamped(tmp_path):
    """A hung exec must not hold a hub request open indefinitely."""
    assert (
        CuriosityConfig.from_env({"MAC_CURIOSITY_TIMEOUT_SECONDS": "99999"}).timeout_seconds
        == 600.0
    )
    assert CuriosityConfig.from_env({"MAC_CURIOSITY_TIMEOUT_SECONDS": "0"}).timeout_seconds == 1.0
    assert (
        CuriosityConfig.from_env({"MAC_CURIOSITY_TIMEOUT_SECONDS": "not-a-number"}).timeout_seconds
        == 60.0
    )


def test_statuses_match_the_sidecar_cli():
    """These are passed through to `curiosity list --status`; drift breaks it."""
    assert set(CURIOSITY_STATUSES) == {"quarantined", "approved", "rejected"}
