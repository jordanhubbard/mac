"""Contract tests for MAC's stock OpenClaw/OpenShell gateway deployment."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_DIR = ROOT / "deploy" / "openclaw"
INSTALLER = OPENCLAW_DIR / "install-openclaw-gateway.sh"
CONTAINERFILE = OPENCLAW_DIR / "OpenClaw.Containerfile"
POLICY = OPENCLAW_DIR / "openclaw-policy.yaml"
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"
FLEET_CONFIG = ROOT / "deploy" / "fleet" / "config.yaml"
SYSTEMD_UNIT = ROOT / "deploy" / "systemd" / "mac-openclaw-gateway.service"
APPLY_CRON_PLAN = OPENCLAW_DIR / "apply-cron-plan.mjs"


def _run_apply_cron_plan(
    tmp_path: Path, scenario: str
) -> tuple[subprocess.CompletedProcess[str], dict]:
    plan_path = tmp_path / "cron-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema": "mac.openclaw_cron_migration.v1",
                "jobs": [
                    {
                        "name": "existing-job",
                        "legacy_id": "legacy-existing",
                        "cron": "0 * * * *",
                        "message": "existing",
                        "enabled": True,
                    },
                    {
                        "name": "script-job",
                        "legacy_id": "legacy-script",
                        "cron": "30 * * * *",
                        "message": "script",
                        "legacy_script": "/opt/hermes/dream-cycle.sh",
                        "enabled": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    calls_path = tmp_path / "openclaw-calls.jsonl"
    fake_openclaw = tmp_path / "openclaw"
    fake_openclaw.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["FAKE_OPENCLAW_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")

scenario = os.environ["FAKE_OPENCLAW_SCENARIO"]
is_list = args[:3] == ["cron", "list", "--json"]
name = args[args.index("--name") + 1] if "--name" in args else ""
if scenario == "fatal" and not is_list:
    print("gateway database unavailable", file=sys.stderr)
    raise SystemExit(1)
if scenario in {"deferred", "tainted-deferral"}:
    print(
        "gateway connect failed: GatewayClientRequestError: scope upgrade pending approval "
        "(requestId: a9ee718d-708b-4426-b155-9f28c3c29f92)",
        file=sys.stderr,
    )
    print(
        "GatewayTransportError: gateway closed (1008): pairing required: device is asking "
        "for more scopes than currently approved "
        "(requestId: a9ee718d-708b-4426-b155-9f28c3c29",
        file=sys.stderr,
    )
    print("Gateway target: ws://127.0.0.1:18789", file=sys.stderr)
    print("Source: local loopback", file=sys.stderr)
    print("Config: /home/sandbox/.config/mac-openclaw/openclaw.json", file=sys.stderr)
    print("Bind: lan", file=sys.stderr)
    if scenario == "tainted-deferral":
        print("ERROR gateway database unavailable", file=sys.stderr)
    raise SystemExit(1)
if scenario == "mixed" and name == "script-job":
    print("GatewayTransportError: gateway closed: pairing required", file=sys.stderr)
    raise SystemExit(1)
if is_list:
    print(json.dumps({"jobs": [{"name": "existing-job", "id": "job-1"}]}))
else:
    print("{}")
""",
        encoding="utf-8",
    )
    fake_openclaw.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "MAC_OPENCLAW_BIN": str(fake_openclaw),
            "FAKE_OPENCLAW_CALLS": str(calls_path),
            "FAKE_OPENCLAW_SCENARIO": scenario,
        }
    )
    result = subprocess.run(
        ["node", str(APPLY_CRON_PLAN), str(plan_path)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    return result, {"calls": calls, "plan_path": plan_path}


def test_mac_continuity_plugin_mirrors_fleet_conversation_to_home_channel() -> None:
    """The conversation-mirroring feature: gated by the mirror_fleet_conversation
    flag, summarized via the gateway model, delivered through the sanctioned
    OpenClaw human-message outbox to the home channel."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    # Gated by the config flag (natural-language toggle wires through the LLM
    # + mac_config_flag_set, so the flag name must be exactly this).
    assert '"mirror_fleet_conversation"' in plugin
    assert "mirrorFlagEnabled" in plugin
    # Summarized by the gateway's own model (the "translator").
    assert "/v1/chat/completions" in plugin
    # Delivered via the OpenClaw human-message outbox to the home channel — never
    # a direct provider SDK (that would bypass the gateway identity/lease).
    assert "/communication/deliveries" in plugin
    assert "MAC_OPENCLAW_HOME_CHANNEL" in plugin
    # Mirroring is hooked into the peer bridge after the reply is published.
    assert "mirrorExchangeToHomeChannel" in plugin
    # The set-flag tool teaches the LLM the on/off phrases.
    assert "let me know what you guys are talking about" in plugin


def _seed_hermes_identity(home: Path, name: str = "Test Agent") -> None:
    hermes = home / ".hermes"
    memories = hermes / "memories"
    memories.mkdir(parents=True)
    (hermes / "SOUL.md").write_text(f"# {name}\n\nDistinct test soul.\n", encoding="utf-8")
    (memories / "USER.md").write_text("# User\n\nLearn preferences from evidence.\n", encoding="utf-8")
    (memories / "MEMORY.md").write_text("# Memory\n\nContinuity seed.\n", encoding="utf-8")


def test_stock_openclaw_artifacts_are_pinned_and_do_not_invoke_nemoclaw() -> None:
    assert INSTALLER.stat().st_mode & 0o111
    container = CONTAINERFILE.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "ghcr.io/openclaw/openclaw:2026.6.11@sha256:" in container
    assert 'OPENCLAW_SLACK_PLUGIN_VERSION="2026.6.11"' in container
    assert 'ARG MAC_OPENCLAW_IMAGE_REVISION=' in container
    assert "/etc/mac-openclaw-image-revision" in container
    # OpenShell's sandbox supervisor creates an isolated network namespace
    # inside the image and fails closed when no trusted `ip` helper exists.
    assert "apt-get install -y --no-install-recommends" in container
    # OpenShell prerequisites plus a baked-in dev toolchain (OpenShell forbids
    # container root, so runtime apt is unavailable — tools must ship in the
    # image; user-scoped pip/venv installs go to the writable home).
    for pkg in ("bash iproute2", "python3", "python3-pip", "build-essential", "git"):
        assert pkg in container
    # PEP 668: let the agent pip install --user in this throwaway dev sandbox.
    # The image ENV only reaches the gateway's own process, not OpenShell exec
    # contexts (the agent's tool runs, ad-hoc exec), so removing Debian's
    # EXTERNALLY-MANAGED marker is what actually makes `pip install --user`
    # work in every context — env-independent.
    assert "PIP_BREAK_SYSTEM_PACKAGES=1" in container
    assert "rm -f /usr/lib/python3*/EXTERNALLY-MANAGED" in container
    assert (
        "COPY deploy/verify-bash-contract.sh "
        "/usr/local/bin/mac-verify-bash-contract" in container
    )
    assert container.count("/usr/local/bin/mac-verify-bash-contract") >= 2
    assert "RUN /bin/bash -c" in container
    assert '"npm:@openclaw/slack@${OPENCLAW_SLACK_PLUGIN_VERSION}"' in container
    assert 'OPENCLAW_VERSION="2026.6.11"' in installer
    assert 'OPENCLAW_IMAGE_REVISION="19"' in installer
    assert 'OPENCLAW_IMAGE="localhost/mac-openclaw:${OPENCLAW_VERSION}-mac.${OPENCLAW_IMAGE_REVISION}"' in installer
    assert "/Applications/Docker.app/Contents/Resources/bin/docker" in installer
    assert 'docker_bin="$(find_docker)"' in installer
    assert 'docker_path="$(dirname "$docker_bin"):$PATH"' in installer
    assert 'PATH="$docker_path" "$docker_bin" build --pull' in installer
    assert 'BUILD_CONTEXT="${MAC_OPENCLAW_BUILD_CONTEXT:-$MAC_SRC}"' in installer
    assert '"$BUILD_CONTEXT"' in installer
    assert "USER sandbox" in container
    assert "install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.profile" in container
    assert "install -m 0644 -o sandbox -g sandbox /dev/null /home/sandbox/.bashrc" in container
    assert "nemoclaw gateway" not in container.lower()
    assert "/nemoclaw" not in container.lower()
    # The cutover audit must name the legacy service to prove it is inactive,
    # but the stock installer must never execute a NemoClaw binary.
    assert "/usr/local/bin/nemoclaw" not in installer.lower()
    assert "exec nemoclaw" not in installer.lower()
    assert 'image_revision" = "$OPENCLAW_IMAGE_REVISION"' in installer
    assert installer.count("/bin/bash --noprofile --norc -c") >= 2
    assert "/usr/local/bin/mac-verify-bash-contract" in installer
    assert "/usr/local/bin/node --input-type=module" in installer
    assert "/usr/bin/node --input-type=module" not in installer
    assert "migrate-hermes-continuity.py" in installer
    assert "apply-cron-plan.mjs" in container
    assert "curiosity-sidecar.py /usr/local/bin/curiosity" in container
    assert "/opt/mac-openclaw/plugins/mac-continuity" in container


def test_macos_host_adr_names_the_unresolved_openclaw_darwin_contradiction() -> None:
    """The darwin OpenClaw gap must be recorded as a contradiction, not a decision.

    `fleet-node-install.sh` installs a launchd OpenClaw job on darwin while the
    installer it calls resolves only a Linux image, so a macOS hub silently ends
    up with no gateway. Whoever closes this has to pick one side and delete the
    other; the ADR has to say so, because the tempting one-line "refuse darwin"
    fix breaks the live launchd route and its rollback hook.
    """
    adr = (ROOT / "docs" / "adr" / "0015-macos-nodes-are-host-installs.md").read_text(
        encoding="utf-8"
    )
    consequences = adr.split("## Consequences", maxsplit=1)[1]
    assert "OpenClaw" in consequences
    assert "install_darwin_openclaw_service()" in consequences

    # Both live halves of the contradiction must still exist, so that this test
    # fails if one is removed without the ADR being updated to match.
    installer_sh = (
        ROOT / "deploy" / "fleet-node-install.sh"
    ).read_text(encoding="utf-8")
    assert "install_darwin_openclaw_service() {" in installer_sh
    assert "mac_launchd_transaction_set_rollback_hook withdraw_openclaw_gateway" in (
        installer_sh
    )


def test_openclaw_policy_is_deny_by_default_and_narrowly_allows_required_services() -> None:
    text = POLICY.read_text(encoding="utf-8")
    assert "run_as_user: sandbox" in text
    read_only, read_write = text.split("  read_write:", maxsplit=1)
    assert "/home/sandbox/.config/mac-openclaw" not in read_only
    assert "/home/sandbox/.config/mac-openclaw" in read_write
    assert "- /sandbox" in read_write
    # The agent's home is writable so it can build a dev environment (pip
    # --user / venvs / git checkouts) as the non-root sandbox user.
    assert "- /home/sandbox\n" in read_write
    assert "- /home/sandbox\n" not in read_only
    # Curated developer package-repo egress — enables install/build while the
    # deny-by-default guard still blocks every other host.
    for repo in ("pypi.org", "files.pythonhosted.org", "github.com",
                 "download.pytorch.org", "developer.download.nvidia.com"):
        assert f"host: {repo}" in text
    assert "dev-repos" in text
    # OpenShell's proxy is per-binary; the dev-repos egress must allowlist the
    # tools that fetch (pip runs as python3, git via its remote-https helper).
    assert "/usr/bin/python3" in text
    assert "/usr/lib/git-core/git-remote-https" in text
    assert "__MAC_ROUTER_HOST__" in text
    assert "__MAC_ROUTER_PORT__" in text
    # Hub-local gateways reach the hub via OpenShell's host-bridge alias (the
    # installer rewrites the loopback hub URL to it); the mac-router policy must
    # allow node's egress to that alias too, or the peer bridge silently breaks
    # on the hub node ("Request was cancelled").
    assert "host: host.openshell.internal" in text
    for host in (
        "slack.com",
        "api.slack.com",
        "hooks.slack.com",
        "wss-primary.slack.com",
        "wss-backup.slack.com",
    ):
        assert f"host: {host}" in text
    assert "protocol: websocket" in text
    assert "host: api.telegram.org" in text
    assert 'path: "/bot*/**"' in text
    assert 'path: "/file/bot*/**"' in text
    assert "host: '*'" not in text
    assert "0.0.0.0/0" not in text
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert f"method: {method}" in text


def test_prepare_rewrites_host_loopback_to_openshell_alias(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    home.mkdir()
    _seed_hermes_identity(home)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_hub",
        "MAC_OPENCLAW_INSTANCE_ID": "hermes_hub",
        "MAC_OPENCLAW_ROUTER_URL": "http://127.0.0.1:8789/v1",
        "MAC_OPENCLAW_CONTROL_URL": "http://localhost:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "test",
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    managed = mac_home / "openclaw" / "managed"
    runtime = (managed / "runtime.env").read_text(encoding="utf-8")
    config = (managed / "openclaw.json").read_text(encoding="utf-8")
    policy = (mac_home / "openclaw" / "openclaw-policy.yaml").read_text(encoding="utf-8")
    assert "MAC_OPENCLAW_CONTROL_URL=http://host.openshell.internal:8789" in runtime
    assert "http://host.openshell.internal:8789/v1" in config
    assert "host: host.openshell.internal" in policy
    assert "127.0.0.1:8789" not in runtime
    assert "localhost:8789" not in runtime


def test_stuck_session_recovery_patch_is_wired_exact_and_fail_closed(
    tmp_path: Path,
) -> None:
    """keep_lane fix (task_b6315ed0): the image build patches OpenClaw's
    stuck-session recovery so a terminal last-progress reason (run:completed /
    embedded_run:ended — what the detector logs as terminalProgressStale) with
    queued work reclaims the lane instead of looping keep_lane forever. The
    patcher must be exact-match, idempotent, and fail the build on upstream
    drift rather than silently dropping the fix."""
    patcher = OPENCLAW_DIR / "patches" / "patch-stuck-session-recovery.py"
    container = CONTAINERFILE.read_text(encoding="utf-8")
    # Wired into the image build, applied as root before USER sandbox.
    assert (
        "COPY deploy/openclaw/patches/patch-stuck-session-recovery.py" in container
    )
    assert (
        "RUN python3 /opt/mac-openclaw/patches/patch-stuck-session-recovery.py"
        in container
    )
    assert container.index("patch-stuck-session-recovery.py") < container.index(
        "USER sandbox"
    )

    import importlib.util

    spec = importlib.util.spec_from_file_location("stuck_patch", patcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Patches the verbatim upstream function (surrounding bundle context
    # included to prove exact-match replacement, not whole-file rewrite).
    target = tmp_path / "diagnostic-stuck-session-recovery.runtime-test.js"
    target.write_text(
        "const recoveriesInFlight = new Set();\n"
        + mod.ORIGINAL
        + "\nexport { recoverStuckDiagnosticSession };\n",
        encoding="utf-8",
    )
    assert mod.patch_file(str(target)) == "patched"
    patched = target.read_text(encoding="utf-8")
    assert 'reason === "run:completed"' in patched
    assert 'reason === "embedded_run:ended"' in patched
    assert "task_b6315ed0" in patched
    # The age fallback survives for genuinely idle wedges.
    assert "lastProgressAgeMs >= params.staleAbortMs" in patched
    # Idempotent on a second pass.
    assert mod.patch_file(str(target)) == "already-patched"

    # Fail-closed: upstream drift must abort the build, not skip the fix.
    drifted = tmp_path / "drifted.js"
    drifted.write_text(
        "function isActiveRunProgressStale(params) { return false; }\n",
        encoding="utf-8",
    )
    try:
        mod.patch_file(str(drifted))
    except SystemExit as exc:
        assert "upstream changed" in str(exc)
    else:
        raise AssertionError("patcher must fail on drifted upstream source")


def test_installer_consolidates_agent_geek_knobs_and_plugin_reports_them(
    tmp_path: Path,
) -> None:
    """Config consolidation (task_dfdf6ea9): the installer renders ONE on-host
    agent-config.yaml with the non-secret deploy knobs, runtime.env carries the
    same knobs into the sandbox (including the home channel the mirror needs),
    and the plugin self-reports the document to the hub at startup so
    `mac agent config show <agent>` has a single fleet-wide place to look."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    home.mkdir()
    _seed_hermes_identity(home)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_knobs",
        "MAC_OPENCLAW_INSTANCE_ID": "hermes_knobs",
        "MAC_OPENCLAW_ROUTER_URL": "http://10.0.0.9:8789/v1",
        "MAC_OPENCLAW_CONTROL_URL": "http://10.0.0.9:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "test",
        "MAC_OPENCLAW_HOME_CHANNEL": "channel:C0TEST",
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    # Consolidated on-host document.
    summary = (mac_home / "openclaw" / "agent-config.yaml").read_text(
        encoding="utf-8"
    )
    assert "schema: mac.agent_deploy_config.v1" in summary
    assert "agent_id: agent_knobs" in summary
    assert "image: localhost/mac-openclaw:" in summary
    assert "sandbox: mac-openclaw-knobs" in summary
    assert "home_channel: channel:C0TEST" in summary
    assert "default: test/model" in summary
    # No secrets in the world-readable summary.
    assert "test-token" not in summary
    # runtime.env now carries the knobs into the sandbox process env —
    # including the home channel, whose omission silently killed the
    # conversation mirror on reinstall.
    runtime = (mac_home / "openclaw" / "managed" / "runtime.env").read_text(
        encoding="utf-8"
    )
    assert "MAC_OPENCLAW_HOME_CHANNEL=channel:C0TEST" in runtime
    assert "MAC_OPENCLAW_IMAGE=localhost/mac-openclaw:" in runtime
    assert "MAC_OPENCLAW_SANDBOX=mac-openclaw-knobs" in runtime
    assert "MAC_OPENCLAW_GATEWAY_HOST=" in runtime
    # The plugin self-reports the same document to the hub at startup.
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "reportDeployConfig" in plugin
    assert '"/deploy-config"' in plugin
    assert "mac.agent_deploy_config.v1" in plugin
    # Never ship the router key in the reported document.
    assert "token: cfg.token" not in plugin.split("reportDeployConfig", 1)[1].split(
        "function mutateMood", 1
    )[0]


def test_workspace_context_routes_agent_coordination_over_agentbus(
    tmp_path: Path,
) -> None:
    """Inter-agent coordination goes over the authenticated AgentBus peer
    bridge (task_f7fdadf9), with Slack reserved for humans: the generated
    AGENTS.md must teach the agent to use mac_agent_send instead of
    @mentioning peers in channels, while still leading with SOUL.md
    authority (the personality-flattening guard)."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    home.mkdir()
    _seed_hermes_identity(home)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_bus",
        "MAC_OPENCLAW_INSTANCE_ID": "hermes_bus",
        "MAC_OPENCLAW_ROUTER_URL": "http://10.0.0.9:8789/v1",
        "MAC_OPENCLAW_CONTROL_URL": "http://10.0.0.9:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "test",
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    context = (mac_home / "openclaw" / "workspace" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    # SOUL.md stays authoritative — coordination guidance must not displace it.
    assert "SOUL.md" in context
    assert context.index("SOUL.md") < context.index("mac_agent_send")
    # The coordination contract itself.
    assert "use AgentBus, not Slack" in context
    assert "mac_agent_send" in context
    assert "reply over the bus" in context
    assert "ONE consolidated answer" in context
    assert "mirror_fleet_conversation" in context
    # The fleet trust model, capability-first rewrite (2026-07-13, from the
    # agents' own guidance audit): lead with "act", floor demoted to physics,
    # the lecture tone deleted.
    assert "The fleet trust model" in context
    assert "delegated authority" in context
    one_line = context.replace("\n", " ")
    # Leads with capability ("do it"/"act"), not a paragraph of justification.
    assert "When one asks you to run, measure, check, or review something: do it" in one_line
    # Natasha's framing: the boundary earns the capability.
    assert "boundary is what earns the capability" in one_line
    # The lecture-tone hedge is GONE (was "Do not stall ordinary...").
    assert "Do not stall ordinary" not in one_line
    # The capability-first operating preamble the audit converged on.
    assert "How you work here" in context
    assert "Outputs, not process" in context
    assert "Do not ask permission for ordinary work" in context
    assert "Silence is the only wrong answer" in context
    # The safety floor survives, reworded as physics (singular "sandbox
    # boundary", "revealing secrets", "physics, not permission").
    # The COMPLETE floor — Bullwinkle's adversarial review (2026-07-13)
    # rejected a synthesis that dropped "safety policy" and "review gate";
    # all five named hard-stops must be present so a future rewrite can't
    # quietly weaken the floor again.
    assert "bypass safety policy" in one_line
    assert "review gate" in one_line
    assert "sandbox boundary" in one_line
    assert "reveal secrets" in one_line
    assert "destruction unrelated to the task" in one_line
    assert "physics, not permission" in one_line


def test_peer_bridge_uses_hub_durable_cursors_and_request_endpoint() -> None:
    """Contract layer (task_0d50e190): bridge read positions persist via the
    hub cursor endpoint (sandbox rebuilds resume, not reset), and single-
    recipient waits go through the hub's first-class /agentbus/request."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "loadPeerStateFromHub" in plugin
    assert "persistPeerState" in plugin
    assert '"/agentbus-cursor"' in plugin
    assert '"/agentbus/request"' in plugin
    # Poll paths persist through the hub-backed variant, not the bare local
    # file write (only the definition and the wrapper's own internal call
    # remain).
    assert plugin.count("savePeerState(state)") == 2
    assert plugin.count("persistPeerState(api, state)") >= 5
    # Hub state merges once per gateway start.
    assert "hubStateMerged" in plugin


def test_every_registered_tool_is_declared_in_the_plugin_manifest() -> None:
    """OpenClaw rejects undeclared tools at runtime ('plugin must declare
    contracts.tools') — mac_agent_share and mac_notify_human shipped in
    index.js but not openclaw.plugin.json and silently never registered
    (caught live by natasha, 2026-07-13). The test stub api doesn't enforce
    the manifest, so pin the invariant statically: contracts.tools must be a
    superset of every name registered in index.js."""
    import re

    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    manifest = json.loads(
        (OPENCLAW_DIR / "plugins" / "mac-continuity" / "openclaw.plugin.json").read_text(
            encoding="utf-8"
        )
    )
    declared = set(manifest["contracts"]["tools"])
    names = re.findall(r'registerTool\(\{\s*name: "([^"]+)"', plugin)
    registered = set(names)
    assert registered, "no registerTool calls found — regex drifted from source"
    # The regex must account for EVERY registration: a single-quoted or
    # variable-named registerTool would otherwise vanish silently while the
    # test stayed green on the remainder.
    assert len(names) == plugin.count("registerTool("), (
        "registerTool call count (%d) != extracted names (%d) — a registration "
        "uses a spelling this test cannot see; normalize it or update the regex"
        % (plugin.count("registerTool("), len(names))
    )
    missing = registered - declared
    assert not missing, "tools registered but not declared in openclaw.plugin.json: %s" % sorted(missing)
    # The DEPLOYMENT gate must also cover every tool, or a manifest regression
    # deploys 'successfully' again: extract the required-tools set from the
    # installer's live plugin inspection and require it to be complete.
    installer = INSTALLER.read_text(encoding="utf-8")
    gate_block = installer.split("} <= tools:", 1)[0].rsplit("if not {", 1)[1]
    gated = set(re.findall(r'"([^"]+)"', gate_block))
    unguarded = registered - gated
    assert not unguarded, (
        "tools missing from the installer's live verify gate (deploy would "
        "report success without them): %s" % sorted(unguarded)
    )


def test_installer_tolerates_empty_generated_json_artifacts() -> None:
    """task_9ebbb783: a zero-byte cron-plan.json crashed prepare on the GKE
    pod ('Expecting value: line 1 column 1'). The cron-plan reader must fall
    back to an empty plan on empty/invalid JSON; the verify plugin-status
    reader must emit a clear retryable message (not a raw traceback) when the
    sandbox is still warming up."""
    installer = INSTALLER.read_text(encoding="utf-8")
    # Empty file treated as 'no jobs' (the -s test replaces the missing-only
    # branch), and the python reader defaults to {} on empty/invalid.
    assert 'if [ ! -s "$MIGRATION_DIR/cron-plan.json" ]; then' in installer
    assert "plan = json.loads(text) if text else {}" in installer
    # Verify reader surfaces a retryable message instead of JSONDecodeError.
    assert "sandbox still warming up); retry" in installer
    assert "returned invalid JSON" in installer


def test_apply_cron_plan_defers_script_backed_jobs() -> None:
    """task_c8bb46ec: the Hermes->OpenClaw migration silently dropped the
    pre-run script stage of two-stage jobs (dream-cycle et al). apply-cron-plan
    only read message/delivery and installed script-backed jobs as ENABLED
    message-only jobs with a false 'Migrated losslessly' description, so they
    fired hourly against a prompt referencing a dream log that was never
    produced. Until the script stage is ported, a job carrying legacy_script
    must be installed DISABLED and described honestly."""
    apply = (OPENCLAW_DIR / "apply-cron-plan.mjs").read_text(encoding="utf-8")
    # The dropped field is now read and drives the guard.
    assert "job.legacy_script" in apply
    # Script-backed jobs are forced disabled regardless of job.enabled.
    assert "const enable = hasScript ? false : Boolean(job.enabled);" in apply
    # Honest description instead of the lossless claim for script jobs.
    assert "NOT yet ported to OpenClaw" in apply
    # The lossless description only applies to genuinely message-only jobs.
    losslessline = next(
        line for line in apply.splitlines() if "Migrated losslessly" in line
    )
    assert "hasScript" not in losslessline
    # Surfaces how many jobs were deferred (operator + summary visibility).
    assert "deferred_script_jobs" in apply


def test_apply_cron_plan_receipt_counts_only_successful_cli_mutations(tmp_path: Path) -> None:
    result, evidence = _run_apply_cron_plan(tmp_path, "success")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "mac.openclaw_cron_migration.v1",
        "applied": 2,
        "inventory_device_approval_deferred": False,
        "device_approval_deferred_jobs": 0,
        "deferred_script_jobs": 1,
        "host_script_jobs": 1,
    }
    assert evidence["calls"][0] == ["cron", "list", "--json"]
    assert evidence["calls"][1][:3] == ["cron", "edit", "job-1"]
    assert evidence["calls"][2][:2] == ["cron", "add"]


def test_apply_cron_plan_receipt_reports_device_approval_deferrals(tmp_path: Path) -> None:
    result, evidence = _run_apply_cron_plan(tmp_path, "deferred")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "mac.openclaw_cron_migration.v1",
        "applied": 0,
        "inventory_device_approval_deferred": True,
        "device_approval_deferred_jobs": 2,
        "deferred_script_jobs": 1,
        "host_script_jobs": 1,
    }
    assert evidence["calls"] == [["cron", "list", "--json"]]
    assert result.stderr.count("deferred until device approval") == 1
    assert result.stderr.count("scope_upgrade_pending_approval") == 1
    assert "GatewayClientRequestError" not in result.stderr
    assert "a9ee718d-708b-4426-b155-9f28c3c29f92" not in result.stderr
    host_spec = json.loads(
        (evidence["plan_path"].parent / "host-script-jobs.json").read_text(
            encoding="utf-8"
        )
    )
    assert host_spec["schema"] == "mac.openclaw_host_script_jobs.v1"
    assert [job["name"] for job in host_spec["jobs"]] == ["script-job"]


