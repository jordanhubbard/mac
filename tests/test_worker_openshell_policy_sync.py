"""Worker-side delivery of the hub-assigned OpenShell policy, and audit spooling.

Before these landed, `mac openshell policy assign` recorded intent that never
reached a running worker (~/.mac/openshell-policy.yaml was written once at
provision time by bootstrap-openshell.sh), and command-audit records were
dropped silently whenever the hub was unreachable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mac import worker
from mac.openshell_service import policy_checksum

POLICY_TEXT = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


class _Client:
    """Records posts; serves a scripted GET, or raises to simulate an outage."""

    def __init__(self, *, policy=None, get_error=None, post_error=None):
        self.posts = []
        self.gets = []
        self._policy = policy
        self._get_error = get_error
        self.post_error = post_error

    def post(self, path, payload):
        if self.post_error is not None and path.endswith("/command-audit"):
            raise self.post_error
        self.posts.append((path, payload))
        return {}

    def get(self, path):
        self.gets.append(path)
        if self._get_error is not None:
            raise self._get_error
        if path.endswith("/openshell/policy") and self._policy is not None:
            return self._policy
        return {}


def _worker(tmp_path, client=None):
    return worker.MacWorker(
        client if client is not None else _Client(),
        "agent-1",
        tmp_path,
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def _assigned(text=POLICY_TEXT, version=3):
    return {
        "schema": "mac.openshell.assigned_policy.v1",
        "agent_id": "agent-1",
        "policy_id": "ospol_1",
        "policy_name": "fleet",
        "version": version,
        "checksum": policy_checksum(text),
        "policy_text": text,
    }


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "machome"
    home.mkdir()
    monkeypatch.setattr(worker.mac_paths, "mac_home", lambda: home)
    monkeypatch.delenv("MAC_OPENSHELL_POLICY", raising=False)
    return home


# --- policy delivery -------------------------------------------------------


def test_assigned_policy_is_installed_owner_only(mac_home, tmp_path):
    client = _Client(policy=_assigned())
    instance = _worker(tmp_path, client)

    instance._maybe_sync_openshell_policy()

    target = mac_home / "openshell-policy.yaml"
    assert target.read_text(encoding="utf-8") == POLICY_TEXT
    assert target.stat().st_mode & 0o777 == 0o600


def test_install_reports_convergence_to_the_hub(mac_home, tmp_path):
    """deploy-status must reflect what is on the host, not what was assigned."""
    client = _Client(policy=_assigned())
    instance = _worker(tmp_path, client)

    instance._maybe_sync_openshell_policy()

    status = next(payload for path, payload in client.posts if path.endswith("/openshell/status"))
    assert status["policy_id"] == "ospol_1"
    assert status["policy_version"] == 3
    assert status["status"] == "active"


def test_converged_policy_is_not_rewritten(mac_home, tmp_path):
    """The checksum short-circuit is what makes this cheap enough to run on
    every sweep; without it the file churns once a minute forever."""
    target = mac_home / "openshell-policy.yaml"
    target.write_text(POLICY_TEXT, encoding="utf-8")
    before = target.stat().st_mtime_ns

    client = _Client(policy=_assigned())
    instance = _worker(tmp_path, client)
    instance._maybe_sync_openshell_policy()

    assert target.stat().st_mtime_ns == before
    assert not [p for p, _ in client.posts if p.endswith("/openshell/status")]


def test_updated_assignment_replaces_an_older_policy(mac_home, tmp_path):
    target = mac_home / "openshell-policy.yaml"
    target.write_text("version: 1\n# superseded\n", encoding="utf-8")

    instance = _worker(tmp_path, _Client(policy=_assigned()))
    instance._maybe_sync_openshell_policy()

    assert target.read_text(encoding="utf-8") == POLICY_TEXT


def test_explicit_operator_override_is_never_overwritten(mac_home, tmp_path, monkeypatch):
    """MAC_OPENSHELL_POLICY outranks the hub assignment; silently replacing the
    file it points at would make the override a lie."""
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(tmp_path / "explicit.yaml"))
    client = _Client(policy=_assigned())
    instance = _worker(tmp_path, client)

    instance._maybe_sync_openshell_policy()

    assert not (mac_home / "openshell-policy.yaml").exists()
    assert client.gets == []


def test_sync_can_be_disabled(mac_home, tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_POLICY_SYNC", "0")
    client = _Client(policy=_assigned())
    _worker(tmp_path, client)._maybe_sync_openshell_policy()
    assert client.gets == []


def test_unreachable_hub_leaves_the_existing_policy_in_place(mac_home, tmp_path):
    """Fail-safe, not fail-open: an agent that cannot reach the hub keeps the
    guardrail it already had rather than losing confinement."""
    target = mac_home / "openshell-policy.yaml"
    target.write_text(POLICY_TEXT, encoding="utf-8")

    instance = _worker(tmp_path, _Client(get_error=RuntimeError("hub down")))
    instance._maybe_sync_openshell_policy()

    assert target.read_text(encoding="utf-8") == POLICY_TEXT


def test_agent_with_no_assignment_keeps_its_policy(mac_home, tmp_path):
    target = mac_home / "openshell-policy.yaml"
    target.write_text(POLICY_TEXT, encoding="utf-8")
    instance = _worker(tmp_path, _Client(policy=None))
    instance._maybe_sync_openshell_policy()
    assert target.read_text(encoding="utf-8") == POLICY_TEXT


@pytest.mark.parametrize(
    "payload",
    [
        {"policy_text": "", "checksum": "sha256:x"},
        {"policy_text": "   ", "checksum": "sha256:x"},
        {"policy_text": POLICY_TEXT, "checksum": ""},
        {"policy_text": None, "checksum": "sha256:x"},
        {},
    ],
)
def test_malformed_assignment_never_installs(mac_home, tmp_path, payload):
    """A truncated or checksum-less response must not become the guardrail."""
    instance = _worker(tmp_path, _Client(policy=payload))
    instance._maybe_sync_openshell_policy()
    assert not (mac_home / "openshell-policy.yaml").exists()


def test_sync_is_throttled(mac_home, tmp_path):
    client = _Client(policy=_assigned())
    instance = _worker(tmp_path, client)
    instance._maybe_sync_openshell_policy()
    instance._maybe_sync_openshell_policy()
    instance._maybe_sync_openshell_policy()
    assert len(client.gets) == 1


def test_no_temp_file_is_left_behind(mac_home, tmp_path):
    _worker(tmp_path, _Client(policy=_assigned()))._maybe_sync_openshell_policy()
    assert list(mac_home.glob("*.tmp")) == []


# --- command-audit spooling ------------------------------------------------


def _audit(command_id):
    return {"command_id": command_id, "phase": "run", "argv": ["echo"]}


def test_failed_post_is_spooled_not_dropped(mac_home, tmp_path):
    client = _Client(post_error=RuntimeError("hub down"))
    instance = _worker(tmp_path, client)

    instance._record_command_audit(_audit("cmd-1"))

    spool = instance._command_audit_spool_path()
    assert spool.is_file()
    assert json.loads(spool.read_text(encoding="utf-8").splitlines()[0])["command_id"] == "cmd-1"


def test_spool_drains_on_the_next_successful_post(mac_home, tmp_path):
    client = _Client(post_error=RuntimeError("hub down"))
    instance = _worker(tmp_path, client)
    instance._record_command_audit(_audit("cmd-1"))
    instance._record_command_audit(_audit("cmd-2"))

    client.post_error = None
    instance._record_command_audit(_audit("cmd-3"))

    delivered = [
        payload["command_id"] for path, payload in client.posts if path.endswith("/command-audit")
    ]
    # cmd-3 posts directly, then the spool replays oldest-first.
    assert delivered == ["cmd-3", "cmd-1", "cmd-2"]
    assert not instance._command_audit_spool_path().exists()


def test_partial_drain_keeps_the_undelivered_remainder_in_order(mac_home, tmp_path):
    client = _Client(post_error=RuntimeError("hub down"))
    instance = _worker(tmp_path, client)
    for index in range(4):
        instance._record_command_audit(_audit("cmd-%d" % index))

    # Recover for the direct post and exactly one replay, then fail again.
    calls = {"n": 0}
    real_post = _Client.post

    def flaky(self, path, payload):
        if path.endswith("/command-audit"):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("hub down again")
        return real_post(self, path, payload)

    client.post_error = None
    type(client).post = flaky
    try:
        instance._record_command_audit(_audit("cmd-live"))
    finally:
        type(client).post = real_post

    remaining = [
        json.loads(line)["command_id"]
        for line in instance._command_audit_spool_path().read_text(encoding="utf-8").splitlines()
    ]
    assert remaining == ["cmd-1", "cmd-2", "cmd-3"]


def test_spool_is_bounded_and_truncation_is_recorded(mac_home, tmp_path, monkeypatch):
    """Silent truncation would turn a bounded buffer into an invisible audit
    gap, so the drop is itself an observation."""
    monkeypatch.setattr(worker.MacWorker, "COMMAND_AUDIT_SPOOL_MAX_RECORDS", 5)
    client = _Client(post_error=RuntimeError("hub down"))
    instance = _worker(tmp_path, client)

    for index in range(9):
        instance._record_command_audit(_audit("cmd-%d" % index))

    kept = [
        json.loads(line)["command_id"]
        for line in instance._command_audit_spool_path().read_text(encoding="utf-8").splitlines()
    ]
    # Oldest dropped, newest retained.
    assert kept == ["cmd-4", "cmd-5", "cmd-6", "cmd-7", "cmd-8"]
    names = [payload.get("name") for path, payload in client.posts if path == "/observability/logs"]
    assert "worker.command_audit.spool_truncated" in names


def test_unparseable_spool_line_does_not_wedge_the_drain(mac_home, tmp_path):
    client = _Client(post_error=RuntimeError("hub down"))
    instance = _worker(tmp_path, client)
    instance._record_command_audit(_audit("cmd-1"))
    spool = instance._command_audit_spool_path()
    spool.write_text("{not json\n" + spool.read_text(encoding="utf-8"), encoding="utf-8")

    client.post_error = None
    instance._record_command_audit(_audit("cmd-2"))

    delivered = [
        payload["command_id"] for path, payload in client.posts if path.endswith("/command-audit")
    ]
    assert delivered == ["cmd-2", "cmd-1"]
    assert not spool.exists()
    # The dropped line is counted, not silently eaten.
    drained = next(
        payload
        for path, payload in client.posts
        if payload.get("name") == "worker.command_audit.spool_drained"
    )
    assert drained["detail"]["unparseable_dropped"] == 1
    assert drained["level"] == "warning"


def test_spool_is_per_worker_not_per_shared_home(mac_home, tmp_path):
    """Several agents can share one ~/.mac. A shared spool would let one worker
    drain another's records and re-post them under its OWN agent_id, silently
    misattributing audited commands."""
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = worker.MacWorker(
        _Client(post_error=RuntimeError("down")),
        "agent-a",
        a_dir,
        lambda _t, _p: worker.WorkerExecution(0, "ok"),
        self_update_repo=a_dir,
    )
    b = worker.MacWorker(
        _Client(),
        "agent-b",
        b_dir,
        lambda _t, _p: worker.WorkerExecution(0, "ok"),
        self_update_repo=b_dir,
    )

    a._record_command_audit(_audit("a-only"))
    assert a._command_audit_spool_path() != b._command_audit_spool_path()
    assert a._command_audit_spool_path().is_file()
    assert not b._command_audit_spool_path().exists()

    # B's successful post must not drain A's spool into B's agent_id.
    b._record_command_audit(_audit("b-only"))
    delivered = [
        payload["command_id"] for path, payload in b.client.posts if path.endswith("/command-audit")
    ]
    assert delivered == ["b-only"]
    assert a._command_audit_spool_path().is_file()


def test_spool_lives_outside_the_shared_mac_home(mac_home, tmp_path):
    instance = _worker(tmp_path, _Client(post_error=RuntimeError("down")))
    instance._record_command_audit(_audit("cmd-1"))
    assert list(mac_home.glob("*spool*")) == []


def test_successful_post_with_no_spool_is_a_noop(mac_home, tmp_path):
    client = _Client()
    instance = _worker(tmp_path, client)
    instance._record_command_audit(_audit("cmd-1"))
    assert not instance._command_audit_spool_path().exists()
    assert len([p for p, _ in client.posts if p.endswith("/command-audit")]) == 1
