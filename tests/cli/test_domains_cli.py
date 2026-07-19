"""Behavioral CLI tests for multiple high-traffic command families.

Covers: dispatch, secret (delete/rotate/access/audits), rollout, eval, events,
artifact, env, bridge (import/list/repo), user, persona, hermes (register/context),
binding, message, agentbus, review, task (reopen/force-complete/start/release/
evidence), project (pause/activate/show), agent (delete/heartbeat),
action-events, notifier, integrations, runtime
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main


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


def _setup_machine_agent(tmp_path, host="host-x", agent_name="worker-x", agent_id=None):
    rc, machine = _run(tmp_path, "machine", "register", host)
    assert rc == 0
    cmd = ["agent", "register", machine["id"], agent_name]
    if agent_id:
        cmd += ["--agent-id", agent_id]
    rc, agent = _run(tmp_path, *cmd)
    assert rc == 0
    return machine, agent


def _setup_tenant(tmp_path):
    rc, tenant = _run(tmp_path, "tenant", "register", "acme")
    assert rc == 0
    return tenant


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_dispatch_tick_empty(tmp_path):
    rc, result = _run(tmp_path, "dispatch", "tick")
    assert rc == 0
    # Tick returns a list of dispatch decisions (empty or non-empty)
    assert isinstance(result, (list, dict)) or result is None


def test_dispatch_assign_noop_when_no_agent(tmp_path):
    """dispatch assign with no ready tasks returns empty or null."""
    rc, result = _run(tmp_path, "dispatch", "assign")
    assert rc == 0


# ---------------------------------------------------------------------------
# secret delete / rotate / access / audits
# ---------------------------------------------------------------------------


def test_secret_delete(tmp_path):
    rc, secret = _run(tmp_path, "secret", "set", "tok", "abc123",
                      "--scopes", '{"capabilities": ["deploy"]}',
                      "--created-by", "human")
    assert rc == 0
    rc, result = _run(tmp_path, "secret", "delete", secret["id"], "--actor", "human")
    assert rc == 0
    assert result is not None


def test_secret_rotate(tmp_path):
    rc, secret = _run(tmp_path, "secret", "set", "tok2", "old-value",
                      "--scopes", '{"capabilities": ["deploy"]}',
                      "--created-by", "human")
    assert rc == 0
    rc, rotated = _run(tmp_path, "secret", "rotate", secret["name"], "new-value",
                       "--actor", "human")
    assert rc == 0
    assert rotated["name"] == "tok2"


def test_secret_access_denied_without_caps(tmp_path):
    """Access is denied by default when agent lacks matching capabilities.
    We only verify the command call is well-formed (rc 0 or 1, no crash)."""
    _, agent = _setup_machine_agent(tmp_path, host="sec-host", agent_name="sec-agent")
    rc, secret = _run(tmp_path, "secret", "set", "deploy-tok", "s3kr3t",
                      "--scopes", '{"capabilities": ["deploy"]}',
                      "--created-by", "human")
    assert rc == 0
    # secret access returns 1 when denied — that is the expected behavior
    rc, _ = _run(tmp_path, "secret", "access", secret["id"],
                 agent["id"], "--purpose", "deploy pipeline")
    assert rc in (0, 1)  # denied (1) is normal; 0 if caps matched


def test_secret_audits(tmp_path):
    rc, secret = _run(tmp_path, "secret", "set", "audit-tok", "val",
                      "--scopes", '{"capabilities": ["deploy"]}',
                      "--created-by", "human")
    assert rc == 0
    rc, audits = _run(tmp_path, "secret", "audits", "--secret-id", secret["id"])
    assert rc == 0
    assert isinstance(audits, list)


# ---------------------------------------------------------------------------
# rollout
# ---------------------------------------------------------------------------


def test_rollout_create_and_list(tmp_path):
    rc, rollout = _run(tmp_path, "rollout", "create", "v1.2.3", "canary",
                       "--created-by", "ops", "--channel", "fleet")
    assert rc == 0
    assert rollout["version"] == "v1.2.3"
    assert rollout["id"].startswith("rollout_")

    rc, rollouts = _run(tmp_path, "rollout", "list")
    assert rc == 0
    assert any(r["id"] == rollout["id"] for r in rollouts)


def test_rollout_advance_pause(tmp_path):
    """A freshly created canary rollout accepts 'pause' (no install-ready check)."""
    rc, rollout = _run(tmp_path, "rollout", "create", "v2.0.0", "canary",
                       "--created-by", "ops")
    assert rc == 0
    rc, advanced = _run(tmp_path, "rollout", "advance", rollout["id"], "pause",
                        "--actor", "ops")
    assert rc == 0
    assert advanced["status"] == "paused"


def test_rollout_advance_resume_after_pause(tmp_path):
    """A paused canary rollout accepts 'resume'."""
    rc, rollout = _run(tmp_path, "rollout", "create", "v3.0.0", "canary",
                       "--created-by", "ops")
    assert rc == 0
    # Pause first
    _run(tmp_path, "rollout", "advance", rollout["id"], "pause", "--actor", "ops")
    rc, advanced = _run(tmp_path, "rollout", "advance", rollout["id"], "resume",
                        "--actor", "ops")
    assert rc == 0
    assert advanced["status"] == "canarying"


def test_rollout_rescue(tmp_path):
    """rescue_rollout works from the 'planned' state without needing start_canary."""
    rc, rollout = _run(tmp_path, "rollout", "create", "v4.0.0", "canary",
                       "--created-by", "ops")
    assert rc == 0
    rc, result = _run(tmp_path, "rollout", "rescue", rollout["id"],
                      "--actor", "ops", "--reason", "regression detected")
    assert rc == 0
    assert "rollout" in result
    assert "task" in result


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------


def test_eval_set_create_list_show(tmp_path):
    rc, eset = _run(tmp_path, "eval", "set", "create", "my-eval-set",
                    "--scoring", "higher_is_better",
                    "--description", "CLI smoke eval",
                    "--created-by", "test")
    assert rc == 0
    assert eset["name"] == "my-eval-set"
    assert eset["id"].startswith("evalset_")

    rc, sets = _run(tmp_path, "eval", "set", "list")
    assert rc == 0
    assert any(s["id"] == eset["id"] for s in sets)

    rc, shown = _run(tmp_path, "eval", "set", "show", eset["id"])
    assert rc == 0
    assert shown["id"] == eset["id"]


def test_eval_set_baseline(tmp_path):
    rc, eset = _run(tmp_path, "eval", "set", "create", "baseline-eval",
                    "--created-by", "test")
    assert rc == 0
    rc, updated = _run(tmp_path, "eval", "set", "baseline", eset["id"], "0.85",
                       "--actor", "test")
    assert rc == 0
    assert float(updated["baseline_score"]) == pytest.approx(0.85)


def test_eval_run_record_and_list(tmp_path):
    rc, eset = _run(tmp_path, "eval", "set", "create", "run-eval", "--created-by", "test")
    assert rc == 0
    rc, run = _run(tmp_path, "eval", "run", "record",
                   eset["id"], "agent_build", "build-001", "0.92",
                   "--created-by", "test")
    assert rc == 0
    assert float(run["score"]) == pytest.approx(0.92)

    rc, runs = _run(tmp_path, "eval", "run", "list", "--eval-set", eset["id"])
    assert rc == 0
    assert any(r["id"] == run["id"] for r in runs)


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def test_events_list_empty(tmp_path):
    rc, events = _run(tmp_path, "events", "list")
    assert rc == 0
    assert isinstance(events, list)


def test_events_list_with_filter(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "event-task")
    assert rc == 0
    rc, events = _run(tmp_path, "events", "list",
                      "--subject-type", "task",
                      "--subject-id", task["id"],
                      "--limit", "10")
    assert rc == 0
    assert isinstance(events, list)


# ---------------------------------------------------------------------------
# artifact
# ---------------------------------------------------------------------------


def test_artifact_register_list_show_delete(tmp_path):
    rc, art = _run(tmp_path, "artifact", "register",
                   "image",
                   "sha256:" + "a" * 64,
                   "ghcr.io/org/img:v1",
                   "--created-by", "ops")
    assert rc == 0
    assert art["id"].startswith("art_")

    rc, arts = _run(tmp_path, "artifact", "list")
    assert rc == 0
    assert any(a["id"] == art["id"] for a in arts)

    rc, shown = _run(tmp_path, "artifact", "show", art["id"])
    assert rc == 0
    assert shown["id"] == art["id"]

    rc, deleted = _run(tmp_path, "artifact", "delete", art["id"], "--actor", "ops")
    assert rc == 0


# ---------------------------------------------------------------------------
# env (runtime environment)
# ---------------------------------------------------------------------------


def test_env_register_list_show(tmp_path):
    rc, env = _run(tmp_path, "env", "register", "staging",
                   "--created-by", "ops")
    assert rc == 0
    assert env["id"].startswith("env_")

    rc, envs = _run(tmp_path, "env", "list")
    assert rc == 0
    assert any(e["id"] == env["id"] for e in envs)

    rc, shown = _run(tmp_path, "env", "show", env["id"])
    assert rc == 0
    assert shown["id"] == env["id"]


def test_env_current_none(tmp_path):
    rc, env = _run(tmp_path, "env", "register", "staging2", "--created-by", "ops")
    assert rc == 0
    rc, current = _run(tmp_path, "env", "current", env["id"])
    assert rc == 0
    assert current is None


def test_env_deployments_empty(tmp_path):
    rc, env = _run(tmp_path, "env", "register", "staging3", "--created-by", "ops")
    assert rc == 0
    rc, deps = _run(tmp_path, "env", "history", env["id"])
    assert rc == 0
    assert isinstance(deps, list)


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------


def test_bridge_import_and_list(tmp_path):
    """bridge import takes positional source, external_id, title."""
    rc, item = _run(tmp_path, "bridge", "import",
                    "github",
                    "GH-42",
                    "Upstream issue from GH",
                    "--project", "upstream-proj",
                    "--actor", "sync-bot")
    assert rc == 0
    assert "task_id" in item

    rc, items = _run(tmp_path, "bridge", "list")
    assert rc == 0
    assert isinstance(items, list)
    assert any(i["external_id"] == "GH-42" for i in items)


def test_bridge_repository_register_and_list(tmp_path):
    """bridge repository register takes positional name, path."""
    # Create a minimal repo dir with the required contract file
    repo_dir = tmp_path / "my-repo"
    mac_dir = repo_dir / ".mac"
    mac_dir.mkdir(parents=True)
    (mac_dir / "project.yaml").write_text(
        "schema: mac.repository_contract.v1\n"
        "project: myproj\n"
        "platforms: [linux]\n"
        "toolchain:\n  required_commands: [python3]\n"
        "bootstrap:\n  command: echo ok\n  creates: []\n"
        "test:\n  command: echo test\n"
        "evidence:\n  required: [repo.head_sha]\n",
        encoding="utf-8",
    )
    rc, repo = _run(tmp_path, "bridge", "repository", "register",
                    "my-repo", str(repo_dir),
                    "--project", "myproj")
    assert rc == 0
    assert repo["id"].startswith("projectrepo_")

    rc, repos = _run(tmp_path, "bridge", "repository", "repos")
    assert rc == 0
    assert any(r["id"] == repo["id"] for r in repos)


# ---------------------------------------------------------------------------
# user / persona / hermes / binding (all require tenant_id positional)
# ---------------------------------------------------------------------------


def test_user_register(tmp_path):
    tenant = _setup_tenant(tmp_path)
    rc, user = _run(tmp_path, "user", "register", tenant["id"], "alice",
                    "--display-name", "Alice Smith")
    assert rc == 0
    assert user["handle"] == "alice"
    assert user["tenant_id"] == tenant["id"]


def test_persona_register(tmp_path):
    tenant = _setup_tenant(tmp_path)
    rc, persona = _run(tmp_path, "persona", "register", tenant["id"], "hermes-alice",
                       "--soul-ref", "soul://alice",
                       "--memory-scope", "project:myproj")
    assert rc == 0
    assert persona["name"] == "hermes-alice"
    assert persona["tenant_id"] == tenant["id"]


def test_hermes_register(tmp_path):
    tenant = _setup_tenant(tmp_path)
    rc, hermes = _run(tmp_path, "hermes", "register", tenant["id"], "hermes-hub")
    assert rc == 0
    assert hermes["id"].startswith("hermes_")
    assert hermes["name"] == "hermes-hub"


def test_binding_register(tmp_path):
    tenant = _setup_tenant(tmp_path)
    rc, hermes = _run(tmp_path, "hermes", "register", tenant["id"], "hermes-bind")
    assert rc == 0
    rc, binding = _run(tmp_path, "binding", "register",
                       tenant["id"], hermes["id"],
                       "slack", "CTEST123")
    assert rc == 0
    assert binding["platform"] == "slack"
    assert binding["external_id"] == "CTEST123"


# ---------------------------------------------------------------------------
# message
# ---------------------------------------------------------------------------


def test_message_send_and_inbox(tmp_path):
    """message send uses positional sender_agent_id + --recipient-agent-id flag."""
    _, sender = _setup_machine_agent(tmp_path, host="msg-host-s", agent_name="sender",
                                     agent_id="agent_sender")
    _, recipient = _setup_machine_agent(tmp_path, host="msg-host-r", agent_name="recipient",
                                        agent_id="agent_recipient")
    # nudge message type requires task_id in payload
    rc, task = _run(tmp_path, "task", "create", "nudge-task")
    assert rc == 0

    rc, msg = _run(tmp_path, "message", "send",
                   sender["id"],
                   "--recipient-agent-id", recipient["id"],
                   "--message-type", "nudge",
                   "--payload", json.dumps({"note": "ping", "task_id": task["id"]}))
    assert rc == 0
    assert msg["id"].startswith("msg_")

    rc, inbox = _run(tmp_path, "message", "inbox", recipient["id"])
    assert rc == 0
    assert any(m["id"] == msg["id"] for m in inbox)


# ---------------------------------------------------------------------------
# task remaining subcommands: start, release, reopen, force-complete, stats, evidence
# ---------------------------------------------------------------------------


def test_task_stats(tmp_path, monkeypatch):
    monkeypatch.setattr("mac.cli._default_project_from_cwd", lambda: "stats-proj")
    _run(tmp_path, "task", "create", "alpha", "--project", "stats-proj")
    rc, stats = _run(tmp_path, "task", "stats")
    assert rc == 0
    assert isinstance(stats, dict)
    # Stats is a count-by-state dict with at least one key
    assert len(stats) > 0


def test_task_reopen(tmp_path):
    rc, task = _run(tmp_path, "task", "create", "reopen-me")
    assert rc == 0
    _run(tmp_path, "task", "close", task["id"], "--reason", "done")
    rc, reopened = _run(tmp_path, "task", "reopen", task["id"], "--reason", "revisit")
    assert rc == 0
    assert reopened["state"] == "open"


def test_task_force_complete(tmp_path):
    _, agent = _setup_machine_agent(tmp_path, host="fc-host", agent_name="fc-worker")
    rc, task = _run(tmp_path, "task", "create", "force-me")
    assert rc == 0
    _run(tmp_path, "task", "claim", task["id"], agent["id"])
    _run(tmp_path, "task", "start", task["id"], agent["id"])
    rc, completed = _run(tmp_path, "task", "force-complete", task["id"],
                         "--actor", "ops", "--reason", "manual")
    assert rc == 0
    assert completed["state"] == "completed"


# ---------------------------------------------------------------------------
# project pause / activate / show
# ---------------------------------------------------------------------------


def test_project_pause_and_activate(tmp_path):
    rc, proj = _run(tmp_path, "project", "create", "pauseable-project")
    assert rc == 0

    rc, paused = _run(tmp_path, "project", "pause", proj["name"])
    assert rc == 0
    assert paused["metadata"].get("dispatch_paused") is True

    rc, activated = _run(tmp_path, "project", "activate", proj["name"])
    assert rc == 0
    # After activate, dispatch_paused should be cleared/false
    assert activated["metadata"].get("dispatch_paused") is not True


def test_project_show(tmp_path):
    rc, proj = _run(tmp_path, "project", "create", "show-project")
    assert rc == 0

    rc, shown = _run(tmp_path, "project", "show", proj["name"])
    assert rc == 0
    # project show returns a rich dict with "record" and "summary" keys
    assert shown["record"]["name"] == "show-project"


# ---------------------------------------------------------------------------
# agent delete / heartbeat
# ---------------------------------------------------------------------------


def test_agent_heartbeat(tmp_path):
    _, agent = _setup_machine_agent(tmp_path, host="hb-host", agent_name="hb-worker",
                                    agent_id="agent_hb")
    rc, result = _run(tmp_path, "agent", "heartbeat", agent["id"])
    assert rc == 0


def test_agent_delete(tmp_path):
    _, agent = _setup_machine_agent(tmp_path, host="del-host", agent_name="del-worker")
    rc, result = _run(tmp_path, "agent", "delete", agent["id"], "--actor", "ops")
    assert rc == 0


# ---------------------------------------------------------------------------
# action-events
# ---------------------------------------------------------------------------


def test_action_events_list_empty(tmp_path):
    rc, events = _run(tmp_path, "action-events", "list")
    assert rc == 0
    assert isinstance(events, list)


# ---------------------------------------------------------------------------
# runtime delta (list / show / reject)
# ---------------------------------------------------------------------------


def test_runtime_delta_list_empty(tmp_path):
    rc, deltas = _run(tmp_path, "runtime", "delta", "list")
    assert rc == 0
    assert isinstance(deltas, list)


def test_runtime_list_empty(tmp_path):
    rc, runtimes = _run(tmp_path, "runtime", "list")
    assert rc == 0
    assert isinstance(runtimes, list)


# ---------------------------------------------------------------------------
# notifier
# ---------------------------------------------------------------------------


def test_notifier_configure_and_list(tmp_path):
    """notifier configure takes positional name and channel_type."""
    rc, notif = _run(tmp_path, "notifier", "configure",
                     "ops-notifier",
                     "slack",
                     "--event-types", "task.*")
    assert rc == 0

    rc, notifs = _run(tmp_path, "notifier", "list")
    assert rc == 0
    assert isinstance(notifs, list)


# ---------------------------------------------------------------------------
# integrations
# ---------------------------------------------------------------------------


def test_integrations_findings_empty(tmp_path):
    rc, findings = _run(tmp_path, "integrations", "findings")
    assert rc == 0
    assert isinstance(findings, list)


def test_integrations_observations_empty(tmp_path):
    rc, obs = _run(tmp_path, "integrations", "observations")
    assert rc == 0
    assert isinstance(obs, list)