def test_apply_cron_plan_receipt_distinguishes_mixed_outcomes(tmp_path: Path) -> None:
    result, _ = _run_apply_cron_plan(tmp_path, "mixed")

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["applied"] == 1
    assert receipt["inventory_device_approval_deferred"] is False
    assert receipt["device_approval_deferred_jobs"] == 1
    assert receipt["deferred_script_jobs"] == 1
    assert receipt["host_script_jobs"] == 1
    assert (
        "openclaw cron deferred until device approval: pairing_required"
        in result.stderr
    )
    assert "GatewayTransportError" not in result.stderr


def test_apply_cron_plan_still_fails_on_non_approval_cli_errors(tmp_path: Path) -> None:
    result, evidence = _run_apply_cron_plan(tmp_path, "fatal")

    assert result.returncode != 0
    assert "gateway database unavailable" in result.stderr
    assert "device approval" not in result.stderr
    assert result.stdout == ""
    assert len(evidence["calls"]) == 2


def test_apply_cron_plan_rejects_tainted_device_approval_error(tmp_path: Path) -> None:
    result, evidence = _run_apply_cron_plan(tmp_path, "tainted-deferral")

    assert result.returncode != 0
    assert "scope upgrade pending approval" in result.stderr
    assert "gateway database unavailable" in result.stderr
    assert result.stdout == ""
    assert evidence["calls"] == [["cron", "list", "--json"]]


def test_headless_agents_have_a_human_voice() -> None:
    """A Slack-less agent must still reach humans (jkh 2026-07-13: GKE runners
    have no Slack presence). mac_notify_human sends through the hub delivery
    proxy with automatic attribution when represented, and AGENTS.md tells
    agents this voice exists and that reporting is expected."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    tool = plugin.split('name: "mac_notify_human"', 1)[1].split("registerTool", 1)[0]
    assert "/communication/deliveries" in tool
    assert "MAC_OPENCLAW_HOME_CHANNEL" in tool
    # Attribution for represented (Slack-less) agents.
    assert "MAC_OPENCLAW_PUBLIC_IDENTITY" in tool
    assert "mac.agent_human_notify.v1" in tool
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "Your voice: talking to humans" in installer
    assert "mac_notify_human" in installer
    assert "expected to report" in installer


def test_media_sharing_travels_typed_and_chunked_over_the_bus(tmp_path) -> None:
    """Multimodal sharing (task_ab4ee852, audit 6/7): files travel as typed
    base64 chunks on a dedicated topic — real MIME types, 128KiB raw chunks,
    8MiB cap — and the receive side reassembles to disk, runs a peer turn,
    replies over the bus, and mirrors with the filename. Verified by driving
    the real module's share path against a scripted hub."""
    import json as _json

    plugin_uri = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    sample = tmp_path / "flamegraph.png"
    sample.write_bytes(b"\x89PNG" + b"x" * (300 * 1024))  # forces 3 chunks
    script = f"""
      const mod = await import({_json.dumps(plugin_uri)});
      const tools = new Map();
      const calls = [];
      globalThis.fetch = async (url, init) => {{
        const body = init?.body ? JSON.parse(init.body) : null;
        calls.push({{url: String(url), method: init?.method || "GET", body}});
        if (String(url).endsWith("/agents")) return {{ok: true, json: async () => ([{{id: "agent_peer", name: "Peer"}}])}};
        if (String(url).endsWith("/agentbus/streams") && init?.method === "POST")
          return {{ok: true, json: async () => ({{id: "bus_media1"}})}};
        return {{ok: true, json: async () => ({{}})}};
      }};
      const api = {{
        pluginConfig: {{timeoutMs: 2000}},
        logger: {{warn: () => {{}}}},
        on: () => {{}},
        registerTool: (tool) => tools.set(tool.name, tool),
      }};
      mod.default.register(api);
      const share = tools.get("mac_agent_share");
      if (!share) process.exit(2);
      const result = await share.execute("t1", {{recipient: "Peer", path: {_json.dumps(str(sample))}, note: "benchmark flamegraph"}});
      const parsed = JSON.parse(result.content[0].text);
      if (parsed.status !== "shared") process.exit(3);
      if (parsed.mime !== "image/png") process.exit(4);
      if (parsed.chunk_count !== 3) process.exit(5);
      const appends = calls.filter((c) => c.url.includes("/agentbus/streams/bus_media1/chunks"));
      if (appends.length !== 3) process.exit(6);
      if (!appends.every((c) => c.body.payload_encoding === "base64" && c.body.content_type === "image/png")) process.exit(7);
      if (appends.at(-1).body.final !== true || appends[0].body.final !== false) process.exit(8);
      const open = calls.find((c) => c.url.endsWith("/agentbus/streams"));
      if (open.body.topic !== "mac.media.share.v1") process.exit(9);
      if (open.body.headers.filename !== "flamegraph.png") process.exit(10);
      if (open.body.headers.total_bytes !== {300 * 1024 + 4}) process.exit(11);
    """
    env = {
        **os.environ,
        "MAC_OPENCLAW_AGENT_ID": "agent_me",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=15,
    )
    # Receive side is wired into the bridge poll.
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "pollMediaShares" in plugin
    assert "reassembleMediaChunks" in plugin
    assert "receiveMediaShare" in plugin
    assert 'join(workspaceDir, "incoming")' in plugin


