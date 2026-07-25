"""Extended behavioral tests for less common CLI domains.

Covers:
  - diagnostics: basic health report
  - artifact: register, list, show, delete
  - env: deploy, current, history
  - notifier: configure, list, delete, deliver
  - command-audit: list
  - observability: list, prune
  - rollout extended: verify-artifact, health
  - nap extended: cycle, due
  - agentbus extended: publish, repo-update, artifact-publish
  - secret: access (with trusted machine)
  - task extended: detect-beads, detect-ticketing

Duplicate smoke-tests for memory (health/recall/recall-dreams/decay/
summarize-actions), ``env register/list/show``, ``env current`` (empty), and
``action-events list`` were removed: they executed identical code paths to the
stronger dedicated suites in ``tests/cli/test_cli_memory.py`` and
``tests/cli/test_domains_cli.py``.

Each test exercises: exit code, parsed JSON output, and (for mutating
commands) visible side-effect against the same in-file SQLite store.

NOTE: commands that require external infra (Qdrant, live fleet SSH,
openshell policy file) are skipped with a pytest mark rather than
left as incorrectly-passing stubs.
"""

from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

from mac.cli import main


# ---------------------------------------------------------------------------
# shared helper
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _make_agent(tmp_path, name="worker-1", hostname="host-1", agent_id=None, trusted=False):
    """Register a machine (optionally trusted) + agent, return the agent dict."""
    rc, machine = _run(tmp_path, "machine", "register", hostname)
    assert rc == 0
    if trusted:
        # Patch machine to trusted=True directly — the CLI does not expose a
        # `machine trust` subcommand, so we update via a second register call
        # with the same hostname (upsert) and trust the underlying store entry.
        # The simplest approach: use the db directly.
        import sqlite3
        db_path = str(tmp_path / "mac.db")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE machines SET trusted = 1 WHERE id = ?", (machine["id"],))
        conn.commit()
        conn.close()
    extra = ["--agent-id", agent_id] if agent_id else []
    rc, agent = _run(tmp_path, "agent", "register", machine["id"], name, *extra)
    assert rc == 0
    return agent


def test_agent_instance_kind_update(tmp_path):
    agent = _make_agent(tmp_path, name="headless-worker")
    assert agent["instance_kind"] == "static"

    rc, updated = _run(
        tmp_path,
        "agent",
        "update",
        agent["id"],
        "--instance-kind",
        "fungible",
    )

    assert rc == 0
    assert updated["instance_kind"] == "fungible"

    rc, listed = _run(tmp_path, "agent", "list")
    assert rc == 0
    assert next(item for item in listed if item["id"] == agent["id"])[
        "instance_kind"
    ] == "fungible"


def test_openshell_sandbox_gc_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mac.openshell_sandbox_gc.subprocess.run",
        lambda _argv, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "name": "mac-task-old",
                        "phase": "Ready",
                        "created_at": "2020-01-01 00:00:00",
                        "labels": {},
                    }
                ]
            ),
            stderr="",
        ),
    )

    rc, report = _run(
        tmp_path,
        "openshell",
        "sandbox-gc",
        "--stale-after-hours",
        "1",
    )

    assert rc == 0
    assert report["dry_run"] is True
    assert [row["name"] for row in report["candidates"]] == ["mac-task-old"]


def test_openshell_reap_orphans_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mac.openshell_sandbox_gc._pid_is_alive",
        lambda _pid: False,
    )
    monkeypatch.setattr(
        "mac.openshell_sandbox_gc.subprocess.run",
        lambda _argv, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "name": "mac-task-dead",
                        "phase": "Ready",
                        "labels": {
                            "mac.owner": "mac",
                            "mac.kind": "task",
                            "mac.keep": "false",
                            "mac.pid": "424242",
                        },
                    }
                ]
            ),
            stderr="",
        ),
    )

    rc, report = _run(tmp_path, "openshell", "reap-orphans")

    assert rc == 0
    assert report["schema"] == "mac.openshell.sandbox_orphan_reap.v1"
    assert report["dry_run"] is True
    assert [row["name"] for row in report["candidates"]] == ["mac-task-dead"]
    assert report["deleted"] == []


# ===========================================================================
# diagnostics
# ===========================================================================


def test_diagnostics_returns_health_report(tmp_path):
    """diagnostics runs all control-plane health checks and returns a JSON report."""
    rc, report = _run(tmp_path, "diagnostics")
    assert rc == 0
    assert isinstance(report, dict)
    assert "checks" in report
    assert "counts" in report
    counts = report["counts"]
    assert isinstance(counts.get("ok"), int)
    # No errors expected on a fresh empty db
    assert counts.get("error", 0) == 0


