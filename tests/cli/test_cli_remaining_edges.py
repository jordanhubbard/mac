"""Focused behavioral coverage for remaining CLI rendering and command paths."""

from __future__ import annotations

import io
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import cli
from mac.models import MACError


def test_read_text_argument_sources_and_failures(monkeypatch, tmp_path) -> None:
    path = tmp_path / "value.txt"
    path.write_text("from file")
    assert cli._read_text_arg("inline", str(path), label="value") == "inline"
    assert cli._read_text_arg(None, str(path), label="value") == "from file"
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert cli._read_text_arg(None, "-", label="value") == "from stdin"
    assert cli._read_text_arg(None, None, label="value", default="default") == "default"
    with pytest.raises(SystemExit, match="failed to read"):
        cli._read_text_arg(None, str(tmp_path / "missing"), label="value")


def test_one_line_and_text_rendering_all_record_shapes() -> None:
    assert cli._one_liner(3) == "3"
    assert "open" in cli._one_liner({"id": "task_1", "state": "open", "title": "Task"})
    assert "▶ task_1" in cli._one_liner(
        {"name": "agent", "status": "busy", "current_task_id": "task_1"}
    )
    assert cli._one_liner({}) == "{}"
    assert cli._render_text(None) == "(none)"
    assert cli._render_text([]) == "(none)"
    assert cli._render_text({}) == "(empty)"
    detail = cli._render_text(
        {
            "task": {
                "id": "task_1",
                "state": "open",
                "title": "Task",
                "assignee": "agent",
                "attempt_count": 1,
                "max_attempts": 3,
                "dependencies": ["task_0"],
            },
            "evidence": [{"id": "e"}],
            "reviews": [],
        }
    )
    assert "assignee: agent" in detail
    assert "dependencies: 1" in detail
    assert "evidence: 1" in detail
    generic = cli._render_text({"nested": {"a": 1}, "items": [1], "value": "x"})
    assert "nested: {1 keys}" in generic
    assert "items: [1]" in generic


def test_client_profile_cli_commands(monkeypatch) -> None:
    from mac import client_profiles

    outputs = []
    monkeypatch.setattr(cli, "_print", outputs.append)
    monkeypatch.setattr(client_profiles, "list_profiles", lambda: [{"profile": "one"}])
    monkeypatch.setattr(client_profiles, "activate_profile", lambda name: {"profile": name})
    monkeypatch.setattr(client_profiles, "remove_profile", lambda name: {"removed": name})
    monkeypatch.setattr(
        client_profiles,
        "migrate_legacy_profile",
        lambda **kwargs: kwargs,
    )
    cli.cmd_client_profile_list(Namespace())
    cli.cmd_client_profile_activate(Namespace(profile_name="one"))
    cli.cmd_client_profile_remove(Namespace(profile_name="one"))
    cli.cmd_client_profile_migrate_legacy(
        Namespace(
            fleet_name="fleet",
            profile_name="profile",
            fleets_config="fleets.yaml",
            env_file=".env",
            allow_legacy_admin_token=True,
            no_activate=True,
        )
    )
    assert outputs[0] == [{"profile": "one"}]
    assert outputs[-1]["activate"] is False


def test_openshell_policy_update_and_render_to_file(monkeypatch, tmp_path) -> None:
    calls = []

    class Plane:
        def update_openshell_policy(self, *args, **kwargs):
            calls.append(("update", args, kwargs))
            return {"updated": True}

        def render_openshell_policy(self, *args, **kwargs):
            calls.append(("render", args, kwargs))
            return {"policy_text": "policy: true", "name": "policy"}

    monkeypatch.setattr(cli, "_plane", lambda _args: Plane())
    outputs = []
    monkeypatch.setattr(cli, "_print", outputs.append)
    cli.cmd_openshell_policy_update(
        Namespace(
            policy="p",
            policy_text="text",
            policy_file=None,
            metadata='{"x":1}',
            metadata_file=None,
            name="name",
            description="description",
            updated_by="actor",
        )
    )
    destination = tmp_path / "nested" / "policy.yaml"
    cli.cmd_openshell_policy_render(
        Namespace(
            policy="p",
            shared_services='{"svc":{}}',
            shared_services_file=None,
            agent_user="agent",
            hub_host="hub",
            hub_port=8789,
            model_gateway_host="model",
            into=str(destination),
        )
    )
    assert destination.read_text() == "policy: true"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert outputs[-1]["policy_text"] == ""