def test_mac_agent_send_supports_group_conversations() -> None:
    """Group semantics (task_588b67fd): several recipients open ONE shared
    stream (participant_agent_ids); members reply as chunks on that same
    stream; the bridge polls group streams with a per-stream cursor; the
    mirror dedupes per reply, not per stream."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "publishGroupMessage" in plugin
    assert "participant_agent_ids" in plugin
    assert "waitForGroupReplies" in plugin
    assert "pollGroupMessages" in plugin
    assert "groupCursors" in plugin
    assert "appendGroupChunk" in plugin
    # The single-recipient path must remain intact.
    assert "publishPeerMessage" in plugin
    assert "waitForPeerReply" in plugin
    # Pair polling must skip group streams (they are chunk-cursor driven).
    assert "!stream?.participants" in plugin
    # Group mirror dedupe key includes the chunk sequence.
    assert "${stream.id}:${sequence}" in plugin


def test_fleet_status_tool_is_capability_aware() -> None:
    """Discovery (task_7debcc9c): the agent-facing fleet tool must surface
    capabilities + hardware from the hub snapshot and accept a capability
    filter, so 'which agents have GPUs?' never needs a human."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    tool = plugin.split('name: "mac_fleet_status"', 1)[1].split("registerTool", 1)[0]
    assert "/fleet/snapshot" in tool
    assert "capability" in tool
    assert "capabilities" in tool
    assert "accelerator" in tool
    assert "hardware" in tool
    assert "departed_at" in tool
    # Discovery pairs with dispatch: the description points at mac_agent_send.
    assert "mac_agent_send" in tool