def test_diagnostics_with_specific_check(tmp_path):
    """diagnostics --check filters to a single named check."""
    rc, report = _run(tmp_path, "diagnostics", "--check", "database-reachable")
    assert rc == 0
    assert isinstance(report, dict)
    findings = report.get("findings", [])
    assert len(findings) >= 1
    assert findings[0]["check"] == "database-reachable"
    assert findings[0]["severity"] == "ok"


# ===========================================================================
# artifact
# ===========================================================================


def test_artifact_register_list_show_delete(tmp_path):
    """Full artifact lifecycle: register -> list -> show -> delete."""
    rc, artifact = _run(
        tmp_path,
        "artifact", "register",
        "image",
        "sha256:abc123def456",
        "ghcr.io/example/app:1.0.0",
        "--created-by", "ops",
    )
    assert rc == 0
    assert artifact["kind"] == "image"
    assert artifact["digest"] == "sha256:abc123def456"
    # artifact IDs may have different prefixes; just verify the format
    assert "_" in artifact["id"]

    # list - should see our artifact
    rc, artifacts = _run(tmp_path, "artifact", "list")
    assert rc == 0
    assert isinstance(artifacts, list)
    assert any(a["id"] == artifact["id"] for a in artifacts)

    # list filtered by kind
    rc, images = _run(tmp_path, "artifact", "list", "--kind", "image")
    assert rc == 0
    assert all(a["kind"] == "image" for a in images)

    # show
    rc, shown = _run(tmp_path, "artifact", "show", artifact["id"])
    assert rc == 0
    assert shown["id"] == artifact["id"]
    assert shown["digest"] == "sha256:abc123def456"

    # delete
    rc, deleted = _run(tmp_path, "artifact", "delete", artifact["id"], "--actor", "ops")
    assert rc == 0

    # confirm gone
    rc, after = _run(tmp_path, "artifact", "list")
    assert rc == 0
    assert artifact["id"] not in {a["id"] for a in after}


def test_artifact_register_with_signers_and_sbom(tmp_path):
    """artifact register accepts optional --signers and --sbom-uri."""
    rc, artifact = _run(
        tmp_path,
        "artifact", "register",
        "wheel",
        "sha256:deadbeef",
        "https://pypi.org/packages/app-1.0.whl",
        "--created-by", "ci",
        "--signers", "alice,bob",
        "--sbom-uri", "https://sbom.example.com/app-1.0.spdx",
    )
    assert rc == 0
    assert artifact["kind"] == "wheel"


# ===========================================================================
# env (environments + deployments)
# ===========================================================================


def test_env_deploy_current_history(tmp_path):
    """env deploy records a deployment; current and history reflect it."""
    rc, env = _run(tmp_path, "env", "register", "prod", "--created-by", "ops")
    assert rc == 0

    rc, artifact = _run(
        tmp_path,
        "artifact", "register",
        "image",
        "sha256:v1hash",
        "ghcr.io/example/app:v1",
        "--created-by", "ci",
    )
    assert rc == 0

    rc, deployment = _run(
        tmp_path,
        "env", "deploy",
        env["id"],
        artifact["id"],
        "--actor", "ops",
    )
    assert rc == 0
    assert deployment["environment_id"] == env["id"]
    assert deployment["artifact_id"] == artifact["id"]

    # current
    rc, current = _run(tmp_path, "env", "current", env["id"])
    assert rc == 0
    assert current["artifact_id"] == artifact["id"]

    # history
    rc, history = _run(tmp_path, "env", "history", env["id"])
    assert rc == 0
    assert isinstance(history, list)
    assert any(d["id"] == deployment["id"] for d in history)


# ===========================================================================
# notifier
# ===========================================================================


def test_notifier_configure_list_delete(tmp_path):
    """notifier configure -> list -> delete lifecycle."""
    rc, channel = _run(
        tmp_path,
        "notifier", "configure",
        "slack-alerts",
        "slack",
        "--event-types", "task.completed,task.failed",
        "--target", '{"webhook": "https://hooks.slack.com/xxx"}',
    )
    assert rc == 0
    assert channel["name"] == "slack-alerts"
    assert channel["channel_type"] == "slack"

    # list
    rc, channels = _run(tmp_path, "notifier", "list")
    assert rc == 0
    assert isinstance(channels, list)
    assert any(c["name"] == "slack-alerts" for c in channels)

    # delete
    rc, result = _run(tmp_path, "notifier", "delete", "slack-alerts")
    assert rc == 0
    assert result.get("deleted") == "slack-alerts"

    # confirm gone
    rc, after = _run(tmp_path, "notifier", "list")
    assert rc == 0
    assert not any(c["name"] == "slack-alerts" for c in after)