def test_fleet_memory_export_and_prune(monkeypatch, tmp_path, capsys) -> None:
    from mac import memory_vetting

    monkeypatch.setattr(
        memory_vetting,
        "QdrantClient",
        lambda _url: SimpleNamespace(scroll=lambda *_a, **_k: [], delete=lambda *_a, **_k: {}),
    )
    monkeypatch.setattr(
        memory_vetting,
        "export_memory_records",
        lambda _scroll, collections, agent_id=None: [
            {"id": 1, "text": "match", "agent": agent_id, "collections": list(collections)},
            {"id": 2, "text": "other"},
        ],
    )
    monkeypatch.setattr(
        memory_vetting,
        "search_records",
        lambda records, term: [record for record in records if term in record.get("text", "")],
    )
    monkeypatch.setattr(
        memory_vetting,
        "prune_points",
        lambda _delete, collection, ids: {"collection": collection, "ids": ids},
    )
    outputs = []
    monkeypatch.setattr(cli, "_print", outputs.append)
    destination = tmp_path / "records.jsonl"
    cli.cmd_fleet_memory_export(
        Namespace(
            qdrant_url="http://q",
            collections="one,two",
            agent="agent",
            search="match",
            into=str(destination),
        )
    )
    assert json.loads(destination.read_text().strip())["id"] == 1
    source = tmp_path / "prune.jsonl"
    source.write_text('{"id":2}\n{"no_id":true}\n')
    cli.cmd_fleet_memory_prune(
        Namespace(id=[1], from_jsonl=str(source), qdrant_url="http://q", collection="one")
    )
    assert outputs[-1]["ids"] == [1, 2]

    cli.cmd_fleet_memory_export(
        Namespace(
            qdrant_url="http://q",
            collections=None,
            agent=None,
            search=None,
            into=None,
        )
    )
    assert '"id": 1' in capsys.readouterr().out


def test_refresh_context_updates_fleet_and_mood(monkeypatch, tmp_path) -> None:
    from mac import hermes_runtime

    plane = SimpleNamespace(fleet_snapshot=lambda **_kwargs: {"members": [{"id": "a"}]})
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_hub_get_mood", lambda _agent: {"mode": "focused"})
    seen = []
    monkeypatch.setattr(hermes_runtime, "render_fleet_section", lambda value: "fleet")
    monkeypatch.setattr(hermes_runtime, "render_mood_section", lambda value: "mood")
    monkeypatch.setattr(
        hermes_runtime, "refresh_fleet_section", lambda path, text: seen.append((path, text))
    )
    monkeypatch.setattr(
        hermes_runtime, "refresh_mood_section", lambda path, text: seen.append((path, text))
    )
    outputs = []
    monkeypatch.setattr(cli, "_print", outputs.append)
    target = tmp_path / "context.md"
    cli.cmd_fleet_refresh_context(Namespace(agent="a", markdown=str(target)))
    assert seen == [(target, "fleet"), (target, "mood")]
    assert outputs[-1]["mood"] == "focused"


def test_journal_commands_delegate_and_render(monkeypatch, tmp_path) -> None:
    from mac import journal

    monkeypatch.setattr(
        journal,
        "snapshot",
        lambda **_kwargs: {
            "date": "2026-01-01",
            "agent_id": "agent",
            "captured": ["soul"],
            "files": ["a", "b"],
            "hook": "ok",
        },
    )
    monkeypatch.setattr(journal, "list_journals", lambda root: [{"root": str(root)}])
    monkeypatch.setattr(journal, "restore", lambda date, **kwargs: {"date": date, **kwargs})
    outputs = []
    monkeypatch.setattr(cli, "_print", outputs.append)
    args = Namespace(
        dir=str(tmp_path),
        home=str(tmp_path / "home"),
        date="2026-01-01",
        agent="agent",
        no_hook=True,
    )
    cli.cmd_journal_snapshot(args)
    cli.cmd_journal_list(Namespace(dir=str(tmp_path)))
    cli.cmd_journal_restore(
        Namespace(dir=str(tmp_path), home=str(tmp_path / "home"), date="2026-01-01", dry_run=True)
    )
    assert outputs[0]["files"] == 2
    assert outputs[-1]["dry_run"] is True


def test_pull_request_open_success_finding_failure_and_required_url(monkeypatch) -> None:
    from mac import gitops

    result = SimpleNamespace(host="github", number=7, url="https://pr/7", state="open")
    monkeypatch.setattr(gitops, "open_pull_request", lambda *_a, **_k: result)
    outputs = []
    monkeypatch.setattr(cli, "_print", outputs.append)
    plane = SimpleNamespace(
        record_integration_finding=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("ledger down")
        )
    )
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    with pytest.raises(SystemExit, match="repo-url is required"):
        cli.cmd_pull_request_open(
            Namespace(repo_url="", head="h", base=None, title=None, body=None, task_id=None)
        )
    cli.cmd_pull_request_open(
        Namespace(
            repo_url="https://repo", head="h", base="main", title="t", body="b", task_id="task"
        )
    )
    assert outputs[-1]["finding_error"] == "ledger down"