def test_mac_continuity_plugin_registers_runtime_hook_and_tools() -> None:
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    script = f"""
      const mod = await import({json.dumps(plugin)});
      const hooks = new Map();
      const tools = new Map();
      globalThis.fetch = async () => ({{
        ok: true,
        json: async () => ({{
          mood_prompt: "Current mood: warm",
          memories: [{{summary: "durable fact", score: 0.9}}],
        }}),
      }});
      const api = {{
        pluginConfig: {{maxMemories: 5, timeoutMs: 1000}},
        logger: {{warn: () => {{}}}},
        on: (name, handler) => hooks.set(name, handler),
        registerTool: (tool) => tools.set(tool.name, tool),
      }};
      mod.default.register(api);
      if (!hooks.has("before_prompt_build")) process.exit(2);
      if (!["mac_memory_recall", "mac_memory_store", "mac_mood_current", "mac_mood_set", "mac_mood_clear", "mac_fleet_status", "mac_agent_send", "mac_agent_share", "mac_notify_human", "mac_fs_put", "mac_fs_get", "mac_directive_verify", "mac_agent_inbox", "mac_config_flag_list", "mac_config_flag_set", "mac_config_flag_clear", "mac_image_generate", "curiosity_candidate_submit", "curiosity_candidates_list", "curiosity_abuse_frame"].every((name) => tools.has(name))) process.exit(3);
      const result = await hooks.get("before_prompt_build")({{prompt: "what matters?"}});
      if (!result.prependContext.includes("Current mood: warm")) process.exit(4);
      if (!result.prependContext.includes("durable fact")) process.exit(5);
    """
    env = {
        **os.environ,
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_mac_agent_send_uses_authenticated_agentbus_not_openclaw_session_visibility() -> None:
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    script = f"""
      const mod = await import({json.dumps(plugin)});
      const tools = new Map();
      const requests = [];
      globalThis.fetch = async (url, opts = {{}}) => {{
        const path = new URL(String(url)).pathname;
        requests.push({{path, method: opts.method || "GET", body: opts.body ? JSON.parse(opts.body) : null}});
        if (path === "/agents") return {{ok: true, json: async () => ([{{id: "agent_rocky", name: "rocky"}}])}};
        if (path === "/agentbus") return {{ok: true, json: async () => ({{stream: {{id: "bus_1"}}}})}};
        throw new Error(`unexpected URL ${{url}}`);
      }};
      const api = {{
        pluginConfig: {{}}, logger: {{warn: () => {{}}, info: () => {{}}}},
        on: () => {{}}, registerTool: (tool) => tools.set(tool.name, tool),
      }};
      mod.default.register(api);
      const result = await tools.get("mac_agent_send").execute("call", {{
        recipient: "rocky", message: "coordinate directly", timeoutSeconds: 0,
      }});
      const sent = requests.find((item) => item.path === "/agentbus");
      if (!sent || sent.method !== "POST") process.exit(2);
      if (sent.body.sender_agent_id !== "agent_natasha") process.exit(3);
      if (sent.body.recipient_agent_id !== "agent_rocky") process.exit(4);
      if (sent.body.topic !== "peer.message.v1") process.exit(5);
      if (sent.body.payload.schema !== "mac.agent.peer_message.v1") process.exit(6);
      const output = JSON.parse(result.content[0].text);
      if (output.status !== "queued" || output.stream_id !== "bus_1") process.exit(7);
    """
    env = {
        **os.environ,
        "MAC_OPENCLAW_AGENT_ID": "agent_natasha",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "bound-agent-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )


def test_mac_peer_bridge_turns_authenticated_inbound_stream_into_correlated_reply(
    tmp_path: Path,
) -> None:
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    state_dir = tmp_path / "state"
    agent_dir = tmp_path / "agent"
    workspace = tmp_path / "workspace"
    script = f"""
      const mod = await import({json.dumps(plugin)});
      let service = null;
      const posts = [];
      let runs = 0;
      const incoming = {{
        id: "bus_in", sender_agent_id: "agent_rocky", recipient_agent_id: "agent_natasha",
        topic: "peer.message.v1", status: "closed", created_at: "2026-07-12T00:00:00Z",
        headers: {{correlation_id: "corr-1"}},
      }};
      globalThis.fetch = async (url, opts = {{}}) => {{
        const parsed = new URL(String(url));
        if (parsed.pathname === "/agentbus/streams") return {{ok: true, json: async () => [incoming]}};
        if (parsed.pathname === "/agentbus/streams/bus_in/chunks") return {{ok: true, json: async () => [{{payload: {{
          schema: "mac.agent.peer_message.v1", correlation_id: "corr-1",
          from_agent_id: "agent_rocky", to_agent_id: "agent_natasha", message: "Can you review this?",
        }}}}]}};
        if (parsed.pathname === "/agentbus" && (opts.method || "GET") === "POST") {{
          posts.push(JSON.parse(opts.body));
          return {{ok: true, json: async () => ({{stream: {{id: "bus_reply"}}}})}};
        }}
        throw new Error(`unexpected URL ${{url}}`);
      }};
      const api = {{
        pluginConfig: {{peerPollIntervalMs: 250, peerMaxAttempts: 2}},
        config: {{}},
        logger: {{warn: () => {{}}, info: () => {{}}}},
        on: () => {{}}, registerTool: () => {{}}, registerService: (value) => {{service = value;}},
        runtime: {{
          config: {{current: () => ({{}})}},
          agent: {{
            ensureAgentWorkspace: async () => {{}},
            resolveAgentDir: () => {json.dumps(str(agent_dir))},
            resolveAgentWorkspaceDir: () => {json.dumps(str(workspace))},
            resolveAgentTimeoutMs: () => 1000,
            runEmbeddedAgent: async (params) => {{
              runs += 1;
              if (!params.prompt.includes("Sender: agent_rocky")) process.exit(2);
              if (!params.prompt.includes("Can you review this?")) process.exit(3);
              return {{payloads: [{{text: "Yes, send me the branch."}}]}};
            }},
          }},
        }},
      }};
      mod.default.register(api);
      if (!service || service.id !== "mac-agent-peer-bridge") process.exit(4);
      service.start();
      await new Promise((resolve) => setTimeout(resolve, 120));
      service.stop();
      if (runs !== 1) process.exit(5);
      if (posts.length !== 1) process.exit(6);
      if (posts[0].topic !== "peer.reply.v1") process.exit(7);
      if (posts[0].recipient_agent_id !== "agent_rocky") process.exit(8);
      if (posts[0].payload.correlation_id !== "corr-1") process.exit(9);
      if (posts[0].payload.reply !== "Yes, send me the branch.") process.exit(10);
    """
    env = {
        **os.environ,
        "OPENCLAW_STATE_DIR": str(state_dir),
        "MAC_OPENCLAW_AGENT_ID": "agent_natasha",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "bound-agent-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
    )
    state = json.loads((state_dir / "mac-continuity" / "peer-bridge.json").read_text())
    assert state["processed"] == ["bus_in"]


def test_peer_bridge_signs_embedded_turn_failure_as_non_ok(tmp_path: Path) -> None:
    """Honest outcome (task_7f2ce5e4, incident task_60be7f29): an embedded turn
    that returns "LLM request failed / timed out" as reply text must be signed
    with a non-ok peer.reply status and a structured turn_outcome — never ok —
    and its Slack mirror must carry provenance (model-generated, not execution
    evidence). Drives the real module against a scripted hub."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    state_dir = tmp_path / "state"
    agent_dir = tmp_path / "agent"
    workspace = tmp_path / "workspace"
    script = f"""
      const mod = await import({json.dumps(plugin)});
      let service = null;
      const posts = [];
      const deliveries = [];
      const incoming = {{
        id: "bus_in", sender_agent_id: "agent_rocky", recipient_agent_id: "agent_natasha",
        topic: "peer.message.v1", status: "closed", created_at: "2026-07-12T00:00:00Z",
        headers: {{correlation_id: "corr-1"}},
      }};
      globalThis.fetch = async (url, opts = {{}}) => {{
        const parsed = new URL(String(url));
        if (parsed.pathname === "/agentbus/streams") return {{ok: true, json: async () => [incoming]}};
        if (parsed.pathname === "/agentbus/streams/bus_in/chunks") return {{ok: true, json: async () => [{{payload: {{
          schema: "mac.agent.peer_message.v1", correlation_id: "corr-1",
          from_agent_id: "agent_rocky", to_agent_id: "agent_natasha", message: "Please run the build.",
        }}}}]}};
        if (parsed.pathname.endsWith("/config-flags")) return {{ok: true, json: async () => ({{flags: [{{flag: "mirror_fleet_conversation", value: true}}]}})}};
        if (parsed.pathname === "/agents") return {{ok: true, json: async () => [
          {{id: "agent_rocky", name: "Rocky"}}, {{id: "agent_natasha", name: "Natasha"}},
        ]}};
        if (parsed.pathname === "/communication/deliveries" && (opts.method || "GET") === "POST") {{
          deliveries.push(JSON.parse(opts.body));
          return {{ok: true, json: async () => ({{}})}};
        }}
        if (parsed.pathname === "/agentbus" && (opts.method || "GET") === "POST") {{
          posts.push(JSON.parse(opts.body));
          return {{ok: true, json: async () => ({{stream: {{id: "bus_reply"}}}})}};
        }}
        // Mirror summarizer chat-completion (control URL /v1/chat/completions).
        if (parsed.pathname.endsWith("/v1/chat/completions")) return {{ok: true, json: async () => ({{choices: [{{message: {{content: "Rocky asked Natasha to run the build."}}}}]}})}};
        throw new Error(`unexpected URL ${{url}}`);
      }};
      const api = {{
        pluginConfig: {{peerPollIntervalMs: 250, peerMaxAttempts: 2}},
        config: {{}},
        logger: {{warn: () => {{}}, info: () => {{}}}},
        on: () => {{}}, registerTool: () => {{}}, registerService: (value) => {{service = value;}},
        runtime: {{
          config: {{current: () => ({{}})}},
          agent: {{
            ensureAgentWorkspace: async () => {{}},
            resolveAgentDir: () => {json.dumps(str(agent_dir))},
            resolveAgentWorkspaceDir: () => {json.dumps(str(workspace))},
            resolveAgentTimeoutMs: () => 1000,
            runEmbeddedAgent: async () => ({{
              stop_reason: "turn_limit",
              payloads: [{{text: "LLM request failed / timed out after 300 seconds."}}],
            }}),
          }},
        }},
      }};
      mod.default.register(api);
      service.start();
      await new Promise((resolve) => setTimeout(resolve, 200));
      service.stop();
      const reply = posts.find((p) => p.topic === "peer.reply.v1");
      if (!reply) process.exit(2);
      if (reply.payload.status === "ok") process.exit(3);          // never ok
      if (!reply.payload.turn_outcome) process.exit(4);            // structured
      if (reply.payload.status !== "timeout" && reply.payload.status !== "failed") process.exit(5);
      const mirror = deliveries.find((d) => d.metadata && d.metadata.schema === "mac.fleet_conversation_mirror.v1");
      if (!mirror) process.exit(6);
      if (mirror.metadata.summary_is_model_generated !== true) process.exit(7);
      if (mirror.metadata.is_execution_evidence !== false) process.exit(8);
      if (mirror.metadata.reply_status === "ok") process.exit(9);  // reflects failure
      if (mirror.metadata.turn_binding !== "persona") process.exit(10);
    """
    env = {
        **os.environ,
        "OPENCLAW_STATE_DIR": str(state_dir),
        "MAC_OPENCLAW_AGENT_ID": "agent_natasha",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "bound-agent-token",
        "MAC_OPENCLAW_HOME_CHANNEL": "slack:home",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env,
        check=True,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_peer_bridge_reply_and_mirror_expose_honest_semantics_source() -> None:
    """Static contract: the plugin classifies turn outcomes, signs the honest
    status, and stamps mirror provenance (mirrors src/mac/agentbus_outcomes.py)."""
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "classifyTurnOutcome" in plugin
    assert "replyStatusForOutcome" in plugin
    assert "deliveryOutcome" in plugin
    assert "mirrorProvenance" in plugin
    # The honest-status seam is threaded into publishPeerReply and the mirror.
    assert "turn_outcome" in plugin
    assert "summary_is_model_generated" in plugin
    assert "is_execution_evidence" in plugin
    assert "turn_binding" in plugin


def test_mac_image_generate_posts_to_hub_media_router_and_writes_png() -> None:
    # 1x1 PNG, base64 — what the hub /v1/media/image.generate returns.
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    plugin = (OPENCLAW_DIR / "plugins" / "mac-continuity" / "index.js").as_uri()
    script = f"""
      const {{readFileSync}} = await import("node:fs");
      const mod = await import({json.dumps(plugin)});
      const tools = new Map();
      let captured = null;
      globalThis.fetch = async (url, opts) => {{
        captured = {{url: String(url), method: opts.method, body: JSON.parse(opts.body)}};
        return {{ok: true, json: async () => ({{
          artifacts: [{{base64: {json.dumps(png_b64)}}}],
          provider: "nvidia", model: "black-forest-labs/flux.1-schnell",
        }})}};
      }};
      const api = {{
        pluginConfig: {{}}, logger: {{warn: () => {{}}}},
        on: () => {{}}, registerTool: (t) => tools.set(t.name, t),
      }};
      mod.default.register(api);
      const tool = tools.get("mac_image_generate");
      if (!tool) process.exit(2);
      const out = await tool.execute("id", {{prompt: "a red circle"}});
      // Must hit the hub media router, NOT any nvidia endpoint directly.
      if (!captured.url.endsWith("/v1/media/image.generate")) process.exit(3);
      if (captured.url.includes("nvidia") || captured.url.includes("build.nvidia")) process.exit(4);
      if (captured.body.prompt !== "a red circle") process.exit(5);
      const payload = JSON.parse(out.content[0].text);
      if (!payload.ok || !payload.path.endsWith(".png")) process.exit(6);
      const bytes = readFileSync(payload.path);
      if (bytes.length < 60 || bytes[0] !== 0x89 || bytes[1] !== 0x50) process.exit(7);
    """
    env = {
        **os.environ,
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_CONTROL_URL": "http://hub:8789",
        "MAC_OPENCLAW_ROUTER_API_KEY": "test-token",
    }
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        env=env, check=True, text=True, capture_output=True, timeout=10,
    )


def test_prepare_renders_valid_secret_ref_config_without_log_leaks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    openclaw_home.mkdir(parents=True)
    _seed_hermes_identity(home)
    (openclaw_home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {
                    "name": "offtera",
                    "team_id": "T123",
                    "channel_id": "C123HOME",
                    "channel_name": "#rockyandfriends",
                },
                {
                    "name": "omgjkh",
                    "team_id": "T456",
                    "channel_id": "C456HOME",
                    "channel_name": "#rockyandfriends",
                },
            ]
        ),
        encoding="utf-8",
    )
    secrets = (
        "router-secret-value",
        "xox" + "b-test-placeholder",
        "xap" + "p-test-placeholder",
        "123456:test-telegram-placeholder",
        "xox" + "b-second-workspace-placeholder",
        "xap" + "p-second-workspace-placeholder",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "hermes_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": secrets[0],
        "MAC_OPENCLAW_MODEL": "azure/anthropic/claude-sonnet-4-6",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID": "offtera",
        "MAC_OPENCLAW_SLACK_ACCOUNT_IDS": "offtera,omgjkh",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": secrets[1],
        "MAC_OPENCLAW_SLACK_APP_TOKEN": secrets[2],
        "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN": secrets[4],
        "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN": secrets[5],
        "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN": secrets[3],
    }
    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert all(secret not in output for secret in secrets)

    managed = mac_home / "openclaw" / "managed"
    config_path = managed / "openclaw.json"
    runtime_path = managed / "runtime.env"
    sandbox_identity_path = managed / "sandbox-name"
    wrapper_path = mac_home / "bin" / "openclaw-gateway"
    stop_wrapper_path = mac_home / "bin" / "openclaw-gateway-stop"
    first_runtime = runtime_path.read_text(encoding="utf-8")
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert runtime_path.read_text(encoding="utf-8") == first_runtime
    config = json.loads(config_path.read_text(encoding="utf-8"))
    provider = config["models"]["providers"]["mac-router"]
    slack = config["channels"]["slack"]["accounts"]["offtera"]
    second_slack = config["channels"]["slack"]["accounts"]["omgjkh"]
    telegram = config["channels"]["telegram"]["accounts"]["default"]
    # Per-tool-call progress narration ("Tidepooling… Exec") is suppressed —
    # it's noise in a human chat channel.
    assert config["channels"]["slack"]["streaming"]["progress"]["toolProgress"] is False
    assert config["channels"]["slack"]["streaming"]["preview"]["toolProgress"] is False
    assert config["channels"]["telegram"]["streaming"]["progress"]["toolProgress"] is False
    assert provider["apiKey"] == "${MAC_OPENCLAW_ROUTER_API_KEY}"
    assert provider["headers"] == {
        "x-mac-agent-id": "agent_test",
        "x-mac-hermes-instance-id": "hermes_test",
    }
    assert slack["botToken"]["id"] == "MAC_OPENCLAW_SLACK_OFFTERA_BOT_TOKEN"
    assert slack["appToken"]["id"] == "MAC_OPENCLAW_SLACK_OFFTERA_APP_TOKEN"
    assert second_slack["botToken"]["id"] == "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN"
    assert second_slack["appToken"]["id"] == "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN"
    assert telegram["botToken"]["id"] == "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN"
    assert telegram["dmPolicy"] == "pairing"
    assert telegram["groupPolicy"] == "allowlist"
    assert config["plugins"]["allow"] == ["mac-continuity", "slack", "telegram"]
    assert config["plugins"]["entries"]["slack"] == {"enabled": True}
    assert config["plugins"]["entries"]["telegram"] == {"enabled": True}
    assert config["plugins"]["entries"]["mac-continuity"]["enabled"] is True
    assert config["plugins"]["entries"]["mac-continuity"]["hooks"] == {
        "allowConversationAccess": True,
        "allowPromptInjection": True,
    }
    assert config["plugins"]["load"]["paths"] == [
        "/opt/mac-openclaw/plugins/mac-continuity"
    ]
    assert config["agents"]["defaults"]["workspace"] == "/sandbox/workspace"
    assert "memorySearch" not in config["agents"]["defaults"]
    assert config["plugins"]["slots"]["memory"] == "mac-continuity"
    assert config["plugins"]["entries"]["mac-continuity"]["config"] == {
        "maxMemories": 5,
        "timeoutMs": 10000,
        "peerPollIntervalMs": 2000,
        "peerMaxAttempts": 3,
        "peerTurnTimeoutMs": 300000,
    }
    # OpenClaw session visibility applies only within this one gateway. MAC's
    # authenticated AgentBus bridge owns cross-host fleet communication.
    assert config["tools"]["sessions"]["visibility"] == "agent"
    assert config["gateway"]["auth"]["token"]["id"] == "OPENCLAW_GATEWAY_TOKEN"
    assert all(secret not in config_path.read_text(encoding="utf-8") for secret in secrets)
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert runtime_path.stat().st_mode & 0o777 == 0o600
    assert sandbox_identity_path.read_text(encoding="utf-8") == "mac-openclaw-test\n"
    assert sandbox_identity_path.stat().st_mode & 0o777 == 0o600
    runtime_keys = {
        line.split("=", 1)[0]
        for line in runtime_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert {
        "MAC_OPENCLAW_AGENT_ID",
        "MAC_OPENCLAW_CONTROL_URL",
        "MAC_OPENCLAW_WORKSPACE",
        "MAC_OPENCLAW_SLACK_OFFTERA_APP_TOKEN",
        "MAC_OPENCLAW_SLACK_OFFTERA_BOT_TOKEN",
        "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN",
        "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN",
        "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN",
    } <= runtime_keys
    assert {
        "SLACK_APP_TOKEN",
        "SLACK_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    }.isdisjoint(runtime_keys)
    assert wrapper_path.stat().st_mode & 0o777 == 0o700
    assert stop_wrapper_path.stat().st_mode & 0o777 == 0o700
    curiosity_wrapper = mac_home / "bin" / "curiosity"
    assert curiosity_wrapper.stat().st_mode & 0o777 == 0o700
    assert "/usr/local/bin/curiosity" in curiosity_wrapper.read_text(encoding="utf-8")
    cron_plan = json.loads((managed / "cron-plan.json").read_text(encoding="utf-8"))
    curiosity_job = next(
        job for job in cron_plan["jobs"] if job["name"] == "MAC continuous curiosity review"
    )
    assert curiosity_job["enabled"] is True
    assert "curiosity_candidate_submit" in curiosity_job["message"]
    assert (mac_home / "bin" / "openclaw-message").stat().st_mode & 0o777 == 0o700
    assert (mac_home / "bin" / "openclaw-agent").stat().st_mode & 0o777 == 0o700
    assert (mac_home / "openclaw" / "home-channel-target").read_text(
        encoding="utf-8"
    ).strip() == "channel:C123HOME"
    wrapper = wrapper_path.read_text(encoding="utf-8")
    stop_wrapper = stop_wrapper_path.read_text(encoding="utf-8")
    message_wrapper = (mac_home / "bin" / "openclaw-message").read_text(
        encoding="utf-8"
    )
    agent_wrapper = (mac_home / "bin" / "openclaw-agent").read_text(
        encoding="utf-8"
    )
    managed_entrypoint = (managed / "entrypoint.sh").read_text(encoding="utf-8")
    checkpoint_quiescer = (managed / "checkpoint-quiesce.sh").read_text(
        encoding="utf-8"
    )
    assert "sandbox create" in wrapper
    # GPU passthrough is self-detecting per host: --gpu on CUDA machines, a
    # no-op on GPU-less hosts (Apple Silicon). Scalar (not array) so an empty
    # value under `set -u` doesn't abort bash 3.2 on macOS.
    assert "nvidia-smi -L" in wrapper
    assert "GPU_ARG=--gpu" in wrapper
    assert "sandbox create $GPU_ARG" in wrapper
    assert "-- env HOME=/tmp BASH_ENV=/dev/null /bin/bash --noprofile --norc /home/sandbox/.config/mac-openclaw/entrypoint.sh" in wrapper
    assert managed_entrypoint.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "mac-openclaw-gateway.pid" in managed_entrypoint
    assert "runtime_token" in managed_entrypoint
    assert 'kill -TERM "$writer_pid"' in checkpoint_quiescer
    assert '"/proc/$writer_pid/status"' in checkpoint_quiescer
    assert '"/proc/$writer_pid/comm"' in checkpoint_quiescer
    assert '"/proc/$entrypoint_pid/cmdline"' in checkpoint_quiescer
    assert 'writer_name" = openclaw' in checkpoint_quiescer
    assert "sandbox delete" in stop_wrapper
    assert "sandbox download" in stop_wrapper
    assert "fcntl.flock" in stop_wrapper
    assert "PRAGMA quick_check" in stop_wrapper
    assert "TAR_OPTIONS" not in stop_wrapper
    assert "sandbox_state" in stop_wrapper
    assert "wait_for_sandbox_absent" in stop_wrapper
    assert 'sandbox delete "$SANDBOX" >/dev/null 2>&1 || true' not in stop_wrapper
    assert "/sandbox/workspace" in stop_wrapper
    assert "/sandbox/state" in stop_wrapper
    assert "pgrep -x openclaw" not in stop_wrapper
    assert "trap cleanup EXIT" in wrapper
    assert "stop_gateway" in wrapper
    assert '--upload "$WORKSPACE:/sandbox"' in wrapper
    assert '--upload "$STATE:/sandbox"' in wrapper
    assert "checkpoint-quiesce.sh:/home/sandbox/.config/mac-openclaw/checkpoint-quiesce.sh" in wrapper
    subprocess.run(["bash", "-n", str(wrapper_path)], check=True, timeout=10)
    subprocess.run(["bash", "-n", str(stop_wrapper_path)], check=True, timeout=10)
    subprocess.run(["bash", "-n", str(managed / "checkpoint-quiesce.sh")], check=True, timeout=10)
    assert "set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a" in (
        message_wrapper
    )
    assert "env HOME=/tmp BASH_ENV=/dev/null /bin/bash --noprofile --norc -c" in message_wrapper
    assert "/usr/local/bin/openclaw agent" in agent_wrapper
    assert "env HOME=/tmp BASH_ENV=/dev/null /bin/bash --noprofile --norc -c" in agent_wrapper
    assert "set -a; . /home/sandbox/.config/mac-openclaw/runtime.env; set +a" in (
        INSTALLER.read_text(encoding="utf-8")
    )
    assert "nemoclaw" not in wrapper.lower()
    assert all(secret not in wrapper for secret in secrets)

    rendered_policy = (mac_home / "openclaw" / "openclaw-policy.yaml").read_text(
        encoding="utf-8"
    )
    assert "host: 100.64.0.1" in rendered_policy
    assert "port: 8789" in rendered_policy
    assert "__MAC_" not in rendered_policy


def _render_stop_wrapper_with_fake_openshell(
    tmp_path: Path,
) -> tuple[Path, dict[str, str], Path]:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    _seed_hermes_identity(home)
    calls = tmp_path / "openshell-calls"
    openshell = bin_dir / "openshell"
    openshell.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$MAC_TEST_OPEN_SHELL_CALLS"
case "$1:$2" in
  sandbox:get)
    if [ -n "${MAC_TEST_SANDBOX_STATE:-}" ] \
        && [ "$(sed -n '1p' "$MAC_TEST_SANDBOX_STATE")" = absent ]; then
      cat >&2 <<'DIAGNOSTIC'
Error:   × code: 'Some requested entity was not found', message: "sandbox not found"
DIAGNOSTIC
      exit 1
    fi
    case "${MAC_TEST_SANDBOX_MODE:-active}" in
      active|delete-failure|persistent) exit 0 ;;
      sleeping-inspection) sleep 30 ;;
      absent)
        cat >&2 <<'DIAGNOSTIC'
Error:   × code: 'Some requested entity was not found', message: "sandbox not found"
DIAGNOSTIC
        exit 1
        ;;
      inspection-error)
        echo 'synthetic OpenShell inspection failure' >&2
        exit 70
        ;;
    esac
    ;;
  sandbox:exec)
    exit 0
    ;;
  sandbox:download)
    if [ "${MAC_TEST_DOWNLOAD_FAILURE:-}" = "$4" ]; then
      echo 'synthetic checkpoint download failure' >&2
      exit 74
    fi
    sleep "${MAC_TEST_DOWNLOAD_SLEEP:-0}"
    mkdir -p "$5"
    if [ -n "${MAC_TEST_CHECKPOINT_SOURCE:-}" ]; then
      case "$4" in
        /sandbox/workspace)
          /bin/cp -Rf "$MAC_TEST_CHECKPOINT_SOURCE/workspace/." "$5/"
          ;;
        /sandbox/state)
          /bin/cp -Rf "$MAC_TEST_CHECKPOINT_SOURCE/state/." "$5/"
          ;;
      esac
    else
      printf '%s\n' checkpoint-preserved > "$5/checkpoint.txt"
    fi
    ;;
  sandbox:delete)
    if [ "${MAC_TEST_SANDBOX_MODE:-active}" = delete-failure ]; then
      echo 'synthetic delete rejection' >&2
      exit 9
    fi
    if [ "${MAC_TEST_SANDBOX_MODE:-active}" != persistent ] \
        && [ -n "${MAC_TEST_SANDBOX_STATE:-}" ]; then
      printf '%s\n' absent > "$MAC_TEST_SANDBOX_STATE"
    fi
    ;;
  *) echo "unexpected openshell invocation: $*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    openshell.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": str(openshell),
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_stop_contract",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_stop_contract",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_TEST_OPEN_SHELL_CALLS": str(calls),
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return mac_home / "bin" / "openclaw-gateway-stop", env, calls


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.is_dir():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed_checkpoint_source(root: Path) -> tuple[Path, sqlite3.Connection]:
    source = root / "checkpoint-source"
    workspace = source / "workspace"
    state = source / "state" / "state"
    workspace.mkdir(parents=True)
    state.mkdir(parents=True)
    (workspace / "candidate.txt").write_text("candidate workspace\n", encoding="utf-8")
    database = state / "openclaw.sqlite"
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    connection.execute("INSERT INTO messages(body) VALUES ('from WAL')")
    connection.commit()
    assert Path(str(database) + "-wal").is_file()
    return source, connection


def test_stop_wrapper_quiesces_validates_and_promotes_wal_checkpoint(
    tmp_path: Path,
) -> None:
    stop_wrapper, base_env, calls_path = _render_stop_wrapper_with_fake_openshell(
        tmp_path
    )
    mac_home = Path(base_env["MAC_HOME"])
    state_file = tmp_path / "sandbox-state"
    state_file.write_text("active\n", encoding="utf-8")
    source, connection = _seed_checkpoint_source(tmp_path)
    try:
        result = subprocess.run(
            [str(stop_wrapper)],
            env={
                **base_env,
                "MAC_TEST_SANDBOX_STATE": str(state_file),
                "MAC_TEST_CHECKPOINT_SOURCE": str(source),
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
    finally:
        connection.close()

    assert result.returncode == 0, result.stderr
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    quiesce_index = next(
        index for index, call in enumerate(calls) if "checkpoint-quiesce.sh" in call
    )
    download_indexes = [
        index for index, call in enumerate(calls) if "sandbox download" in call
    ]
    delete_index = next(
        index for index, call in enumerate(calls) if "sandbox delete" in call
    )
    assert quiesce_index < min(download_indexes) < max(download_indexes) < delete_index
    assert state_file.read_text(encoding="utf-8").strip() == "absent"
    promoted = mac_home / "openclaw" / "state" / "state" / "openclaw.sqlite"
    with sqlite3.connect(promoted) as checked:
        assert checked.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert checked.execute("SELECT body FROM messages").fetchone() == ("from WAL",)
    assert (
        mac_home / "openclaw" / "workspace" / "candidate.txt"
    ).read_text(encoding="utf-8") == "candidate workspace\n"
    archives = sorted((mac_home / "openclaw" / "archive").glob("checkpoint-*"))
    assert len(archives) == 1
    assert (archives[0] / "workspace" / "AGENTS.md").is_file()


def test_stop_wrapper_rejects_malformed_sqlite_without_replacing_last_good(
    tmp_path: Path,
) -> None:
    stop_wrapper, base_env, calls_path = _render_stop_wrapper_with_fake_openshell(
        tmp_path
    )
    mac_home = Path(base_env["MAC_HOME"])
    host_root = mac_home / "openclaw"
    (host_root / "workspace" / "last-good.txt").write_text(
        "last good workspace\n", encoding="utf-8"
    )
    (host_root / "state" / "last-good.txt").write_text(
        "last good state\n", encoding="utf-8"
    )
    archive = host_root / "archive" / "checkpoint-existing"
    archive.mkdir()
    (archive / "marker").write_text("existing archive\n", encoding="utf-8")
    before_workspace = _tree_bytes(host_root / "workspace")
    before_state = _tree_bytes(host_root / "state")
    before_archive = _tree_bytes(host_root / "archive")

    source = tmp_path / "invalid-source"
    (source / "workspace").mkdir(parents=True)
    database = source / "state" / "state" / "openclaw.sqlite"
    database.parent.mkdir(parents=True)
    (source / "workspace" / "candidate.txt").write_text("invalid\n", encoding="utf-8")
    database.write_bytes(b"SQLite format 3\x00" + b"not-a-database" * 32)
    state_file = tmp_path / "sandbox-state"
    state_file.write_text("active\n", encoding="utf-8")

    result = subprocess.run(
        [str(stop_wrapper)],
        env={
            **base_env,
            "MAC_TEST_SANDBOX_STATE": str(state_file),
            "MAC_TEST_CHECKPOINT_SOURCE": str(source),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    assert "checkpoint validation failed" in result.stderr
    assert "SQLite quick_check failed" in result.stderr
    assert _tree_bytes(host_root / "workspace") == before_workspace
    assert _tree_bytes(host_root / "state") == before_state
    assert _tree_bytes(host_root / "archive") == before_archive
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert any("checkpoint-quiesce.sh" in call for call in calls)
    assert sum("sandbox download" in call for call in calls) == 2
    assert not any("sandbox delete" in call for call in calls)
    assert state_file.read_text(encoding="utf-8").strip() == "active"
    assert not list(host_root.glob(".checkpoint-*"))


@pytest.mark.parametrize(
    ("failed_source", "diagnostic"),
    (
        ("/sandbox/workspace", "workspace checkpoint download failed"),
        ("/sandbox/state", "state checkpoint download failed"),
    ),
)
def test_stop_wrapper_download_failure_preserves_last_good(
    tmp_path: Path, failed_source: str, diagnostic: str
) -> None:
    stop_wrapper, base_env, calls_path = _render_stop_wrapper_with_fake_openshell(
        tmp_path
    )
    host_root = Path(base_env["MAC_HOME"]) / "openclaw"
    (host_root / "workspace" / "last-good.txt").write_text("workspace\n", encoding="utf-8")
    (host_root / "state" / "last-good.txt").write_text("state\n", encoding="utf-8")
    before_workspace = _tree_bytes(host_root / "workspace")
    before_state = _tree_bytes(host_root / "state")
    before_archive = _tree_bytes(host_root / "archive")
    state_file = tmp_path / "sandbox-state"
    state_file.write_text("active\n", encoding="utf-8")

    result = subprocess.run(
        [str(stop_wrapper)],
        env={
            **base_env,
            "MAC_TEST_SANDBOX_STATE": str(state_file),
            "MAC_TEST_DOWNLOAD_FAILURE": failed_source,
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert _tree_bytes(host_root / "workspace") == before_workspace
    assert _tree_bytes(host_root / "state") == before_state
    assert _tree_bytes(host_root / "archive") == before_archive
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert any("checkpoint-quiesce.sh" in call for call in calls)
    assert not any("sandbox delete" in call for call in calls)
    assert not list(host_root.glob(".checkpoint-*"))


def test_stop_wrapper_serializes_concurrent_checkpoint_attempts(tmp_path: Path) -> None:
    stop_wrapper, base_env, calls_path = _render_stop_wrapper_with_fake_openshell(
        tmp_path
    )
    state_file = tmp_path / "sandbox-state"
    state_file.write_text("active\n", encoding="utf-8")
    env = {
        **base_env,
        "MAC_TEST_SANDBOX_STATE": str(state_file),
        "MAC_TEST_DOWNLOAD_SLEEP": "0.2",
    }
    first = subprocess.Popen(
        [str(stop_wrapper)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    second = subprocess.Popen(
        [str(stop_wrapper)], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    first_output = first.communicate(timeout=15)
    second_output = second.communicate(timeout=15)

    assert first.returncode == 0, first_output
    assert second.returncode == 0, second_output
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert sum("checkpoint-quiesce.sh" in call for call in calls) == 1
    assert sum("sandbox download" in call for call in calls) == 2
    assert sum("sandbox delete" in call for call in calls) == 1


@pytest.mark.parametrize(
    ("sandbox_mode", "expected_error"),
    (
        ("delete-failure", "sandbox delete failed (exit 9)"),
        ("persistent", "sandbox remained present after deletion"),
        ("inspection-error", "could not inspect sandbox"),
    ),
)
def test_stop_wrapper_fails_closed_without_sandbox_absence_proof(
    tmp_path: Path,
    sandbox_mode: str,
    expected_error: str,
) -> None:
    stop_wrapper, base_env, calls_path = _render_stop_wrapper_with_fake_openshell(
        tmp_path
    )
    env = {
        **base_env,
        "MAC_TEST_SANDBOX_MODE": sandbox_mode,
        "MAC_OPENCLAW_SANDBOX_DELETE_TIMEOUT_SECONDS": "0",
    }

    result = subprocess.run(
        [str(stop_wrapper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    if sandbox_mode == "inspection-error":
        assert "exit 70" in result.stderr
        assert not any("sandbox download" in call for call in calls)
        assert not any("sandbox delete" in call for call in calls)
    else:
        download_indexes = [
            index for index, call in enumerate(calls) if "sandbox download" in call
        ]
        delete_index = next(
            index for index, call in enumerate(calls) if "sandbox delete" in call
        )
        assert len(download_indexes) == 2
        assert max(download_indexes) < delete_index
        assert "sandbox remained present after deletion" in result.stderr


@pytest.mark.process_e2e
def test_stop_wrapper_bounds_hung_openshell_inspection(tmp_path: Path) -> None:
    stop_wrapper, base_env, _calls_path = _render_stop_wrapper_with_fake_openshell(
        tmp_path
    )
    env = {
        **base_env,
        "MAC_TEST_SANDBOX_MODE": "sleeping-inspection",
        "MAC_OPENCLAW_SUBPROCESS_TIMEOUT_SECONDS": "1",
    }

    result = subprocess.run(
        [str(stop_wrapper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=6,
    )

    assert result.returncode != 0
    assert "OpenClaw subprocess timed out" in result.stderr
    assert "could not inspect sandbox" in result.stderr


def test_prepare_rejects_unsafe_sandbox_identity_without_replacing_last_good_value(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    managed = mac_home / "openclaw" / "managed"
    managed.mkdir(parents=True)
    sandbox_identity = managed / "sandbox-name"
    sandbox_identity.write_text("mac-openclaw-last-good\n", encoding="utf-8")
    sandbox_identity.chmod(0o600)
    _seed_hermes_identity(home)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_identity_contract",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_identity_contract",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_SANDBOX_NAME": "unsafe\nsecond-record",
    }

    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode != 0
    assert "MAC_OPENCLAW_SANDBOX_NAME must be a lowercase mac-openclaw-* identity" in result.stderr
    assert sandbox_identity.read_text(encoding="utf-8") == "mac-openclaw-last-good\n"


def test_prepare_migrates_legacy_slack_routing_into_openclaw_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    legacy_home = home / ".hermes"
    legacy_home.mkdir(parents=True)
    (legacy_home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {
                    "name": "offtera",
                    "team_id": "T123",
                    "channel_id": "C123HOME",
                    "channel_name": "#rockyandfriends",
                    "ignored_secret": "must-not-migrate",
                }
            ]
        ),
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_LIVE_CANARY": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID": "offtera",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": "xoxb-placeholder",
        "MAC_OPENCLAW_SLACK_APP_TOKEN": "xapp-placeholder",
    }

    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    migrated = mac_home / "openclaw" / "slack_home_channels.json"
    rows = json.loads(migrated.read_text(encoding="utf-8"))
    assert rows == [
        {
            "channel_id": "C123HOME",
            "channel_name": "#rockyandfriends",
            "name": "offtera",
            "team_id": "T123",
        }
    ]
    assert migrated.stat().st_mode & 0o777 == 0o600
    assert "ignored_secret" not in migrated.read_text(encoding="utf-8")
    assert "migrated legacy Slack channel routing" in result.stdout
    assert (mac_home / "openclaw" / "home-channel-target").read_text(
        encoding="utf-8"
    ).strip() == "channel:C123HOME"


def test_verify_waits_for_new_sandbox_and_gateway_health(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    counter = tmp_path / "attempts"
    calls = tmp_path / "calls"
    openclaw_home = mac_home / "openclaw"
    openclaw_home.mkdir(parents=True)
    _seed_hermes_identity(home)
    (openclaw_home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {
                    "name": "offtera",
                    "team_id": "T123",
                    "channel_id": "C123HOME",
                    "channel_name": "#rockyandfriends",
                },
                {
                    "name": "omgjkh",
                    "team_id": "T456",
                    "channel_id": "C456HOME",
                    "channel_name": "#rockyandfriends",
                },
            ]
        ),
        encoding="utf-8",
    )
    openshell = bin_dir / "openshell"
    openshell.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$MAC_TEST_CALLS\"\n"
        "counter=${MAC_TEST_ATTEMPTS:?}\n"
        "attempts=$(cat \"$counter\" 2>/dev/null || echo 0)\n"
        "case \"$1:$2\" in\n"
        "  sandbox:get)\n"
        "    attempts=$((attempts + 1)); echo \"$attempts\" > \"$counter\"\n"
        "    [ \"$attempts\" -ge 3 ]\n"
        "    ;;\n"
            "  sandbox:exec)\n"
            "    case \"$*\" in\n"
            "      *'OPENCLAW_CONTROL_PROBE_OK'*) printf '%s\\n' 'OPENCLAW_CONTROL_PROBE_OK' ;;\n"
            "      *'channels status'*) printf '%s\\n' '{\"channelAccounts\": {\"slack\": [{\"accountId\": \"offtera\", \"enabled\": true, \"configured\": true, \"probe\": {\"ok\": true, \"team\": {\"id\": \"T123\"}}}, {\"accountId\": \"omgjkh\", \"enabled\": true, \"configured\": true, \"probe\": {\"ok\": true, \"team\": {\"id\": \"T456\"}}}]}, \"channelDefaultAccountId\": {\"slack\": \"offtera\"}}' ;;\n"
            "      *'plugins inspect mac-continuity'*) printf '%s\\n' '{\"plugin\": {\"imported\": true, \"status\": \"loaded\", \"toolNames\": [\"memory_search\", \"memory_get\", \"memory_store\", \"mac_memory_recall\", \"mac_memory_store\", \"mac_mood_current\", \"mac_mood_set\", \"mac_mood_clear\", \"mac_fleet_status\", \"mac_agent_send\", \"mac_agent_share\", \"mac_notify_human\", \"mac_fs_put\", \"mac_fs_get\", \"mac_directive_verify\", \"mac_agent_inbox\", \"mac_config_flag_list\", \"mac_config_flag_set\", \"mac_config_flag_clear\", \"mac_image_generate\", \"curiosity_candidate_submit\", \"curiosity_candidates_list\", \"curiosity_abuse_frame\"], \"hookNames\": [\"before_prompt_build\"]}}' ;;\n"
        "      *'curiosity verify'*) printf '%s\\n' '{\"valid\": true, \"events\": 0}' ;;\n"
        "      *'curiosity abuse-frame'*) printf '%s\\n' '{\"possible_false_equivalence\": true}' ;;\n"
        "      *'memory status'*) printf '%s\\n' '{\"files\": 3}' ;;\n"
        "      *'memory search'*) printf '%s\\n' \"$*\" ;;\n"
        "      *'Read your workspace IDENTITY.md'*) printf '%s\\n' '{\"result\": \"mac-hive\"}' ;;\n"
        "      *'openclaw agent'*) printf '%s\\n' '{\"result\": \"MAC_OPENCLAW_CANARY_OK\"}' ;;\n"
        "      *) : ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    openshell.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": str(openshell),
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_headless",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_headless",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID": "offtera",
        "MAC_OPENCLAW_SLACK_ACCOUNT_IDS": "offtera,omgjkh",
        "MAC_OPENCLAW_HOME_CHANNEL": "rockyandfriends",
        "MAC_OPENCLAW_SLACK_BOT_TOKEN": "xoxb-primary-placeholder",
        "MAC_OPENCLAW_SLACK_APP_TOKEN": "xapp-primary-placeholder",
        "MAC_OPENCLAW_SLACK_OMGJKH_BOT_TOKEN": "xoxb-second-placeholder",
        "MAC_OPENCLAW_SLACK_OMGJKH_APP_TOKEN": "xapp-second-placeholder",
        "MAC_OPENCLAW_LIVE_CANARY": "1",
        "MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT": "3",
        "MAC_OPENCLAW_VERIFY_STARTUP_INTERVAL": "0",
        "MAC_TEST_ATTEMPTS": str(counter),
        "MAC_TEST_CALLS": str(calls),
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    result = subprocess.run(
        [str(INSTALLER), "verify"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    assert int(counter.read_text(encoding="utf-8")) >= 3
    assert "verified stock OpenClaw runtime" in result.stdout
    pending = json.loads(
        (mac_home / "openclaw" / "verification-pending.json").read_text(
            encoding="utf-8"
        )
    )
    assert pending["openclaw_runtime"]["verified"] is True
    assert pending["chat_gateway"]["channels"]["slack"] == {
        "account_id": "offtera",
        "account_ids": ["offtera", "omgjkh"],
        "enabled": True,
        "transport": "socket",
    }
    calls_text = calls.read_text(encoding="utf-8")
    assert "--account offtera --target channel:C123HOME" in calls_text
    assert "--account omgjkh --target channel:C456HOME" in calls_text


def test_fleet_deploy_selects_stock_openclaw_on_every_supervisor() -> None:
    config = FLEET_CONFIG.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8") + "\n" + NODE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert "gateway_impl: openclaw" in config
    assert 'openclaw|"")\n      install_linux_openclaw_service' in deploy
    assert "install_darwin_openclaw_service" in deploy
    assert "OPENCLAW_SUPERVISORD_PROG" in deploy
    assert "verify_openclaw_gateway" in deploy
    assert "finalize_openclaw_gateway" in deploy
    assert "ROLLBACK_SUPERVISOR_HELPER" in deploy
    assert '--active-gateway "\\$ROLLBACK_ACTIVE_GATEWAY"' in deploy
    assert "MAC_DEPLOY_OPENCLAW_LIVE_CANARY" in deploy
    assert "MAC_WORKER_RESOURCES_FILE" in deploy
    assert "representation_mode: delegated" in config
    assert "OPENCLAW_REPRESENTATION_MODE" in deploy
    assert 'disable_systemd_service_if_present "$HERMES_SERVICE_NAME"' in deploy
    assert "ExecStart=__MAC_HOME__/bin/openclaw-gateway" in unit
    assert "ExecStop=__MAC_HOME__/bin/openclaw-gateway-stop" in unit
    assert "ExecStopPost=__MAC_HOME__/bin/openclaw-gateway-stop" in unit
    assert "SuccessExitStatus=143 SIGTERM" in unit
    assert "TimeoutStopSec=600" in unit
    assert "User=__MAC_USER__" in unit

    launchd = deploy.split("install_darwin_openclaw_service() {", 1)[1].split(
        "install_darwin_agent_service() {", 1
    )[0]
    assert "<key>ExitTimeOut</key><integer>600</integer>" in launchd
    assert "<key>AbandonProcessGroup</key><false/>" in launchd


def test_openclaw_prefers_reviewed_cli_over_stale_configured_runtime(
    tmp_path: Path,
) -> None:
    mac_home = tmp_path / ".mac"
    reviewed = mac_home / "bin" / "openshell"
    stale = tmp_path / ".local" / "bin" / "openshell"
    reviewed.parent.mkdir(parents=True)
    stale.parent.mkdir(parents=True)
    reviewed.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stale.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    reviewed.chmod(0o700)
    stale.chmod(0o700)
    source = INSTALLER.read_text(encoding="utf-8")
    function = "find_openshell() {" + source.split("find_openshell() {", 1)[1].split(
        "\n}\n\nopenclaw_subprocess_timeout()", 1
    )[0] + "\n}\n"

    result = subprocess.run(
        ["/bin/bash", "-c", function + "\nfind_openshell"],
        env={
            **os.environ,
            "MAC_HOME": str(mac_home),
            "MAC_OPENSHELL_BIN": str(stale),
        },
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == str(reviewed)


def test_openclaw_verification_probes_then_advertises_after_exclusive_cutover() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    assert "validate-openclaw-channel-status.py" in installer
    assert "service-advertisement.json" in installer
    assert '"schema": "mac.chat_gateway_service.v1"' in installer
    assert '"provider": "openshell"' in installer
    assert "--channel slack" in installer
    assert "--channel telegram" in installer
    assert "--dry-run --json" in installer
    assert 'rm -f "$ADVERTISEMENT_PATH" "$VERIFICATION_RECORD_PATH"' in installer
    assert '"schema": "mac.gateway_ownership.v1"' in installer
    assert '"exclusive_channel_owner"' in installer
    assert 'finalize) finalize' in installer
    assert '"endpoint": "openshell://%s"' in installer
    assert '"access": "sandbox_exec"' in installer
    assert "http://127.0.0.1:${GATEWAY_PORT}/healthz" not in installer


def _run_launchd_finalizer(
    tmp_path: Path, launchctl_script: str, *, sandbox_mode: str = "active"
) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, dict[str, object]]]:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n" + launchctl_script,
        encoding="utf-8",
    )
    launchctl.chmod(0o700)
    openshell = bin_dir / "openshell"
    openshell.write_text(
        "#!/bin/sh\n"
        "case \"${MAC_TEST_SANDBOX_MODE:-active}:$1:$2\" in\n"
        "  active:sandbox:get) exit 0 ;;\n"
        "  absent:sandbox:get)\n"
        "    printf '%s\\n' \"Error:   × code: 'Some requested entity was not found', message: \\\"sandbox not found\\\"\" >&2\n"
        "    exit 1\n"
        "    ;;\n"
        "  inspection-error:sandbox:get)\n"
        "    echo 'synthetic OpenShell inspection failure' >&2\n"
        "    exit 70\n"
        "    ;;\n"
        "  *) echo \"unexpected openshell invocation: $*\" >&2; exit 99 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    openshell.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "launchd",
        "MAC_OPENSHELL_BIN": str(openshell),
        "MAC_TEST_SANDBOX_MODE": sandbox_mode,
    }
    result = subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    return result, openclaw_home, pending


def test_finalize_launchd_publishes_only_for_active_openclaw_and_absent_legacy_jobs(
    tmp_path: Path,
) -> None:
    result, openclaw_home, _pending = _run_launchd_finalizer(
        tmp_path,
        """case "$2" in
  *.openclaw-gateway) exit 0 ;;
  *.hermes-gateway|*.nemoclaw-gateway)
    echo 'Could not find service in domain for user' >&2
    exit 113
    ;;
  *) echo "unexpected launchctl target: $*" >&2; exit 99 ;;