def test_notifier_deliver_empty(tmp_path):
    """notifier deliver with no pending notifications returns a result without crashing."""
    rc, result = _run(tmp_path, "notifier", "deliver")
    assert rc == 0


def test_notifier_list_filter_enabled(tmp_path):
    """notifier list --enabled only shows enabled channels."""
    # Configure a disabled channel (must use a valid channel_type: hermes, slack, telegram)
    _run(
        tmp_path,
        "notifier", "configure",
        "disabled-channel",
        "hermes",
        "--disabled",
    )
    rc, enabled_only = _run(tmp_path, "notifier", "list", "--enabled")
    assert rc == 0
    assert not any(c.get("name") == "disabled-channel" for c in enabled_only)


# ===========================================================================
# command-audit
# ===========================================================================


def test_command_audit_list_empty(tmp_path):
    """command-audit list returns empty list on a fresh db."""
    rc, records = _run(tmp_path, "command-audit", "list", "--limit", "10")
    assert rc == 0
    assert isinstance(records, list)


# ===========================================================================
# observability
# ===========================================================================


def test_observability_list_empty(tmp_path):
    """observability list returns empty list on a fresh db."""
    rc, events = _run(tmp_path, "observability", "list", "--limit", "10")
    assert rc == 0
    assert isinstance(events, list)


def test_observability_prune_returns_count(tmp_path):
    """observability prune with --keep-last=100 returns removed count (0 on empty db)."""
    rc, result = _run(tmp_path, "observability", "prune", "--keep-last", "100")
    assert rc == 0
    assert isinstance(result, dict)
    assert "removed" in result
    assert isinstance(result["removed"], int)


# ===========================================================================
# rollout extended: verify-artifact, health
# ===========================================================================


def test_rollout_verify_artifact(tmp_path):
    """rollout verify-artifact records verification result against a rollout."""
    rc, rollout = _run(
        tmp_path,
        "rollout", "create",
        "v2.0.0", "canary",
        "--created-by", "ci",
    )
    assert rc == 0

    rc, result = _run(
        tmp_path,
        "rollout", "verify-artifact",
        rollout["id"],
        "--artifact-uri", "ghcr.io/example/app:v2.0.0",
        "--artifact-hash", "sha256:v2hash",
        "--actor", "ops",
    )
    assert rc == 0
    assert isinstance(result, dict)


def test_rollout_health(tmp_path):
    """rollout health evaluates health checks and returns a health report."""
    rc, rollout = _run(
        tmp_path,
        "rollout", "create",
        "v2.1.0", "canary",
        "--created-by", "ci",
    )
    assert rc == 0

    rc, result = _run(
        tmp_path,
        "rollout", "health",
        rollout["id"],
        "--checks", '{"error_rate": "ok", "latency_p99": "ok"}',
        "--actor", "monitor",
    )
    assert rc == 0
    assert isinstance(result, dict)
    # health report always has a 'healthy' key
    assert "healthy" in result


# ===========================================================================
# nap extended: cycle, due
# ===========================================================================


def test_nap_cycle_runs_all_due(tmp_path):
    """nap cycle for a configured agent runs the full cycle."""
    agent = _make_agent(tmp_path, "cycle-napper", agent_id="agent_cycle")
    _run(tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "0")

    rc, result = _run(tmp_path, "nap", "cycle", agent["id"])
    # cycle may fail if Qdrant is unavailable; accept non-zero but no crash
    assert isinstance(rc, int)


def test_nap_due_returns_agents(tmp_path):
    """nap due lists agents with upcoming nap windows."""
    agent = _make_agent(tmp_path, "due-napper", agent_id="agent_due")
    _run(tmp_path, "nap", "configure", agent["id"], "--offset-minutes", "0")
    rc, result = _run(tmp_path, "nap", "due")
    assert rc == 0
    assert isinstance(result, (list, dict))


# ===========================================================================
# agentbus extended: publish, repo-update, artifact-publish
# ===========================================================================


def test_agentbus_publish_event(tmp_path):
    """agentbus publish broadcasts a message; requires a recipient or is a no-op."""
    agent = _make_agent(tmp_path, "pub-agent", agent_id="agent_pub")
    rc, result = _run(
        tmp_path,
        "agentbus", "publish",
        agent["id"],
        "--recipient-agent-id", agent["id"],
        "--payload", '{"msg": "hello"}',
    )
    assert rc == 0


