from __future__ import annotations

from pathlib import Path

import pytest

from mac import coding_agent, fleet_env, ticketing


def test_fleet_env_malformed_quotes_chmod_and_scoped_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parse_path = tmp_path / "parse.env"
    parse_path.write_text("MALFORMED\nVALID=value\n", encoding="utf-8")
    assert fleet_env.parse_env_file(parse_path) == {"VALID": "value"}

    env_path = tmp_path / "chmod.env"
    env_path.write_text("K=old\n", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "chmod",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("unsupported")),
    )
    assert fleet_env.set_env_key(env_path, "K", "new", backup=True)

    migrate = tmp_path / "migrate.env"
    migrate.write_text("MAC_API_TOKEN=has space\n", encoding="utf-8")
    fleet_env.migrate_env_file(migrate, "fleet")
    assert 'MAC_API_TOKEN__FLEET="has space"' in migrate.read_text(encoding="utf-8")

    monkeypatch.setenv("MAC_API_TOKEN__FLEET", "token")
    monkeypatch.setenv("UNSCOPED", "ignored")
    assert ("MAC_API_TOKEN", "FLEET", "token") in list(fleet_env.list_scoped_vars())


def test_coding_agent_probe_json_detection_and_public_error_edges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (
        coding_agent._which("broken", lambda _name: (_ for _ in ()).throw(RuntimeError("probe")))
        is None
    )

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    assert coding_agent._read_json(invalid) is None
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("denied")),
    )
    assert coding_agent._read_json(invalid) is None
    monkeypatch.undo()

    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "stat",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("stat denied")),
    )
    detected = coding_agent._detect_codex({}, tmp_path, lambda _name: "/bin/codex")
    assert detected[0] is False

    unknown = coding_agent.resolve_coding_agent(
        env={"MAC_CODING_AGENT": "mystery"}, home=tmp_path, which=lambda _name: None
    )
    assert any("not a known agent" in line for line in unknown.rationale)
    with pytest.raises(ValueError, match="unknown coding agent"):
        coding_agent._default_argv("unknown", "/bin/agent", "prompt")
    with pytest.raises(ValueError, match="available choice"):
        coding_agent.coding_agent_argv(
            coding_agent.CodingAgentChoice(agent="", available=False), "prompt"
        )


def test_coding_agent_describe_serializes_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        coding_agent,
        "resolve_coding_agent",
        lambda **_kwargs: coding_agent.CodingAgentChoice(
            agent="codex", available=True, binary="/bin/codex"
        ),
    )
    assert '"agent": "codex"' in coding_agent._describe({})


def test_ticketing_base_missing_sources_frontmatter_and_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Connector(ticketing.TicketingConnector):
        def detect(self, repo_path: Path) -> ticketing.TicketSourceReport:
            return ticketing.TicketSourceReport("test", False, False)

    connector = Connector()
    assert connector.import_tickets(tmp_path) == []
    with pytest.raises(NotImplementedError):
        connector.convert(tmp_path, project="p")
    meta = ticketing.MetaTicket(id="ticket", title="Ticket")
    assert connector.on_task_transitioned(meta, "open", "done") is None
    assert connector.on_evidence_added(meta, {}) is None
    assert connector.on_review_claimed(meta, "reviewer") is None

    assert ticketing.NativeTicketingConnector().import_tickets(tmp_path) == []
    assert ticketing.BeadsImportConnector().import_tickets(tmp_path) == []
    plain = tmp_path / "plain.md"
    plain.write_text("no frontmatter", encoding="utf-8")
    assert ticketing._read_frontmatter(plain) == {}
    assert ticketing._read_frontmatter(tmp_path) == {}
    assert ticketing.connector_for("native").name == "native"
    assert ticketing.connector_for("missing") is None