esac
""",
    )

    assert result.returncode == 0, result.stderr
    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"] == {
        "schema": "mac.gateway_ownership.v1",
        "exclusive": True,
        "owner": "openclaw",
        "supervisor": "launchd",
        "services": {
            "openclaw": "active",
            "hermes": "inactive",
            "nemoclaw": "inactive",
        },
        "verified_at": advertisement["gateway_ownership"]["verified_at"],
    }
    assert advertisement["openclaw_runtime"]["exclusive_service_owner"] is True
    assert advertisement["chat_gateway"]["exclusive_channel_owner"] is True
    assert not (openclaw_home / "verification-pending.json").exists()


@pytest.mark.parametrize(
    ("invalid_state", "expected_error"),
    (
        ("openclaw-inactive", "OpenClaw service is not active after cutover"),
        ("hermes-active", "Hermes gateway remains active after OpenClaw cutover"),
        ("nemoclaw-active", "NemoClaw gateway remains active after OpenClaw cutover"),
    ),
)
def test_finalize_launchd_rejects_every_false_exclusivity_state(
    tmp_path: Path,
    invalid_state: str,
    expected_error: str,
) -> None:
    result, openclaw_home, pending = _run_launchd_finalizer(
        tmp_path,
        f"""case "$2" in
  *.openclaw-gateway)
    if [ "{invalid_state}" = openclaw-inactive ]; then
      echo 'Could not find service synthetic' >&2
      exit 113
    fi
    exit 0
    ;;
  *.hermes-gateway)
    if [ "{invalid_state}" = hermes-active ]; then exit 0; fi
    echo 'Could not find service synthetic' >&2
    exit 113
    ;;
  *.nemoclaw-gateway)
    if [ "{invalid_state}" = nemoclaw-active ]; then exit 0; fi
    echo 'Could not find service synthetic' >&2
    exit 113
    ;;
  *) echo "unexpected launchctl target: $*" >&2; exit 99 ;;