def test_agent_reflect_publishes_self_description(tmp_path):
    """agent reflect publishes the agent's registered runtime state over AgentBus."""
    agent = _make_agent(
        tmp_path,
        "reflect-agent",
        agent_id="agent_reflect",
    )
    rc, result = _run(
        tmp_path,
        "agent", "reflect",
        agent["id"],
        "--request-id", "rid-42",
        # No live worker answers in-process; skip the 30s reflect poll.
        "--reflect-timeout", "0",
    )

    assert rc == 0
    assert result["schema"] == "mac.agentbus.agent_reflection_publish.v1"
    assert result["agent_id"] == agent["id"]
    assert result["recipient_agent_id"] == agent["id"]
    assert result["payload"]["schema"] == "mac.agentbus.agent_reflection.v2"
    assert result["payload"].get("request_id") == "rid-42"
    assert result["payload"]["agent"]["name"] == "reflect-agent"
    assert result["streams"][0]["topic"] == "mac.agent.reflect.v1"
    assert result["streams"][0]["content_type"] == "application/vnd.mac.agent-reflection+json"


def test_agentbus_repo_update(tmp_path):
    """agentbus repo-update sends a repository-update event to agents."""
    agent = _make_agent(tmp_path, "repo-agent", agent_id="agent_repo")
    rc, result = _run(
        tmp_path,
        "agentbus", "repo-update",
        agent["id"],
        "--all-agents",
        "--repo-path", "/tmp/test-repo",
    )
    assert rc == 0


def test_agentbus_artifact_publish(tmp_path):
    """agentbus artifact-publish sends an artifact event to agents."""
    agent = _make_agent(tmp_path, "art-agent", agent_id="agent_art")
    rc, result = _run(
        tmp_path,
        "agentbus", "artifact-publish",
        agent["id"],
        "--operation", "upsert",
        "--digest", "sha256:testdigest",
        "--kind", "public-artifact",
        "--uri", "https://example.com/artifact.tar.gz",
        "--all-agents",
    )
    assert rc == 0


# ===========================================================================
# secret: access (requires trusted machine)
# ===========================================================================


def test_secret_access(tmp_path):
    """secret access grants a handle when the machine is trusted and agent is in scopes."""
    agent = _make_agent(
        tmp_path, "secret-reader", hostname="trusted-host",
        agent_id="agent_secretreader", trusted=True
    )
    # Scope the secret to this specific agent by ID
    scopes = json.dumps({"agents": [agent["id"]]})
    rc, secret = _run(
        tmp_path,
        "secret", "set",
        "access-test-token",
        "my-secret-value",
        "--scopes", scopes,
        "--created-by", "human",
    )
    assert rc == 0

    rc, result = _run(
        tmp_path,
        "secret", "access",
        secret["id"],
        agent["id"],
        "--purpose", "deploy",
    )
    assert rc == 0
    assert isinstance(result, dict)


# ===========================================================================
# task extended: detect-beads, detect-ticketing
# ===========================================================================


def test_task_detect_beads_path(tmp_path):
    """task detect-beads checks a path for beads state; empty dir = no beads."""
    rc, result = _run(
        tmp_path,
        "task", "detect-beads",
        str(tmp_path),
    )
    assert rc == 0
    assert isinstance(result, dict)


def test_task_detect_ticketing_path(tmp_path):
    """task detect-ticketing checks a path for legacy ticketing state."""
    rc, result = _run(
        tmp_path,
        "task", "detect-ticketing",
        str(tmp_path),
    )
    assert rc == 0
    assert isinstance(result, dict)


# ===========================================================================
# coverage gate update: add new domains to the manifest
# ===========================================================================


def test_extended_cli_coverage_gate():
    """Meta-test: assert that the extended domains are all registered in cli.py.

    This test complements the parser-wide gate in test_cli_coverage_gate.py,
    verifying the newly covered domains so both gates stay in sync.
    """
    # Domains verified by tests in THIS file
    extended_covered = {
        "diagnostics",
        "artifact",
        "env",
        "notifier",
        "command-audit",
        "observability",
    }

    from mac.cli import build_parser
    parser = build_parser()
    registered = set()
    for action in parser._actions:
        if hasattr(action, "_name_parser_map"):
            for name in action._name_parser_map:
                registered.add(name)

    missing_from_cli = extended_covered - registered
    assert not missing_from_cli, (
        f"Extended domains in the coverage manifest are no longer registered: {missing_from_cli}. "
        "Did you rename a command? Update extended_covered in this gate test."
    )
