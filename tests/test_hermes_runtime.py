from __future__ import annotations

import json

from mac.hermes_runtime import stable_id, write_runtime_context


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
    assert set(stored["first_class_objects"]["objects"]) == {"fleets", "tasks", "projects", "agents"}
    assert stored["first_class_objects"]["objects"]["fleets"]["authority"] == "mac"
    assert stored["first_class_objects"]["objects"]["tasks"]["authority"] == "mac"
    assert stored["first_class_objects"]["objects"]["projects"]["authority"] == "mac"
    assert stored["first_class_objects"]["objects"]["agents"]["authority"] == "mac"
    assert "children: subtasks" in "; ".join(
        stored["first_class_objects"]["vocabulary"]["task_relationships"]
    )
    assert "hgmac agents identity agent_rocky_host" in stored["first_class_objects"]["objects"]["agents"]["hgmac_cli"]
    assert "hgmac fleets list" in stored["first_class_objects"]["objects"]["fleets"]["hgmac_cli"]
    assert "hgmac tasks list" in stored["first_class_objects"]["objects"]["tasks"]["hgmac_cli"]
    assert "hgmac tasks add-child {task_id} --title ..." in stored["first_class_objects"]["objects"]["tasks"]["hgmac_cli"]
    assert "hgmac projects list" in stored["first_class_objects"]["objects"]["projects"]["hgmac_cli"]
    assert "/ui?view=fleets&selected={fleet_id}" in stored["first_class_objects"]["objects"]["fleets"]["dashboard_urls"]
    assert "/ui?view=work&selected={task_id}" in stored["first_class_objects"]["objects"]["tasks"]["dashboard_urls"]
    assert "/ui?view=work&project={project}" in stored["first_class_objects"]["objects"]["projects"]["dashboard_urls"]
    assert "/ui?view=agents&selected={agent_id}" in stored["first_class_objects"]["objects"]["agents"]["dashboard_urls"]
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
    assert "mac-hermes register-beads-repository <name> <path> --project <project>" in markdown
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
    assert "hgmac agents claim-next agent_rocky_host --dry-run" in markdown
    assert "mac-hermes claim {task_id} agent_rocky_host" in markdown
    assert "mac-hermes add-child-task {task_id} <child-title>" in markdown
    assert "Direct Session Parity" in markdown
    assert "`mac task ready" in markdown
    assert "`hgmac agents list`" in markdown
    assert "`hgmac fleets list`" in markdown
    assert "`hgmac projects list`" in markdown
    assert "`hgmac tasks list`" in markdown
    assert "`scripts/run-contract-tests.sh`" in markdown
    assert "`hermes_oneshot_executor`" in markdown
    assert "mac-hermes-task-executor" in markdown
    assert "mac-agent --loop --executor" in markdown
    assert "`git commit -m \"<message>\"`" in markdown
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
