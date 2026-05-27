import io
import json

from mac.hgmac import run


def test_hgmac_agent_crud_commands_emit_json_and_use_api_paths(monkeypatch):
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body, token))
        return {"method": method, "url": url, "body": body, "token": token}

    out = io.StringIO()
    rc = run(
        [
            "--url",
            "http://hub:8789",
            "--token",
            "secret",
            "agents",
            "create",
            "--machine-id",
            "machine_1",
            "--name",
            "worker",
            "--capabilities",
            "python,ops",
            "--resources-json",
            '{"cpu": 4}',
        ],
        transport=transport,
        stdout=out,
    )

    assert rc == 0
    payload = json.loads(out.getvalue())
    assert payload["method"] == "POST"
    assert payload["url"] == "http://hub:8789/agents"
    assert payload["token"] == "secret"
    assert payload["body"]["capabilities"] == ["ops", "python"]
    assert payload["body"]["resources"] == {"cpu": 4}

    out = io.StringIO()
    rc = run(
        [
            "--url",
            "http://hub:8789",
            "agents",
            "update",
            "agent_1",
            "--status",
            "offline",
            "--health-status",
            "degraded",
        ],
        transport=transport,
        stdout=out,
    )
    assert rc == 0
    assert calls[-1][0] == "PUT"
    assert calls[-1][1] == "http://hub:8789/agents/agent_1"
    assert calls[-1][2]["status"] == "offline"

    run(["--url", "http://hub:8789", "agents", "disable", "agent_1"], transport=transport, stdout=io.StringIO())
    run(["--url", "http://hub:8789", "agents", "delete", "agent_1"], transport=transport, stdout=io.StringIO())
    assert calls[-2][0:2] == ("POST", "http://hub:8789/agents/agent_1/disable")
    assert calls[-1][0:2] == ("DELETE", "http://hub:8789/agents/agent_1")


def test_hgmac_loads_fleet_scoped_token_from_home_env(monkeypatch, tmp_path):
    for key in ("HGMAC_TOKEN", "MAC_API_TOKEN", "MAC_API_TOKEN__ROCKY"):
        monkeypatch.delenv(key, raising=False)
    home_env = tmp_path / ".mac" / ".env"
    home_env.parent.mkdir(parents=True)
    home_env.write_text("MAC_API_TOKEN__ROCKY=rocky-token\nMAC_API_TOKEN=legacy-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body, token))
        return []

    rc = run(
        ["--url", "http://hub:8789", "--fleet", "rocky", "fleets", "list"],
        transport=transport,
        stdout=io.StringIO(),
    )

    assert rc == 0
    assert calls == [("GET", "http://hub:8789/fleets", None, "rocky-token")]


def test_hgmac_exported_env_overrides_home_env(monkeypatch, tmp_path):
    home_env = tmp_path / ".mac" / ".env"
    home_env.parent.mkdir(parents=True)
    home_env.write_text("MAC_API_TOKEN__ROCKY=file-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MAC_API_TOKEN__ROCKY", "exported-token")

    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body, token))
        return []

    rc = run(
        ["--url", "http://hub:8789", "--fleet", "rocky", "fleets", "list"],
        transport=transport,
        stdout=io.StringIO(),
    )

    assert rc == 0
    assert calls[0][3] == "exported-token"


def test_hgmac_covers_related_agent_operations(monkeypatch):
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body))
        return {"ok": True}

    commands = [
        ["agents", "heartbeat", "agent_1", "--status", "idle"],
        ["agents", "claim-next", "agent_1", "--dry-run", "--allowed-project", "nanolang"],
        ["agents", "identity", "agent_1"],
        ["agents", "role", "assign", "agent_1", "reviewer"],
        ["agents", "role", "unassign", "agent_1"],
        ["agents", "mood", "set", "agent_1", "--mode", "focused"],
        ["agents", "mood", "show", "agent_1"],
        ["agents", "mood", "clear", "agent_1"],
        ["agents", "mood", "history", "agent_1", "--limit", "5"],
        ["agents", "nap", "configure", "agent_1", "--offset-minutes", "60"],
        ["agents", "nap", "show", "agent_1"],
        ["agents", "nap", "next", "agent_1"],
        ["agents", "nap", "begin", "agent_1"],
        ["agents", "nap", "runs", "--agent-id", "agent_1"],
        ["agents", "nap", "complete", "nap_1"],
        ["agents", "nap", "fail", "nap_1", "--reason", "interrupted"],
        ["agents", "command-audit", "list", "--agent-id", "agent_1"],
        [
            "agents",
            "command-audit",
            "record",
            "agent_1",
            "--phase",
            "completed",
            "--argv-json",
            '["pytest"]',
            "--cwd",
            "/repo",
        ],
    ]

    for command in commands:
        rc = run(["--url", "http://hub:8789", *command], transport=transport, stdout=io.StringIO())
        assert rc == 0

    paths = [url.removeprefix("http://hub:8789") for _method, url, _body in calls]
    assert "/agents/agent_1/heartbeat" in paths
    assert "/agents/agent_1/claim-next" in paths
    assert "/agents/agent_1/identity" in paths
    assert "/agents/agent_1/role" in paths
    assert "/agents/agent_1/mood" in paths
    assert "/agents/agent_1/nap-schedule" in paths
    assert "/agents/agent_1/nap-runs" in paths
    assert "/nap-runs/nap_1/complete" in paths
    assert "/nap-runs/nap_1/fail" in paths
    assert any(path.startswith("/command-audit?") for path in paths)
    assert "/agents/agent_1/command-audit" in paths