esac
""",
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not (openclaw_home / "service-advertisement.json").exists()
    assert json.loads(
        (openclaw_home / "verification-pending.json").read_text(encoding="utf-8")
    ) == pending


@pytest.mark.parametrize("failed_service", ["hermes", "nemoclaw"])
def test_finalize_launchd_inspection_error_fails_closed_and_preserves_pending(
    tmp_path: Path,
    failed_service: str,
) -> None:
    result, openclaw_home, pending = _run_launchd_finalizer(
        tmp_path,
        f"""case "$2" in
  *.openclaw-gateway) exit 0 ;;
  *.{failed_service}-gateway)
    echo 'synthetic launchctl transport failure' >&2
    exit 70
    ;;
  *.hermes-gateway|*.nemoclaw-gateway)
    echo 'Could not find service in domain for user' >&2
    exit 113
    ;;
  *) echo "unexpected launchctl target: $*" >&2; exit 99 ;;
esac
""",
    )

    assert result.returncode != 0
    assert (
        f"could not inspect launchd job com.mac.{failed_service}-gateway (exit 70)"
        in result.stderr
    )
    assert "synthetic launchctl transport failure" in result.stderr
    assert not (openclaw_home / "service-advertisement.json").exists()
    pending_path = openclaw_home / "verification-pending.json"
    assert json.loads(pending_path.read_text(encoding="utf-8")) == pending


@pytest.mark.parametrize(
    ("sandbox_mode", "expected_error"),
    (
        ("absent", "OpenClaw sandbox is not active after cutover"),
        ("inspection-error", "could not inspect OpenShell sandbox"),
    ),
)
def test_finalize_requires_proven_active_openshell_sandbox_and_preserves_pending(
    tmp_path: Path,
    sandbox_mode: str,
    expected_error: str,
) -> None:
    result, openclaw_home, pending = _run_launchd_finalizer(
        tmp_path,
        """case "$2" in
  *.openclaw-gateway) exit 0 ;;
  *.hermes-gateway|*.nemoclaw-gateway)
    echo 'Could not find service in domain for user' >&2
    exit 113
    ;;
  *) echo "unexpected launchctl target: $*" >&2; exit 99 ;;
esac
""",
        sandbox_mode=sandbox_mode,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    if sandbox_mode == "inspection-error":
        assert "exit 70" in result.stderr
        assert "synthetic OpenShell inspection failure" in result.stderr
    assert not (openclaw_home / "service-advertisement.json").exists()
    assert json.loads(
        (openclaw_home / "verification-pending.json").read_text(encoding="utf-8")
    ) == pending


def test_finalize_publishes_only_after_legacy_gateways_are_inactive(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n[ \"$1\" != -n ] || shift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) printf 'LoadState=loaded\\nActiveState=active\\n' ;;\n"
        "  *) printf 'LoadState=loaded\\nActiveState=inactive\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "systemd",
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
    }

    subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    assert advertisement["gateway_ownership"]["services"] == {
        "openclaw": "active",
        "hermes": "inactive",
        "nemoclaw": "inactive",
    }
    assert advertisement["openclaw_runtime"]["exclusive_service_owner"] is True
    assert advertisement["chat_gateway"]["exclusive_channel_owner"] is True
    assert not (openclaw_home / "verification-pending.json").exists()


def test_prepare_supports_verified_headless_openclaw_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    _seed_hermes_identity(home, "Headless Test")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_headless",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_headless",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_REPRESENTED_BY": "mac-hive",
        # Stale rollback credentials must not activate channels without a
        # logical public identity assignment.
        "SLACK_BOT_TOKEN": "xoxb-stale",
        "SLACK_APP_TOKEN": "xapp-stale",
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    config = json.loads(
        (mac_home / "openclaw" / "managed" / "openclaw.json").read_text()
    )
    runtime = (mac_home / "openclaw" / "managed" / "runtime.env").read_text()
    workspace = (mac_home / "openclaw" / "workspace" / "AGENTS.md").read_text()
    assert config["channels"] == {}
    assert config["plugins"]["entries"]["slack"] == {"enabled": False}
    assert config["plugins"]["entries"]["telegram"] == {"enabled": False}
    assert config["plugins"]["entries"]["mac-continuity"]["enabled"] is True
    assert "SLACK_APP_TOKEN" not in runtime
    assert "SLACK_BOT_TOKEN" not in runtime
    assert "TELEGRAM_" not in runtime
    assert "Representation mode: delegated" in workspace
    # SOUL.md is wired as the authoritative persona, first and prominently, so
    # the OpenClaw runtime actually embodies the agent's personality instead of
    # leaving SOUL.md an orphaned workspace file (rocky-personality-flattening).
    assert "SOUL.md" in workspace
    who, modes = workspace.split("## Modes you can invoke", maxsplit=1)
    assert "Who you are" in who
    assert "authoritative" in who
    # The curiosity / moral-clarity block is demoted from default temperament to
    # invocable modes — it must NOT read as "this is how you always are."
    assert "Be endlessly curious, ruthless toward bad data" not in workspace
    assert "not your default temperament" in workspace
    assert "Angry Librarian mode" in modes
    assert "false equivalence" in modes
    installer = INSTALLER.read_text(encoding="utf-8")
    assert 'MAC_OPENCLAW_CHANNELS=""' in installer
    assert "local channels=()" not in installer
    assert 'printf \'%s\' "${channels[*]}"' not in installer


def test_public_identity_without_any_channel_credentials_fails_closed(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(home / ".mac"),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_no_channels",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_no_channels",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_PUBLIC_IDENTITY": "mac-hive",
    }

    result = subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )

    assert result.returncode != 0
    assert "has no configured channel credentials" in result.stderr


def test_shell_artifacts_parse() -> None:
    for script in (INSTALLER, DEPLOY):
        subprocess.run(["bash", "-n", str(script)], check=True, timeout=10)


def test_finalize_supervisord_nemoclaw_no_such_process_yields_not_installed(
    tmp_path: Path,
) -> None:
    """supervisorctl returning 'no such process' for the nemoclaw program must
    set services.nemoclaw == not_installed and leave exclusive == True."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n[ \"$1\" != -n ] || shift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    supervisorctl = bin_dir / "supervisorctl"
    supervisorctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway) echo 'mac-openclaw-gateway     RUNNING   pid 1234'; exit 0 ;;\n"
        "  *-hermes-gateway)   echo 'mac-hermes-gateway       STOPPED'; exit 0 ;;\n"
        "  *-nemoclaw-gateway) echo 'mac-nemoclaw-gateway: ERROR (no such process)'; exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    supervisorctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "supervisord",
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
    }

    subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    assert advertisement["gateway_ownership"]["services"]["nemoclaw"] == "not_installed"
    assert not (openclaw_home / "verification-pending.json").exists()


def test_finalize_systemd_nemoclaw_unknown_unit_yields_not_installed(
    tmp_path: Path,
) -> None:
    """An exact systemd LoadState=not-found result is recorded as absent."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n[ \"$1\" != -n ] || shift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) printf 'LoadState=loaded\\nActiveState=active\\n' ;;\n"
        "  *-hermes-gateway.service) printf 'LoadState=loaded\\nActiveState=inactive\\n' ;;\n"
        "  *-nemoclaw-gateway.service) printf 'LoadState=not-found\\nActiveState=inactive\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "systemd",
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
    }

    subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    assert advertisement["gateway_ownership"]["services"]["nemoclaw"] == "not_installed"
    assert not (openclaw_home / "verification-pending.json").exists()


def test_finalize_systemd_hermes_failed_state_normalized_to_inactive(
    tmp_path: Path,
) -> None:
    """systemd ActiveState=failed for the Hermes unit must be normalized to
    inactive; finalize() must succeed and publish
    services.hermes == 'inactive' (the normalized value) with exclusive == True."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n[ \"$1\" != -n ] || shift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    # A failed ActiveState is the durable state of a unit that stopped with an
    # error but has not had reset-failed called yet.
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) printf 'LoadState=loaded\\nActiveState=active\\n' ;;\n"
        "  *-hermes-gateway.service) printf 'LoadState=loaded\\nActiveState=failed\\n' ;;\n"
        "  *-nemoclaw-gateway.service) printf 'LoadState=not-found\\nActiveState=inactive\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "systemd",
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
    }

    result = subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, (
        f"finalize() must succeed when hermes is in 'failed' state (normalized to inactive);\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    advertisement = json.loads(
        (openclaw_home / "service-advertisement.json").read_text(encoding="utf-8")
    )
    assert advertisement["gateway_ownership"]["exclusive"] is True
    # The installer normalizes failed -> inactive before recording state.
    assert advertisement["gateway_ownership"]["services"]["hermes"] == "inactive"
    assert advertisement["gateway_ownership"]["services"]["nemoclaw"] == "not_installed"
    assert not (openclaw_home / "verification-pending.json").exists()


