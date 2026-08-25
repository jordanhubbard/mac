from __future__ import annotations

import json

from mac.hermes_runtime import stable_id, write_runtime_context

# imports relocated from test_hermes_runtime_edges.py
from pathlib import Path
from typing import Any
import pytest
from mac import hermes_runtime as runtime


def parse_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_write_runtime_context_materializes_mac_task_project_bridge(tmp_path):
    hermes_home = tmp_path / ".hermes"
    mac_home = tmp_path / ".mac"
    workspace = tmp_path / "workspace" / "mac"
    (workspace / ".mac").mkdir(parents=True)
    (workspace / ".mac" / "project.yaml").write_text(
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: repo-beads-mac",
                "toolchain:",
                "  required_commands:",
                "    - python3",
                "    - git",
                "bootstrap:",
                "  command: python3 scripts/bootstrap-project.py",
                "test:",
                "  command: scripts/run-contract-tests.sh",
                "evidence:",
                "  required:",
                "    - repo.pushed",
                "    - tests",
                "",
            ]
        ),
        encoding="utf-8",
    )
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    env_path = hermes_home / ".env"

    context = write_runtime_context(
        context_path=context_path,
        markdown_path=markdown_path,
        hermes_env_path=env_path,
        agent_name="Rocky Host",
        fleet_name="classic-fleet",
        mac_url="http://hub.example.internal:8789/path?token=hidden",
        hermes_home=hermes_home,
        mac_home=mac_home,
        workspace_path=workspace,
    )

    stored = json.loads(context_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    env = parse_env(env_path)
    assert context["schema"] == "mac.hermes.runtime_context.v1"
    assert stored["identity"]["tenant_id"] == "tenant_classic-fleet"
    assert stored["agent"]["agent_id"] == "agent_rocky_host"
    assert stored["identity"]["hermes_instance_id"] == "hermes_rocky_host"
    assert stored["authority"]["tasks"] == "mac"
    assert stored["authority"]["projects"] == "mac"
    assert stored["authority"]["agents"] == "mac"
    assert stored["authority"]["fleets"] == "mac"
    assert stored["authority"]["personality"] == "hermes"
    assert set(stored["first_class_objects"]["objects"]) == {
        "fleets",
        "tasks",
        "projects",
        "agents",
    }
    assert stored["first_class_objects"]["objects"]["fleets"]["authority"] == "mac"
    assert stored["first_class_objects"]["objects"]["tasks"]["authority"] == "mac"
    assert stored["first_class_objects"]["objects"]["projects"]["authority"] == "mac"
    assert stored["first_class_objects"]["objects"]["agents"]["authority"] == "mac"
    assert "children: subtasks" in "; ".join(
        stored["first_class_objects"]["vocabulary"]["task_relationships"]
    )
    # The legacy `hgmac` binary is gone: objects expose `mac`/`mac-hermes` CLI
    # surfaces and the REST API, never an hgmac_cli.
    objects = stored["first_class_objects"]["objects"]
    assert "hgmac_cli" not in objects["agents"]
    assert "hgmac_cli" not in objects["tasks"]
    assert "hgmac_cli" not in objects["fleets"]
    assert "mac-hermes agent-identity agent_rocky_host" in objects["agents"]["mac_hermes_cli"]
    assert "/fleets" in objects["fleets"]["api_paths"]
    assert "mac task list" in objects["tasks"]["mac_cli"]
    assert "mac project list" in objects["projects"]["mac_cli"]
    assert (
        "/ui?view=fleets&selected={fleet_id}"
        in stored["first_class_objects"]["objects"]["fleets"]["dashboard_urls"]
    )
    assert (
        "/ui?view=work&selected={task_id}"
        in stored["first_class_objects"]["objects"]["tasks"]["dashboard_urls"]
    )
    assert (
        "/ui?view=work&project={project}"
        in stored["first_class_objects"]["objects"]["projects"]["dashboard_urls"]
    )
    assert (
        "/ui?view=agents&selected={agent_id}"
        in stored["first_class_objects"]["objects"]["agents"]["dashboard_urls"]
    )
    assert stored["endpoints"]["mac_api"] == "http://hub.example.internal:8789/path"
    assert stored["workspace"]["path"] == str(workspace)
    assert stored["workspace"]["project_contract"]["project"] == "repo-beads-mac"
    capability_names = {item["name"] for item in stored["session_capabilities"]["capabilities"]}
    assert {
        "mac_api",
        "mac_cli",
        "mac_hermes_cli",
        "shell_execution",
        "workspace_file_access",
        "ticket_mirror",
        "mac_task_cli",
        "git_source_control",
        "quality_gate",
        "hermes_oneshot_executor",
        "command_audit",
        "web_search",
    } <= capability_names
    assert "mac-hermes work-context hermes_rocky_host --active-only" in markdown
    assert "Identity boundary" in markdown
    assert "answer only as `Rocky Host`" in markdown
    assert "never claim to be, proxy for, or relay as another agent" in markdown
    assert "mac-hermes tasks --state open" in markdown
    assert "First-Class Objects" in markdown
    assert "MAC Vocabulary" in markdown
    assert "`fleets`: authority `mac`" in markdown
    assert "`tasks`: authority `mac`" in markdown
    assert "`projects`: authority `mac`" in markdown
    assert "`agents`: authority `mac`" in markdown
    assert "Project Bridge" in markdown
    assert "mac-hermes projects" in markdown
    assert "mac-hermes project-detail <project>" in markdown
    assert "mac-hermes project-items" in markdown
    assert "mac-hermes register-project-repository <name> <path> --project <project>" in markdown
    assert "Agent View" in markdown
    assert "mac-hermes agents" in markdown
    assert "mac-hermes claim-next agent_rocky_host --dry-run" in markdown
    assert "mac-hermes command-audit list --agent-id agent_rocky_host" in markdown
    assert "Dashboard Views" in markdown
    assert "/ui?view=work&selected={task_id}" in markdown
    assert "/ui?view=fleets&selected={fleet_id}" in markdown
    assert "/ui?view=projects&project={project}" in markdown
    assert "/ui?view=work&project={project}" in markdown
    assert "/ui?view=agents&selected={agent_id}" in markdown
    assert "Web Research" in markdown
    assert 'mac-hermes web-search "current project dependency release notes" --limit 5' in markdown
    assert "mac-hermes claim-next agent_rocky_host --dry-run" in markdown
    assert "mac-hermes claim {task_id} agent_rocky_host" in markdown
    assert "mac-hermes add-child-task {task_id} <child-title>" in markdown
    assert "Direct Session Parity" in markdown
    assert "`mac task ready" in markdown
    # `hgmac` is gone — agent/fleet/project/task access is via mac / mac-hermes.
    assert "hgmac" not in markdown
    assert "mac-hermes agents" in markdown
    assert "mac-hermes projects" in markdown
    assert "mac-hermes tasks --state open" in markdown
    assert "`scripts/run-contract-tests.sh`" in markdown
    assert "`hermes_oneshot_executor`" in markdown
    assert "mac-task-executor" in markdown
    assert "mac-agent --loop --executor" in markdown
    session_rules = "\n".join(stored["session_capabilities"]["rules"])
    assert "Own the full code lifecycle" in session_rules
    assert "without routing through a human merge gate" in session_rules
    assert "humans direct intent and consume outcomes" in markdown
    assert "ledger remains complete" in markdown
    assert '`git commit -m "<message>"`' in markdown
    assert "`git push`" in markdown
    # mac-dolt-off: bd dolt push was removed from the canonical
    # workflow when dolt sync was disabled. Beads JSONL travels via git.
    assert "`bd dolt push`" not in markdown
    assert env["MAC_HERMES_RUNTIME_CONTEXT_FILE"] == str(context_path)
    assert env["MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"] == str(markdown_path)
    assert env["MAC_HERMES_RUNTIME_CONTEXT_REQUIRED"] == "1"
    assert env["MAC_FLEET_TENANT_ID"] == "tenant_classic-fleet"
    assert env["MAC_HERMES_PERSONA_ID"] == "persona_rocky_host"
    assert env["MAC_HERMES_INSTANCE_ID"] == "hermes_rocky_host"
    assert env["MAC_WORKER_HERMES_INSTANCE_ID"] == "hermes_rocky_host"
    assert env["MAC_AGENT_ID"] == "agent_rocky_host"
    assert env["MAC_WORKER_AGENT_NAME"] == "Rocky Host"
    assert env["MAC_WORKER_HOSTNAME"] == "Rocky Host"
    assert env["MAC_URL"] == "http://hub.example.internal:8789/path"
    assert env["MAC_HUB_URL"] == "http://hub.example.internal:8789/path"
    assert env["HERMES_HOME"] == str(hermes_home)
    assert env["MAC_HERMES_WORKSPACE"] == str(workspace)
    assert env["MAC_PROJECT_CONTRACT_FILE"] == str(workspace / ".mac" / "project.yaml")
    assert "token=hidden" not in str(stored)
    assert "MAC_TOKEN" not in env


def test_stable_id_matches_deployed_worker_id_shape():
    assert stable_id("agent", "Rocky Host") == "agent_rocky_host"
    assert stable_id("hermes", "puck.local") == "hermes_puck.local"


def test_runtime_context_advertises_directory_backed_public_artifact_publish(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_PUBLISH_WEBDAV_ENABLED", "1")
    monkeypatch.setenv("MAC_PUBLISH_DIR", "/srv/mac-artifacts")
    monkeypatch.setenv("MAC_PUBLISH_PUBLIC_URL", "http://principal.example:8790/artifacts")
    hermes_home = tmp_path / ".hermes"
    mac_home = tmp_path / ".mac"
    workspace = tmp_path / "workspace" / "mac"
    workspace.mkdir(parents=True)
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    env_path = hermes_home / ".env"

    context = write_runtime_context(
        context_path=context_path,
        markdown_path=markdown_path,
        hermes_env_path=env_path,
        agent_name="Rocky Host",
        fleet_name="rocky",
        mac_url="http://hub.example:8789",
        hermes_home=hermes_home,
        mac_home=mac_home,
        workspace_path=workspace,
    )

    method = context["publication"]["methods"][0]
    assert method["kind"] == "hub_directory_static_http"
    assert method["publish_dir"] == "/srv/mac-artifacts"
    assert method["public_url"] == "http://principal.example:8790/artifacts"
    assert method["write"]["http_ingress"] is False
    assert method["crud"]["cli"] == "mac admin agentbus artifact-publish"
    assert "--path artifact" in method["example_upload"]
    assert "--public-url" not in method["example_upload"]
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Artifact Publication" in markdown
    assert "MAC_PUBLISH_DIR" in markdown
    env = parse_env(env_path)
    assert env["MAC_PUBLISH_DIR"] == "/srv/mac-artifacts"
    assert env["MAC_PUBLISH_PUBLIC_URL"] == "http://principal.example:8790/artifacts"


# --- relocated from test_hermes_runtime_edges.py (coverage companion folded in) ---


def test_connection_url_invalid_and_ipv6_redaction() -> None:
    assert runtime.connection_url(" local-address ") == "local-address"
    assert (
        runtime.connection_url("https://user:secret@[::1]:8443/path/?token=x")
        == "https://[::1]:8443/path"
    )


def test_set_env_preserves_comments_replaces_removes_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    path.write_text("# heading\n\nMALFORMED\nKEEP=old\nREPLACE=old\nREMOVE=old\n", encoding="utf-8")
    runtime.set_env(path, {"REPLACE": "new", "REMOVE": None, "ADDED": "yes", "SKIP": None})
    text = path.read_text(encoding="utf-8")
    assert "# heading" in text
    assert "MALFORMED" in text
    assert "KEEP=old" in text
    assert "REPLACE=new" in text
    assert "REMOVE=" not in text
    assert text.endswith("ADDED=yes\n")
    assert path.stat().st_mode & 511 == 384


def test_repository_contract_invalid_yaml_and_non_object(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    contract = workspace / ".mac" / "project.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("root: [unterminated", encoding="utf-8")
    assert "error" in runtime._repository_contract(workspace)
    contract.write_text("- a\n- b\n", encoding="utf-8")
    assert (
        runtime._repository_contract(workspace)["error"]
        == "repository contract root is not an object"
    )


def test_main_writes_context_and_reports_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    def fake_write_runtime_context(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "agent": {"agent_id": "agent_edge"},
            "identity": {"hermes_instance_id": "hermes_edge"},
            "endpoints": {"mac_api": ""},
        }

    monkeypatch.setattr(runtime, "write_runtime_context", fake_write_runtime_context)
    result = runtime._main(
        [
            str(tmp_path / "context.json"),
            str(tmp_path / "context.md"),
            str(tmp_path / ".env"),
            "--agent-name",
            "Edge",
            "--fleet-name",
            "fleet",
            "--mac-url",
            "http://mac",
            "--hermes-home",
            str(tmp_path / "hermes"),
            "--mac-home",
            str(tmp_path / "mac"),
            "--tenant-id",
            "tenant",
            "--persona-id",
            "persona",
            "--hermes-instance-id",
            "hermes",
            "--agent-id",
            "agent",
            "--workspace",
            str(tmp_path / "workspace"),
        ]
    )
    assert result == 0
    assert captured["agent_name"] == "Edge"
    assert captured["workspace_path"] == tmp_path / "workspace"
    assert "mac_url=unconfigured" in capsys.readouterr().out