def test_hgmac_exposes_unified_events_and_observability_lists():
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body))
        return {"ok": True}

    commands = [
        [
            "events",
            "list",
            "--subject-type",
            "project",
            "--subject-id",
            "project_1",
            "--prefix",
            "project.",
            "--actor",
            "alice",
        ],
        [
            "observability",
            "list",
            "--kind",
            "log",
            "--layer",
            "control_plane",
            "--level",
            "info",
            "--subject-type",
            "fleet",
            "--subject-id",
            "fleet_1",
            "--after-sequence",
            "10",
        ],
    ]

    for command in commands:
        rc = run(["--url", "http://hub:8789", *command], transport=transport, stdout=io.StringIO())
        assert rc == 0

    paths = [url.removeprefix("http://hub:8789") for _method, url, _body in calls]
    assert (
        "/events?subject_type=project&subject_id=project_1&actor=alice&event_type_prefix=project.&limit=100"
        in paths
    )
    assert (
        "/observability?kind=log&layer=control_plane&level=info&subject_type=fleet&subject_id=fleet_1&after_sequence=10&limit=100"
        in paths
    )


def test_hgmac_first_class_object_crud_commands_use_api_paths():
    calls = []

    def transport(method, url, body, token):
        calls.append((method, url, body))
        return {"ok": True}

    commands = [
        ["fleets", "list", "--status", "active"],
        ["fleets", "show", "classic"],
        ["fleets", "create", "--name", "classic", "--agent-ids", "agent_1,agent_2", "--metadata-json", '{"hub":"rocky"}'],
        ["fleets", "update", "classic", "--status", "inactive", "--agent-id", "agent_1"],
        ["fleets", "delete", "classic"],
        ["tasks", "list", "--state", "open"],
        ["tasks", "show", "task_1"],
        ["tasks", "create", "--title", "Do work", "--project", "nanolang", "--capabilities", "python"],
        ["tasks", "add-child", "task_1", "--title", "Child work", "--capabilities", "python,test"],
        ["tasks", "update", "task_1", "--priority", "5", "--dependencies", "task_0"],
        ["tasks", "delete", "task_1", "--force"],
        ["projects", "list"],
        ["projects", "show", "nanolang"],
        ["projects", "create", "--name", "nanolang", "--metadata-json", '{"repo":"nanolang"}'],
        ["projects", "update", "nanolang", "--status", "archived"],
        ["projects", "delete", "nanolang", "--force"],
    ]

    for command in commands:
        rc = run(["--url", "http://hub:8789", *command], transport=transport, stdout=io.StringIO())
        assert rc == 0

    by_method_path = [(method, url.removeprefix("http://hub:8789")) for method, url, _body in calls]
    assert ("GET", "/fleets?status=active") in by_method_path
    assert ("GET", "/fleets/classic") in by_method_path
    assert ("POST", "/fleets") in by_method_path
    assert ("PUT", "/fleets/classic") in by_method_path
    assert ("DELETE", "/fleets/classic") in by_method_path
    assert ("GET", "/tasks?state=open") in by_method_path
    assert ("GET", "/tasks/task_1") in by_method_path
    assert ("POST", "/tasks") in by_method_path
    assert ("POST", "/tasks/task_1/children") in by_method_path
    assert ("PUT", "/tasks/task_1") in by_method_path
    assert ("DELETE", "/tasks/task_1?force=true&actor=human") in by_method_path
    assert ("GET", "/projects") in by_method_path
    assert ("GET", "/projects/nanolang") in by_method_path
    assert ("POST", "/projects") in by_method_path
    assert ("PUT", "/projects/nanolang") in by_method_path
    assert ("DELETE", "/projects/nanolang?force=true&actor=human") in by_method_path
    assert calls[2][2]["agent_ids"] == ["agent_1", "agent_2"]
    assert calls[7][2]["required_capabilities"] == ["python"]
    assert calls[8][2]["children"][0]["required_capabilities"] == ["python", "test"]