def test_finalize_systemd_hermes_active_still_dies(
    tmp_path: Path,
) -> None:
    """An active Hermes unit must block finalization and publication."""
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text(
        "#!/bin/sh\n[ \"$1\" != -n ] || shift\nexec \"$@\"\n",
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    # hermes is still active — OpenClaw cutover has not completed.
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *-openclaw-gateway.service) printf 'LoadState=loaded\\nActiveState=active\\n' ;;\n"
        "  *-hermes-gateway.service) printf 'LoadState=loaded\\nActiveState=active\\n' ;;\n"
        "  *-nemoclaw-gateway.service) printf 'LoadState=not-found\\nActiveState=inactive\\n' ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_test",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_test",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": "systemd",
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
    }

    result = subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode != 0, (
        "finalize() must fail (non-zero exit) when hermes is still active after OpenClaw cutover"
    )
    assert not (openclaw_home / "service-advertisement.json").exists(), (
        "service-advertisement.json must NOT be written when hermes is still active"
    )


def _run_linux_finalizer_with_probe(
    tmp_path: Path, supervisor: str, probe_script: str
) -> tuple[subprocess.CompletedProcess[str], Path, dict[str, object]]:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    bin_dir = tmp_path / "bin"
    openclaw_home.mkdir(parents=True)
    bin_dir.mkdir()
    pending: dict[str, object] = {
        "openclaw_runtime": {"implementation": "openclaw", "verified": True},
        "chat_gateway": {"implementation": "openclaw", "verified": True},
    }
    (openclaw_home / "verification-pending.json").write_text(
        json.dumps(pending), encoding="utf-8"
    )
    sudo = bin_dir / "sudo"
    sudo.write_text(
        '#!/bin/sh\n[ "$1" != -n ] || shift\nexec "$@"\n',
        encoding="utf-8",
    )
    sudo.chmod(0o700)
    probe = bin_dir / ("systemctl" if supervisor == "systemd" else "supervisorctl")
    probe.write_text("#!/bin/sh\n" + probe_script, encoding="utf-8")
    probe.chmod(0o700)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_AGENT_ID": "agent_probe_contract",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_probe_contract",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": supervisor,
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
    }
    result = subprocess.run(
        [str(INSTALLER), "finalize"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    return result, openclaw_home, pending


@pytest.mark.parametrize(
    ("supervisor", "probe_script", "expected_error"),
    (
        (
            "systemd",
            """case "$2" in
  *-openclaw-gateway.service) printf 'LoadState=loaded\nActiveState=active\n' ;;
  *-hermes-gateway.service) echo 'transport unavailable'; exit 70 ;;
  *-nemoclaw-gateway.service) printf 'LoadState=not-found\nActiveState=inactive\n' ;;
esac
""",
            "could not inspect systemd unit mac-hermes-gateway.service (exit 70)",
        ),
        (
            "supervisord",
            """case "$2" in
  *-openclaw-gateway) echo 'mac-openclaw-gateway RUNNING pid 12'; exit 0 ;;
  *-hermes-gateway) echo 'supervisor transport unavailable'; exit 70 ;;
  *-nemoclaw-gateway) echo 'mac-nemoclaw-gateway: ERROR (no such process)'; exit 1 ;;
esac
""",
            "could not inspect supervisord program mac-hermes-gateway (exit 70)",
        ),
    ),
)
def test_finalize_linux_legacy_probe_errors_are_unknown_not_absent(
    tmp_path: Path,
    supervisor: str,
    probe_script: str,
    expected_error: str,
) -> None:
    result, openclaw_home, pending = _run_linux_finalizer_with_probe(
        tmp_path, supervisor, probe_script
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not (openclaw_home / "service-advertisement.json").exists()
    assert json.loads(
        (openclaw_home / "verification-pending.json").read_text(encoding="utf-8")
    ) == pending


def _run_rollback(
    tmp_path: Path,
    supervisor: str,
    scenario: str = "success",
    *,
    action: str = "rollback",
    fleet_transaction: bool = False,
    sandbox_identity: str | None = "mac-openclaw-rollback-contract",
    runtime_env: str | None = None,
    runtime_env_mode: int = 0o600,
    runtime_env_artifact: str = "file",
) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    openclaw_home = mac_home / "openclaw"
    managed = openclaw_home / "managed"
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    managed.mkdir(parents=True)
    bin_dir.mkdir()
    state_dir.mkdir()
    if sandbox_identity is not None:
        sandbox_identity_path = managed / "sandbox-name"
        sandbox_identity_path.write_text(sandbox_identity + "\n", encoding="utf-8")
        sandbox_identity_path.chmod(0o600)
    if runtime_env is not None:
        runtime_path = managed / "runtime.env"
        if runtime_env_artifact == "file":
            runtime_path.write_text(runtime_env, encoding="utf-8")
            runtime_path.chmod(runtime_env_mode)
        elif runtime_env_artifact == "symlink":
            target = tmp_path / "runtime-target.env"
            target.write_text(runtime_env, encoding="utf-8")
            target.chmod(runtime_env_mode)
            runtime_path.symlink_to(target)
        elif runtime_env_artifact == "directory":
            runtime_path.mkdir()
        else:  # pragma: no cover - harness misuse.
            raise ValueError(runtime_env_artifact)
    (openclaw_home / "service-advertisement.json").write_text(
        '{"advertised": true}\n', encoding="utf-8"
    )
    (openclaw_home / "verification-pending.json").write_text(
        '{"verified": true}\n', encoding="utf-8"
    )
    calls = tmp_path / "rollback-calls"

    openshell = bin_dir / "openshell"
    openshell.write_text(
        """#!/bin/sh
printf 'openshell %s\n' "$*" >> "$MAC_TEST_CALLS"
case "$1:$2" in
  sandbox:get)
    case "$MAC_TEST_SCENARIO" in
      sandbox-inspection-error)
        echo 'synthetic sandbox inspection failure' >&2
        exit 70
        ;;
      sandbox-timeout) sleep 30 ;;
    esac
    if [ -f "$MAC_TEST_STATE/sandbox-deleted" ]; then
      cat >&2 <<'DIAGNOSTIC'
Error:   × code: 'Some requested entity was not found', message: "sandbox not found"
DIAGNOSTIC
      exit 1
    fi
    exit 0
    ;;
  sandbox:delete)
    if [ "$MAC_TEST_SCENARIO" = sandbox-delete-failure ]; then
      echo 'synthetic sandbox delete failure' >&2
      exit 9
    fi
    touch "$MAC_TEST_STATE/sandbox-deleted"
    ;;
  *) echo "unexpected openshell invocation: $*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    openshell.chmod(0o700)
    sudo = bin_dir / "sudo"
    sudo.write_text(
        """#!/bin/sh
printf 'sudo %s\n' "$*" >> "$MAC_TEST_CALLS"
[ "$1" = -n ] || exit 98
shift
exec "$@"
""",
        encoding="utf-8",
    )
    sudo.chmod(0o700)

    if supervisor == "systemd":
        command = bin_dir / "systemctl"
        command.write_text(
            """#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$MAC_TEST_CALLS"
case "$1" in
  show)
    case "$2" in
      *-openclaw-gateway.service)
        if [ "$MAC_TEST_SCENARIO" = supervisor-inspection-error ]; then
          echo 'synthetic systemd transport failure'; exit 70
        fi
        if [ -f "$MAC_TEST_STATE/openclaw-stopped" ]; then
          printf 'LoadState=loaded\nActiveState=inactive\n'; exit 0
        fi
        printf 'LoadState=loaded\nActiveState=active\n'; exit 0
        ;;
      *-hermes-gateway.service)
        if [ -f "$MAC_TEST_STATE/hermes-started" ] \
            && [ "$MAC_TEST_SCENARIO" != hermes-stays-inactive ]; then
          case " $* " in
            *" -p SubState "*)
              if [ "$MAC_TEST_SCENARIO" = hermes-bad-runtime-state ]; then
                printf 'LoadState=loaded\nActiveState=active\nSubState=exited\nMainPID=13\nUnitFileState=enabled\n'
              else
                printf 'LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=13\nUnitFileState=enabled\n'
              fi
              ;;
            *) printf 'LoadState=loaded\nActiveState=active\n' ;;
          esac
          exit 0
        fi
        printf 'LoadState=loaded\nActiveState=inactive\n'; exit 0
        ;;
    esac
    ;;
  disable)
    if [ "$MAC_TEST_SCENARIO" = supervisor-stop-failure ]; then exit 71; fi
    if [ "$MAC_TEST_SCENARIO" != supervisor-still-active ]; then
      touch "$MAC_TEST_STATE/openclaw-stopped"
    fi
    ;;
  enable)
    case "$MAC_TEST_SCENARIO" in
      hermes-start-failure) exit 72 ;;
      hermes-start-timeout)
        python3 -c 'import fcntl,os,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); f=open(os.environ["MAC_TEST_TIMEOUT_LOCK"],"w"); fcntl.flock(f,fcntl.LOCK_EX); open(os.environ["MAC_TEST_TIMEOUT_READY"],"w").write("ready"); os.close(1); os.close(2); signal.pause()'
        ;;
      *) touch "$MAC_TEST_STATE/hermes-started" ;;
    esac
    ;;
esac
""",
            encoding="utf-8",
        )
    elif supervisor == "supervisord":
        command = bin_dir / "supervisorctl"
        command.write_text(
            """#!/bin/sh
printf 'supervisorctl %s\n' "$*" >> "$MAC_TEST_CALLS"
case "$1:$2" in
  status:*-openclaw-gateway)
    if [ "$MAC_TEST_SCENARIO" = supervisor-inspection-error ]; then
      echo 'synthetic supervisor transport failure'; exit 70
    fi
    if [ -f "$MAC_TEST_STATE/openclaw-stopped" ]; then
      echo 'mac-openclaw-gateway STOPPED'; exit 0
    fi
    echo 'mac-openclaw-gateway RUNNING pid 12'; exit 0
    ;;
  stop:*-openclaw-gateway)
    if [ "$MAC_TEST_SCENARIO" = supervisor-stop-failure ]; then exit 71; fi
    if [ "$MAC_TEST_SCENARIO" != supervisor-still-active ]; then
      touch "$MAC_TEST_STATE/openclaw-stopped"
    fi
    ;;
  start:*-hermes-gateway)
    case "$MAC_TEST_SCENARIO" in
      hermes-start-failure) exit 72 ;;
      hermes-start-timeout)
        python3 -c 'import fcntl,os,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); f=open(os.environ["MAC_TEST_TIMEOUT_LOCK"],"w"); fcntl.flock(f,fcntl.LOCK_EX); open(os.environ["MAC_TEST_TIMEOUT_READY"],"w").write("ready"); os.close(1); os.close(2); signal.pause()'
        ;;
      *) touch "$MAC_TEST_STATE/hermes-started" ;;
    esac
    ;;
  status:*-hermes-gateway)
    if [ -f "$MAC_TEST_STATE/hermes-started" ] \
        && [ "$MAC_TEST_SCENARIO" != hermes-stays-inactive ]; then
      if [ "$MAC_TEST_SCENARIO" = hermes-missing-pid ]; then
        echo 'mac-hermes-gateway RUNNING'
      else
        echo 'mac-hermes-gateway RUNNING pid 13'
      fi
    else
      echo 'mac-hermes-gateway STOPPED'
    fi
    ;;
  *) echo "unexpected supervisorctl invocation: $*" >&2; exit 99 ;;
esac
""",
            encoding="utf-8",
        )
    else:
        command = bin_dir / "launchctl"
        command.write_text(
            """#!/bin/sh
printf 'launchctl %s\n' "$*" >> "$MAC_TEST_CALLS"
case "$1" in
  print)
    case "$2" in
      *.openclaw-gateway)
        if [ "$MAC_TEST_SCENARIO" = supervisor-inspection-error ]; then
          echo 'synthetic launchctl transport failure' >&2; exit 70
        fi
        if [ -f "$MAC_TEST_STATE/openclaw-stopped" ]; then
          echo 'Could not find service synthetic' >&2; exit 113
        fi
        exit 0
        ;;
      *.hermes-gateway)
        if [ -f "$MAC_TEST_STATE/hermes-started" ]; then exit 0; fi
        echo 'Could not find service synthetic' >&2; exit 113
        ;;
    esac
    ;;
  bootout)
    if [ "$MAC_TEST_SCENARIO" = supervisor-stop-failure ]; then exit 71; fi
    if [ "$MAC_TEST_SCENARIO" != supervisor-still-active ]; then
      touch "$MAC_TEST_STATE/openclaw-stopped"
    fi
    ;;
  disable|enable) exit 0 ;;
  bootstrap) touch "$MAC_TEST_STATE/hermes-started" ;;
  *) echo "unexpected launchctl invocation: $*" >&2; exit 99 ;;
