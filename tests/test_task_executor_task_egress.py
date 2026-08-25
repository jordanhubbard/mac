"""Executor wiring for per-repo sandbox egress (ADR 0009 §2a).

The pure classification/rendering contract is pinned in
``tests/test_sandbox_egress.py``. These tests pin the *wiring*: that expansion
is off unless an operator turns it on, that the task classes which attest their
own policy digest are excluded, and that a task's derived contract actually
reaches the ``--policy`` the sandbox is created with — the loop that was open
before this landed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import task_executor as te

BASE_POLICY = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


@pytest.fixture
def base_policy(tmp_path, monkeypatch):
    path = tmp_path / "openshell-policy.yaml"
    path.write_text(BASE_POLICY, encoding="utf-8")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(path))
    return path


def _task(*, derived=None, declared=None, task_id="task_egress_1", metadata=None):
    meta = dict(metadata or {})
    if derived is not None:
        meta.setdefault("runtime", {})["environment_contract"] = {
            "schema": "mac.environment_contract.v1",
            "egress": {"hosts": list(derived)},
        }
    if declared is not None:
        meta["egress_contract"] = {"hosts": list(declared)}
    return {"id": task_id, "metadata": meta}


def test_expansion_is_off_by_default(base_policy, monkeypatch):
    """A repo must not be able to widen its own sandbox by adding a lockfile;
    enabling the feature is an operator act."""
    monkeypatch.delenv("MAC_OPENSHELL_TASK_EGRESS", raising=False)
    resolved = te._resolve_task_openshell_policy(_task(derived=["registry.npmjs.org"]))
    assert resolved == str(base_policy)


def test_enabled_expansion_widens_egress_for_a_trusted_registry(base_policy, monkeypatch):
    yaml = pytest.importorskip("yaml")
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    resolved = te._resolve_task_openshell_policy(_task(derived=["registry.npmjs.org"]))

    assert resolved != str(base_policy)
    parsed = yaml.safe_load(Path(resolved).read_text(encoding="utf-8"))
    added = parsed["network_policies"]["repo_declared_egress"]
    assert [e["host"] for e in added["endpoints"]] == ["registry.npmjs.org"]
    assert added["endpoints"][0]["access"] == "read-only"
    # The base policy is untouched on disk.
    assert base_policy.read_text(encoding="utf-8") == BASE_POLICY


def test_rendered_policy_is_owner_only(base_policy, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    resolved = te._resolve_task_openshell_policy(_task(derived=["pypi.org"]))
    assert Path(resolved).stat().st_mode & 0o777 == 0o600


def test_untrusted_derived_host_does_not_widen_the_policy(base_policy, monkeypatch):
    """The exfiltration guard, at the wiring level: an all-refused decision must
    leave the sandbox on the base policy rather than render an empty block."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    resolved = te._resolve_task_openshell_policy(_task(derived=["evil.example"]))
    assert resolved == str(base_policy)


def test_read_only_report_tasks_never_expand(base_policy, monkeypatch):
    """A read-only report attests the policy_sha256 it ran under; rendering a
    per-task policy would invalidate that attestation."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    task = _task(derived=["registry.npmjs.org"])
    monkeypatch.setattr(te, "metadata_declares_read_only_report_repository", lambda _meta: True)
    assert te._resolve_task_openshell_policy(task) == str(base_policy)


def test_declared_hosts_must_come_from_top_level_task_metadata(base_policy, monkeypatch):
    """`metadata.runtime` is worker-written, so a declared list smuggled under it
    carries only repo trust and must be classified as derived (and refused)."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    smuggled = {
        "id": "task_smuggle",
        "metadata": {
            "runtime": {
                "environment_contract": {"egress": {"hosts": ["opensky-network.org"]}},
                "egress_contract": {"hosts": ["opensky-network.org"]},
            }
        },
    }
    assert te._resolve_task_openshell_policy(smuggled) == str(base_policy)

    # The same host declared at the top level IS granted.
    allowed = te._resolve_task_openshell_policy(_task(declared=["opensky-network.org"]))
    assert allowed != str(base_policy)
    assert "opensky-network.org" in Path(allowed).read_text(encoding="utf-8")


