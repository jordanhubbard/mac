from __future__ import annotations

import json

from mac import cli
from mac.hgx_elastic_capacity import CAPACITY_SCHEMA


def _run(tmp_path, *args):
    del tmp_path
    return cli.main(["--json", *args])


class _FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.registered_agents: list[object] = []

    def status(self, *, pending_request_count: int = 0, registered_agents=None):
        self.calls.append(("status", pending_request_count))
        self.registered_agents.append(registered_agents)
        return {"mode": "status", "read_only": True}

    def plan(self, *, pending_request_count: int = 0, registered_agents=None):
        self.calls.append(("plan", pending_request_count))
        self.registered_agents.append(registered_agents)
        return {"mode": "plan", "read_only": True}

    def execute(self, *, pending_request_count: int = 0, registered_agents=None):
        self.calls.append(("execute", pending_request_count))
        self.registered_agents.append(registered_agents)
        return {
            "mode": "execute",
            "read_only": False,
            "deletion": {"automatic": False, "performed": False},
        }


def test_hgx_capacity_cli_separates_reads_from_explicit_execute(
    tmp_path, monkeypatch, capsys
) -> None:
    controller = _FakeController()
    monkeypatch.setattr(cli, "_hgx_capacity_controller", lambda _args: controller)

    assert _run(
        tmp_path, "hgx", "capacity", "status", "--pending-requests", "1"
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {"mode": "status", "read_only": True}

    assert _run(
        tmp_path, "hgx", "capacity", "plan", "--pending-requests", "2"
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan == {"mode": "plan", "read_only": True}

    assert _run(
        tmp_path, "hgx", "capacity", "execute", "--pending-requests", "3"
    ) == 0
    execute = json.loads(capsys.readouterr().out)
    assert execute["read_only"] is False
    assert execute["deletion"]["automatic"] is False
    assert controller.calls == [("status", 1), ("plan", 2), ("execute", 3)]


def test_hgx_capacity_cli_marks_attested_session_onboarded(
    tmp_path, capsys
) -> None:
    state_path = tmp_path / "capacity.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": CAPACITY_SCHEMA,
                "sessions": {
                    "session-immutable": {
                        "session_id": "session-immutable",
                        "created_by_controller": True,
                        "attestation_status": "passed",
                        "onboarding_status": "not_onboarded",
                    }
                },
                "last_create_at": None,
            }
        ),
        encoding="utf-8",
    )

    assert _run(
        tmp_path,
        "hgx",
        "capacity",
        "mark-onboarded",
        "session-immutable",
        "--agent-id",
        "agent-real",
        "--state-file",
        str(state_path),
    ) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["session_id"] == "session-immutable"
    assert result["agent_id"] == "agent-real"
    assert result["provider_mutation"] is False
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["sessions"]["session-immutable"]["onboarding_status"] == (
        "onboarded"
    )


def test_hgx_capacity_cli_passes_registered_agents_file(
    tmp_path, monkeypatch, capsys
) -> None:
    controller = _FakeController()
    monkeypatch.setattr(cli, "_hgx_capacity_controller", lambda _args: controller)

    registry = tmp_path / "registered.json"
    registry.write_text(
        json.dumps({"hgx-immutable": "agent_worker_1"}), encoding="utf-8"
    )

    assert _run(
        tmp_path,
        "hgx",
        "capacity",
        "plan",
        "--registered-agents-file",
        str(registry),
    ) == 0
    capsys.readouterr()

    assert controller.registered_agents == [{"hgx-immutable": "agent_worker_1"}]