esac
""",
            encoding="utf-8",
        )
    command.chmod(0o700)

    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENCLAW_FLEET_NAME": "mac",
        "MAC_OPENCLAW_SUPERVISOR": supervisor,
        "MAC_OPENSHELL_BIN": str(openshell),
        "MAC_OPENCLAW_SUBPROCESS_TIMEOUT_SECONDS": "1",
        "MAC_OPENCLAW_SANDBOX_DELETE_TIMEOUT_SECONDS": "0",
        "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "0.2",
        "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS": "0.3",
        "MAC_LAUNCHD_POLL_INTERVAL_SECONDS": "0.01",
        "MAC_TEST_CALLS": str(calls),
        "MAC_TEST_SCENARIO": scenario,
        "MAC_TEST_STATE": str(state_dir),
        "MAC_TEST_RUNTIME_SOURCE_MARKER": str(tmp_path / "runtime-env-was-sourced"),
        "MAC_TEST_TIMEOUT_LOCK": str(tmp_path / "timeout-child.lock"),
        "MAC_TEST_TIMEOUT_READY": str(tmp_path / "timeout-child.ready"),
    }
    if fleet_transaction:
        env["MAC_DEPLOY_GENERATION"] = "release-epoch:agent-test:attempt-1"
    result = subprocess.run(
        [str(INSTALLER), action],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=8,
    )
    return (
        result,
        calls.read_text(encoding="utf-8").splitlines() if calls.exists() else [],
        openclaw_home,
    )


def test_rollback_recovers_private_runtime_sandbox_identity_without_sourcing(
    tmp_path: Path,
) -> None:
    secret = "rollback-runtime-secret-must-not-leak"
    sandbox = "mac-openclaw-runtime-fallback"
    runtime = (
        "# Generated host-local OpenClaw runtime environment.\n"
        f"OPENCLAW_GATEWAY_TOKEN='{secret}'\n"
        "NOT_MAC_OPENCLAW_SANDBOX='mac-openclaw-wrong'\n"
        f"MAC_OPENCLAW_SANDBOX='{sandbox}'\n"
        'MAC_OPENCLAW_HOSTILE=$(touch "$MAC_TEST_RUNTIME_SOURCE_MARKER")\n'
    )

    result, calls, _ = _run_rollback(
        tmp_path,
        "systemd",
        action="withdraw",
        sandbox_identity=None,
        runtime_env=runtime,
    )

    assert result.returncode == 0, result.stderr
    assert any(f"openshell sandbox delete {sandbox}" in call for call in calls)
    assert any(f"openshell sandbox get {sandbox}" in call for call in calls)
    assert all("mac-openclaw-wrong" not in call for call in calls)
    assert not (tmp_path / "runtime-env-was-sourced").exists()
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_rollback_prefers_canonical_sandbox_identity_over_runtime_fallback(
    tmp_path: Path,
) -> None:
    canonical = "mac-openclaw-canonical-identity"
    stale = "mac-openclaw-stale-runtime-identity"
    result, calls, _ = _run_rollback(
        tmp_path,
        "systemd",
        action="withdraw",
        sandbox_identity=canonical,
        runtime_env=f"MAC_OPENCLAW_SANDBOX='{stale}'\n",
    )

    assert result.returncode == 0, result.stderr
    assert any(f"openshell sandbox delete {canonical}" in call for call in calls)
    assert all(stale not in call for call in calls)


def test_rollback_does_not_fallback_past_a_corrupt_canonical_identity(
    tmp_path: Path,
) -> None:
    secret = "corrupt-canonical-runtime-secret-must-not-leak"
    result, calls, _ = _run_rollback(
        tmp_path,
        "systemd",
        action="withdraw",
        sandbox_identity="mac-openclaw-first\nmac-openclaw-second",
        runtime_env=(
            f"OPENCLAW_GATEWAY_TOKEN='{secret}'\n"
            "MAC_OPENCLAW_SANDBOX='mac-openclaw-runtime-fallback'\n"
        ),
    )

    assert result.returncode != 0
    assert "managed OpenClaw sandbox identity is malformed" in result.stderr
    assert calls == []
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_fleet_transaction_rollback_uses_private_runtime_sandbox_fallback(
    tmp_path: Path,
) -> None:
    sandbox = "mac-openclaw-fleet-runtime-fallback"
    result, calls, _ = _run_rollback(
        tmp_path,
        "systemd",
        fleet_transaction=True,
        sandbox_identity=None,
        runtime_env=f"MAC_OPENCLAW_SANDBOX='{sandbox}'\n",
    )

    assert result.returncode == 0, result.stderr
    assert any(f"openshell sandbox delete {sandbox}" in call for call in calls)
    assert not any("mac-hermes-gateway" in call for call in calls)
    assert "exact prior gateway restoration delegated" in result.stdout


@pytest.mark.parametrize(
    ("runtime_env_artifact", "runtime_env_mode"),
    (("file", 0o644), ("symlink", 0o600), ("directory", 0o700)),
)
def test_rollback_rejects_unsafe_runtime_identity_artifacts_without_disclosure(
    tmp_path: Path,
    runtime_env_artifact: str,
    runtime_env_mode: int,
) -> None:
    secret = "unsafe-runtime-secret-must-not-leak"
    result, calls, _ = _run_rollback(
        tmp_path,
        "systemd",
        action="withdraw",
        sandbox_identity=None,
        runtime_env=(
            f"OPENCLAW_GATEWAY_TOKEN='{secret}'\n"
            "MAC_OPENCLAW_SANDBOX='mac-openclaw-runtime-fallback'\n"
        ),
        runtime_env_mode=runtime_env_mode,
        runtime_env_artifact=runtime_env_artifact,
    )

    assert result.returncode != 0
    assert "runtime environment is not an owner-only regular file" in result.stderr
    assert calls == []
    assert secret not in result.stdout
    assert secret not in result.stderr


@pytest.mark.parametrize(
    ("sandbox_assignment", "expected_error"),
    (
        ("", "runtime sandbox identity is missing"),
        (
            "MAC_OPENCLAW_SANDBOX='mac-openclaw-one'\n"
            "MAC_OPENCLAW_SANDBOX='mac-openclaw-two'\n",
            "runtime sandbox identity is ambiguous",
        ),
        (
            "MAC_OPENCLAW_SANDBOX='mac-openclaw-unterminated\n",
            "runtime sandbox identity is malformed",
        ),
        (
            "MAC_OPENCLAW_SANDBOX=mac-openclaw-valid; touch forbidden\n",
            "runtime sandbox identity is ambiguous",
        ),
        (
            "MAC_OPENCLAW_SANDBOX='../../unmanaged'\n",
            "sandbox identity is unsafe",
        ),
    ),
)
def test_rollback_rejects_missing_ambiguous_or_malformed_runtime_identity(
    tmp_path: Path,
    sandbox_assignment: str,
    expected_error: str,
) -> None:
    secret = "malformed-runtime-secret-must-not-leak"
    result, calls, _ = _run_rollback(
        tmp_path,
        "systemd",
        action="withdraw",
        sandbox_identity=None,
        runtime_env=f"OPENCLAW_GATEWAY_TOKEN='{secret}'\n{sandbox_assignment}",
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert calls == []
    assert not (tmp_path / "runtime-env-was-sourced").exists()
    assert secret not in result.stdout
    assert secret not in result.stderr


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "supervisord"])
def test_fleet_transaction_rollback_withdraws_without_guessing_prior_gateway(
    tmp_path: Path, supervisor: str
) -> None:
    result, calls, openclaw_home = _run_rollback(
        tmp_path,
        supervisor,
        fleet_transaction=True,
    )

    assert result.returncode == 0, result.stderr
    assert not any(
        "mac-hermes-gateway" in call
        and ("enable" in call or "start" in call or "bootstrap" in call)
        for call in calls
    )
    assert not (openclaw_home / "service-advertisement.json").exists()
    assert not (openclaw_home / "verification-pending.json").exists()
    assert "exact prior gateway restoration delegated" in result.stdout
    assert "rollback complete" not in result.stdout


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "supervisord"])
def test_rollback_restores_hermes_only_after_supervisor_and_sandbox_absence(
    tmp_path: Path, supervisor: str
) -> None:
    result, calls, _ = _run_rollback(tmp_path, supervisor)

    assert result.returncode == 0, result.stderr
    hermes_start = next(
        index
        for index, call in enumerate(calls)
        if (
            "enable --now mac-hermes-gateway.service" in call
            or "bootstrap " in call
            or "supervisorctl start mac-hermes-gateway" in call
        )
    )
    sandbox_probes = [
        index for index, call in enumerate(calls) if "openshell sandbox get" in call
    ]
    assert sandbox_probes
    assert max(sandbox_probes) < hermes_start
    assert any("openshell sandbox delete" in call for call in calls)


@pytest.mark.parametrize(
    ("supervisor", "manager_call", "proof_call"),
    (
        (
            "systemd",
            "sudo -n systemctl enable --now mac-hermes-gateway.service",
            "systemctl show mac-hermes-gateway.service --no-pager -p LoadState "
            "-p ActiveState -p SubState -p MainPID -p UnitFileState",
        ),
        (
            "supervisord",
            "sudo -n supervisorctl start mac-hermes-gateway",
            "supervisorctl status mac-hermes-gateway",
        ),
    ),
)
def test_rollback_uses_bounded_exact_system_scope_and_proves_hermes_running(
    tmp_path: Path,
    supervisor: str,
    manager_call: str,
    proof_call: str,
) -> None:
    result, calls, _ = _run_rollback(tmp_path, supervisor)

    assert result.returncode == 0, result.stderr
    assert manager_call in calls
    assert proof_call in calls
    assert calls.index(manager_call) < calls.index(proof_call)


@pytest.mark.parametrize("supervisor", ["systemd", "supervisord"])
def test_rollback_propagates_bounded_hermes_start_failure(
    tmp_path: Path, supervisor: str
) -> None:
    result, calls, _ = _run_rollback(
        tmp_path, supervisor, "hermes-start-failure"
    )

    assert result.returncode == 72
    assert f"bounded {supervisor} Hermes restore failed (exit 72)" in result.stderr
    assert "rollback complete" not in result.stdout
    assert not any(
        "status mac-hermes-gateway" in call
        or ("show mac-hermes-gateway.service" in call and "SubState" in call)
        for call in calls
    )


@pytest.mark.parametrize(
    ("supervisor", "scenario", "expected_error"),
    (
        ("systemd", "hermes-stays-inactive", "positive main PID"),
        ("systemd", "hermes-bad-runtime-state", "exact running-state proof"),
        ("supervisord", "hermes-stays-inactive", "did not become active"),
        ("supervisord", "hermes-missing-pid", "lacks a positive pid"),
    ),
)
def test_rollback_rejects_incomplete_hermes_running_state_proof(
    tmp_path: Path,
    supervisor: str,
    scenario: str,
    expected_error: str,
) -> None:
    result, _calls, _ = _run_rollback(tmp_path, supervisor, scenario)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert "rollback complete" not in result.stdout


@pytest.mark.parametrize("supervisor", ["systemd", "supervisord"])
def test_rollback_bounds_hung_hermes_restore_command(
    tmp_path: Path, supervisor: str
) -> None:
    result, _calls, _ = _run_rollback(
        tmp_path, supervisor, "hermes-start-timeout"
    )

    assert result.returncode == 124
    assert "OpenClaw subprocess timed out" in result.stderr
    assert f"bounded {supervisor} Hermes restore failed (exit 124)" in result.stderr
    assert (tmp_path / "timeout-child.ready").read_text(encoding="utf-8") == "ready"
    with (tmp_path / "timeout-child.lock").open("a", encoding="utf-8") as stream:
        # This is a synchronization assertion, not a wall-clock assertion: the
        # bounded runner may return only after the entire child process group
        # has released its kernel-owned lock.
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_rollback_restore_has_no_unbounded_or_cross_scope_linux_fallback() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    restore = installer.split("restore_hermes_gateway() {", 1)[1].split(
        "\n}\n\nwithdraw()", 1
    )[0]

    assert "run_systemd_system_scope enable --now" in restore
    assert "run_supervisord_system_scope start" in restore
    assert "prove_systemd_service_running" in restore
    assert "supervisord_program_state" in restore
    assert "sudo systemctl" not in restore
    assert "sudo supervisorctl" not in restore
    assert "systemctl --user" not in restore
    assert "supervisorctl \"$@\" ||" not in restore


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "supervisord"])
def test_withdraw_removes_openclaw_without_starting_hermes(
    tmp_path: Path, supervisor: str
) -> None:
    result, calls, openclaw_home = _run_rollback(
        tmp_path, supervisor, action="withdraw"
    )

    assert result.returncode == 0, result.stderr
    if supervisor == "systemd":
        assert "systemctl disable --now mac-openclaw-gateway.service" in calls
    elif supervisor == "launchd":
        assert any(
            call.startswith("launchctl bootout gui/")
            and call.endswith("/com.mac.openclaw-gateway")
            for call in calls
        )
        assert any(
            call.startswith("launchctl disable gui/")
            and call.endswith("/com.mac.openclaw-gateway")
            for call in calls
        )
    else:
        assert "supervisorctl stop mac-openclaw-gateway" in calls

    delete_index = next(
        index
        for index, call in enumerate(calls)
        if "openshell sandbox delete mac-openclaw-rollback-contract" in call
    )
    assert any(
        index > delete_index
        and "openshell sandbox get mac-openclaw-rollback-contract" in call
        for index, call in enumerate(calls)
    )
    assert not any("mac-hermes-gateway" in call for call in calls)
    assert not (openclaw_home / "service-advertisement.json").exists()
    assert not (openclaw_home / "verification-pending.json").exists()
    assert "withdrawal complete" in result.stdout


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "supervisord"])
def test_rollback_propagates_supervisor_stop_failure_without_starting_hermes(
    tmp_path: Path, supervisor: str
) -> None:
    result, calls, _ = _run_rollback(
        tmp_path, supervisor, "supervisor-stop-failure"
    )

    assert result.returncode != 0
    assert not any(
        "mac-hermes-gateway" in call and ("enable" in call or "start" in call or "bootstrap" in call)
        for call in calls
    )
    assert not any("openshell" in call for call in calls)


@pytest.mark.parametrize(
    ("supervisor", "scenario", "expected_error"),
    (
        ("systemd", "supervisor-still-active", "absence is unproven"),
        ("supervisord", "sandbox-inspection-error", "could not inspect OpenShell sandbox"),
        ("systemd", "sandbox-delete-failure", "sandbox delete failed"),
        ("launchd", "sandbox-timeout", "OpenClaw subprocess timed out"),
    ),
)
def test_rollback_fails_closed_on_absence_proof_delete_or_inspection_failure(
    tmp_path: Path,
    supervisor: str,
    scenario: str,
    expected_error: str,
) -> None:
    result, calls, _ = _run_rollback(tmp_path, supervisor, scenario)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not any(
        "mac-hermes-gateway" in call and ("enable" in call or "start" in call or "bootstrap" in call)
        for call in calls
    )


def test_verify_channel_probe_has_monotonic_bounded_subprocess_deadline(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    mac_home = home / ".mac"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _seed_hermes_identity(home)
    env = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "MAC_HOME": str(mac_home),
        "MAC_SRC": str(ROOT),
        "MAC_OPENSHELL_BIN": "/usr/bin/true",
        "MAC_OPENCLAW_DRY_RUN": "1",
        "MAC_OPENCLAW_AGENT_ID": "agent_channel_timeout",
        "MAC_OPENCLAW_INSTANCE_ID": "instance_channel_timeout",
        "MAC_OPENCLAW_ROUTER_URL": "http://100.64.0.1:8789/v1",
        "MAC_OPENCLAW_ROUTER_API_KEY": "router-secret",
        "MAC_OPENCLAW_MODEL": "test/model",
        "MAC_OPENCLAW_FLEET_NAME": "mac",
    }
    subprocess.run(
        [str(INSTALLER), "prepare"],
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    tools = [
        "memory_search",
        "memory_get",
        "memory_store",
        "mac_memory_recall",
        "mac_memory_store",
        "mac_mood_current",
        "mac_mood_set",
        "mac_mood_clear",
        "mac_config_flag_list",
        "mac_config_flag_set",
        "mac_config_flag_clear",
        "mac_fleet_status",
        "mac_agent_send",
        "mac_agent_share",
        "mac_notify_human",
        "mac_fs_put",
        "mac_fs_get",
        "mac_directive_verify",
        "mac_agent_inbox",
        "mac_image_generate",
        "curiosity_candidate_submit",
        "curiosity_candidates_list",
        "curiosity_abuse_frame",
    ]
    plugin_payload = json.dumps(
        {
            "plugin": {
                "imported": True,
                "status": "loaded",
                "toolNames": tools,
                "hookNames": ["before_prompt_build"],
            }
        }
    )
    openshell = bin_dir / "openshell"
    openshell.write_text(
        f"""#!/bin/sh
case "$1:$2:$*" in
  sandbox:get:*) exit 0 ;;
  *"channels status"*) sleep 30 ;;
  *"plugins inspect"*) printf '%s\n' '{plugin_payload}' ;;
  *mac-verify-bash-contract*) exit 0 ;;
  *'/usr/local/bin/node'*) echo OPENCLAW_CONTROL_PROBE_OK ;;
  *'curiosity verify'*) echo '{{"valid": true}}' ;;
  *'curiosity abuse-frame'*) echo '{{"possible_false_equivalence": true}}' ;;
  sandbox:exec:*) echo '{{}}' ;;
  *) echo "unexpected openshell invocation: $*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    openshell.chmod(0o700)
    verify_env = {
        **env,
        "MAC_OPENSHELL_BIN": str(openshell),
        "MAC_OPENCLAW_SUBPROCESS_TIMEOUT_SECONDS": "1",
        "MAC_OPENCLAW_VERIFY_STARTUP_TIMEOUT": "1",
        "MAC_OPENCLAW_VERIFY_STARTUP_INTERVAL": "0",
    }

    result = subprocess.run(
        [str(INSTALLER), "verify"],
        env=verify_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=6,
    )

    assert result.returncode != 0
    assert "gateway/channel probes did not become healthy within 1s" in result.stderr
    assert not (mac_home / "openclaw" / "verification-pending.json").exists()
    installer_text = INSTALLER.read_text(encoding="utf-8")
    assert "$SECONDS" not in installer_text
    assert " SECONDS +" not in installer_text