def test_no_contract_leaves_the_base_policy_untouched(base_policy, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    assert te._resolve_task_openshell_policy({"id": "t", "metadata": {}}) == str(base_policy)
    assert te._resolve_task_openshell_policy(None) == str(base_policy)


def test_unreadable_base_policy_falls_back_rather_than_aborting(tmp_path, monkeypatch):
    """A task that cannot widen its egress should fail the way it did before the
    feature existed (a denied fetch), not lose the run."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    policy = tmp_path / "deny-all.yaml"
    # No network_policies mapping: expand_policy_text refuses this policy.
    policy.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(policy))
    assert te._resolve_task_openshell_policy(_task(derived=["registry.npmjs.org"])) == str(policy)


def test_create_argv_uses_the_expanded_policy(base_policy, monkeypatch):
    """End-to-end: the loop is closed only if the widened policy reaches the
    actual `openshell sandbox create` argv."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    monkeypatch.setattr(te, "_openshell_bin", lambda: "/usr/bin/openshell")
    monkeypatch.setattr(te, "_openshell_extra_create_argv", lambda **_: [])
    argv = te._build_sandbox_create_argv(
        "sb-1",
        Path("/tmp/workspace"),
        "task-7",
        [
            "/opt/mac-venv/bin/python",
            "-m",
            "mac.agent_command",
            "--prompt-file",
            "/sandbox/task-7/.mac-agent-prompt",
        ],
        task=_task(derived=["registry.npmjs.org"]),
    )
    policy_path = argv[argv.index("--policy") + 1]
    assert policy_path != str(base_policy)
    assert "registry.npmjs.org" in Path(policy_path).read_text(encoding="utf-8")


def test_create_argv_without_a_task_keeps_the_base_policy(base_policy, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    monkeypatch.setattr(te, "_openshell_bin", lambda: "/usr/bin/openshell")
    monkeypatch.setattr(te, "_openshell_extra_create_argv", lambda **_: [])
    argv = te._build_sandbox_create_argv(
        "sb-1",
        Path("/tmp/workspace"),
        "task-7",
        [
            "/opt/mac-venv/bin/python",
            "-m",
            "mac.agent_command",
            "--prompt-file",
            "/sandbox/task-7/.mac-agent-prompt",
        ],
    )
    assert argv[argv.index("--policy") + 1] == str(base_policy)


def test_rendered_policies_do_not_accumulate_for_a_long_lived_worker(base_policy, monkeypatch):
    """The worker is a run loop, so one leaked temp policy per task would grow
    unbounded for the life of the process."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    monkeypatch.setattr(te, "_EXPANDED_POLICY_FILES", [])
    monkeypatch.setattr(te, "_EXPANDED_POLICY_RETAIN", 2)

    produced = [
        Path(
            te._resolve_task_openshell_policy(
                _task(derived=["pypi.org"], task_id="task_%d" % index)
            )
        )
        for index in range(6)
    ]

    assert len(te._EXPANDED_POLICY_FILES) == 2
    assert [path.exists() for path in produced[-2:]] == [True, True]
    assert not any(path.exists() for path in produced[:-2])


def test_decision_is_audited(base_policy, monkeypatch):
    """Grants AND refusals must be observable; a silent widening is the thing
    this feature must never become."""
    monkeypatch.setenv("MAC_OPENSHELL_TASK_EGRESS", "1")
    events = []
    monkeypatch.setattr(te, "emit_telemetry", lambda name, **kw: events.append((name, kw)))
    te._resolve_task_openshell_policy(_task(derived=["registry.npmjs.org", "evil.example"]))
    assert [name for name, _ in events] == ["sandbox_egress_decision"]
    payload = events[0][1]
    assert payload["granted_count"] == 1
    assert payload["rejected_count"] == 1
    assert payload["task_id"] == "task_egress_1"
    assert payload["rejected"][0]["host"] == "evil.example"