def test_secret_value_sources(monkeypatch, tmp_path) -> None:
    with pytest.raises(MACError, match="exactly one"):
        cli._resolve_secret_value(Namespace(value="", from_stdin=False, from_file=None))
    monkeypatch.setattr("sys.stdin", io.StringIO("stdin\n"))
    assert (
        cli._resolve_secret_value(Namespace(value="", from_stdin=True, from_file=None)) == "stdin"
    )
    path = tmp_path / "secret"
    path.write_text("file\n")
    assert (
        cli._resolve_secret_value(Namespace(value="", from_stdin=False, from_file=str(path)))
        == "file"
    )
    assert (
        cli._resolve_secret_value(Namespace(value="value", from_stdin=False, from_file=None))
        == "value"
    )


def test_action_stream_non_follow_prints_events(monkeypatch, capsys) -> None:
    event = SimpleNamespace(to_dict=lambda: {"timestamp": "2026-01-01", "id": "event"})
    plane = SimpleNamespace(list_action_events=lambda **_kwargs: [event])
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    cli.cmd_action_events_stream(
        Namespace(
            since=None,
            follow=False,
            timeout=0,
            interval=0,
            agent_id=None,
            task_id=None,
            session_id=None,
            sandbox_id=None,
            policy_id=None,
            action_type=None,
            outcome=None,
            limit=10,
        )
    )
    assert '"id": "event"' in capsys.readouterr().out


def test_workflow_decisions_and_start(monkeypatch) -> None:
    calls = []

    class Plane:
        def workflow_run_decisions(self, target):
            calls.append(("run", target))
            return []

        def workflow_decisions(self, target, **kwargs):
            calls.append(("workflow", target, kwargs))
            return []

        def start_workflow(self, target, **kwargs):
            calls.append(("start", target, kwargs))
            return {"id": "run"}

    monkeypatch.setattr(cli, "_plane", lambda _args: Plane())
    monkeypatch.setattr(cli, "_print", lambda _value: None)
    cli.cmd_workflow_decisions(Namespace(id_or_slug="run_1", tenant_id=None))
    cli.cmd_workflow_decisions(Namespace(id_or_slug="flow", tenant_id="tenant"))
    with pytest.raises(MACError, match="expects"):
        cli.cmd_workflow_start(
            Namespace(
                workflow_id_or_slug="flow",
                pre_decision=["bad"],
                input="{}",
                started_by="actor",
                tenant_id=None,
            )
        )
    cli.cmd_workflow_start(
        Namespace(
            workflow_id_or_slug="flow",
            pre_decision=["gate=APPROVED"],
            input='{"goal":"x"}',
            started_by="actor",
            tenant_id="tenant",
        )
    )
    assert calls[-1][2]["pre_decisions"] == {"gate": "approved"}


def test_hub_get_mood_uses_fleet_scoped_token(monkeypatch) -> None:
    """_hub_get_mood must authenticate with the fleet-scoped worker token
    (derived from MAC_FLEET) ahead of the legacy flat form (mac-g55y)."""
    import urllib.request as urllib_request

    for name in (
        "MAC_HUB_URL",
        "MAC_URL",
        "MAC_FLEET",
        "MAC_WORKER_TOKEN",
        "MAC_WORKER_TOKEN__ROCKY",
        "MAC_API_TOKEN",
        "MAC_API_TOKEN__ROCKY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("MAC_HUB_URL", "http://hub")
    monkeypatch.setenv("MAC_FLEET", "rocky")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "worker-flat")
    monkeypatch.setenv("MAC_WORKER_TOKEN__ROCKY", "worker-rocky")

    captured: dict[str, str] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"mode": "focused"}'

    def _fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(urllib_request, "urlopen", _fake_urlopen)

    result = cli._hub_get_mood("agent_test")
    assert result == {"mode": "focused"}
    assert captured["auth"] == "Bearer worker-rocky"
    assert captured["url"] == "http://hub/agents/agent_test/mood"


def test_hub_get_mood_falls_back_to_legacy_flat_token(monkeypatch) -> None:
    """Without a scoped form, _hub_get_mood uses the legacy flat token, still
    preferring MAC_WORKER_TOKEN over MAC_API_TOKEN (mac-g55y)."""
    import urllib.request as urllib_request

    for name in (
        "MAC_HUB_URL",
        "MAC_URL",
        "MAC_FLEET",
        "MAC_WORKER_TOKEN",
        "MAC_WORKER_TOKEN__ROCKY",
        "MAC_API_TOKEN",
        "MAC_API_TOKEN__ROCKY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("MAC_HUB_URL", "http://hub")
    monkeypatch.setenv("MAC_FLEET", "rocky")
    monkeypatch.setenv("MAC_API_TOKEN", "api-flat")

    captured: dict[str, str] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"mode": "calm"}'

    def _fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(urllib_request, "urlopen", _fake_urlopen)

    assert cli._hub_get_mood("agent_test") == {"mode": "calm"}
    assert captured["auth"] == "Bearer api-flat"
