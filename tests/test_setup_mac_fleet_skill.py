"""`setup-mac-fleet` is the skill whose rot costs most.

Its reader is, by definition, someone standing up a fleet for the first time --
who therefore cannot tell that a step is wrong. It went 330 commits without a
test while `mac memory` moved under `admin`, TokenHub was retired in favour of
the in-mac router, and two coding agents joined the router's priority list.
Every one of those invalidated advice while every path in the file still
resolved, so a link check would have stayed green throughout.

So this file asserts the CLAIMS, not the syntax: that the deploy verbs exist
with the flags the skill tells the reader to type, that the wizard still asks
what the skill says it asks, that keys still land where the skill says they
land, and that the facts the skill states about supervisor selection and the
sandbox bill of materials are the facts the code implements.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.skill_claims import (
    assert_commands_exist,
    assert_flags_exist,
    assert_paths_exist,
)
from tests.test_mac_cli_skill import _command_tree, _options_for

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "setup-mac-fleet" / "SKILL.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert SKILL.is_file(), "the fleet setup skill is missing"
    return SKILL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The commands, flags and files it tells the reader to use
# ---------------------------------------------------------------------------


def test_every_command_the_skill_names_exists(text):
    assert_commands_exist(text, _command_tree())


def test_every_flag_the_skill_names_exists(text):
    assert_flags_exist(text, _options_for)


def test_every_repository_path_the_skill_names_exists(text):
    assert_paths_exist(text)


def test_the_setup_entrypoint_still_consumes_the_documented_switches():
    """`--configure-only`, `--no-deploy`, `--deploy`, `--hub`, `--new-hub`.

    The skill promises specific behaviour for each: two mean "configure and
    stop", one means nothing at all, and the last two skip the wizard entirely
    and go straight to the deploy wrapper. Those are behaviours, not the mere
    presence of a string, so they are exercised.
    """

    import importlib.util

    spec = importlib.util.spec_from_file_location("_mac_setup_entrypoint", ROOT / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for stop in ("--configure-only", "--no-deploy"):
        forwarded, config_only, _dry, _direct = module.parse_setup_args([stop])
        assert config_only, "%s must configure without deploying" % stop
        assert stop not in forwarded, "%s is consumed, not forwarded" % stop

    forwarded, config_only, _dry, _direct = module.parse_setup_args(["--deploy"])
    assert not config_only and forwarded == [], "--deploy must remain a no-op"

    for direct in ("--hub", "--new-hub"):
        _fwd, _cfg, _dry, deploy_direct = module.parse_setup_args([direct, "node"])
        assert deploy_direct, "%s must short-circuit to the deploy wrapper" % direct

    # ...and configure-only wins over it, which is what makes step 4 safe advice.
    _fwd, config_only, _dry, deploy_direct = module.parse_setup_args(
        ["--hub", "node", "--configure-only"]
    )
    assert config_only and deploy_direct


def test_the_wizard_accepts_the_flags_the_skill_passes_it():
    """`scripts/setup-fleet.py` is invoked directly by the spec workflow."""

    help_text = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup-fleet.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    ).stdout
    for flag in (
        "--fleets-config",
        "--env-file",
        "--list-samples",
        "--init-from",
        "--name",
        "--spec",
        "--force",
        "--validate-only",
        "--new-hub",
        "--target",
        "--supervisor",
    ):
        assert flag in help_text, "setup-fleet.py no longer accepts %s" % flag


def test_the_deploy_script_accepts_the_documented_switches():
    script = (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
    for flag in ("--hub", "--new-hub", "--target", "--fleets-config", "--supervisor"):
        assert "%s)" % flag in script, "deploy-mac-fleet.sh no longer handles %s" % flag


def test_make_deploy_still_takes_HUB():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ndeploy:" in makefile
    assert "make deploy HUB=<hub-node>" in makefile


# ---------------------------------------------------------------------------
# Where provider keys go. This is the claim that had been wrong the longest.
# ---------------------------------------------------------------------------


def test_tokenhub_is_retired_and_the_skill_does_not_resurrect_it(text):
    """The skill used to say TokenHub absorbs the keys on first deploy and
    that they are written to `~/.tokenhub/credentials` by
    `seed_or_merge_credentials()`. TokenHub is retired and that function is
    gone, so the instruction sent an operator looking for a file nothing
    writes."""

    assert "TokenHub is retired" in text, (
        "the retirement is a documented trap, not a silent removal: an operator "
        "who read the old advice must be told where to stop looking"
    )
    assert "seed_or_merge_credentials" not in text
    hits = subprocess.run(
        ["git", "grep", "-l", "seed_or_merge_credentials", "--", "src", "deploy", "scripts"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert hits.stdout.strip() == "", (
        "seed_or_merge_credentials is back; the skill's router advice needs "
        "rechecking: %s" % hits.stdout
    )


def test_the_wizard_writes_the_router_env_the_skill_documents():
    wizard = (ROOT / "scripts" / "setup-fleet.py").read_text(encoding="utf-8")
    for name in ("MAC_ROUTER_BACKEND", "MAC_ROUTER_PROVIDERS", "MAC_ROUTER_DEFAULT_MODEL"):
        assert name in wizard, "the wizard no longer mentions %s" % name
    assert '"inproc"' in wizard, "the hub-side router backend is no longer inproc"


def test_the_skill_names_the_router_not_the_retired_service(text):
    assert "MAC_ROUTER_BACKEND" in text and "MAC_ROUTER_PROVIDERS" in text


# ---------------------------------------------------------------------------
# The wizard's shape: the two opening questions and the provider loop
# ---------------------------------------------------------------------------


def test_the_two_opening_questions_are_still_asked():
    wizard = (ROOT / "scripts" / "setup-fleet.py").read_text(encoding="utf-8")
    assert "Are you running this script on the machine being configured" in wizard
    assert '"Setting up a hub or a worker?"' in wizard
    assert 'choices=["hub", "worker"]' in wizard, "the role question lost its choices"


def test_the_provider_loop_still_refuses_to_exit_empty():
    """The skill promises the loop does not exit until a provider is entered.
    If that stops being true a fleet can be generated with no routable model."""

    wizard = (ROOT / "scripts" / "setup-fleet.py").read_text(encoding="utf-8")
    assert "At least one provider is required." in wizard


def test_the_skill_offers_the_providers_the_wizard_accepts(text):
    """The wizard derives its menu from mac.providers, so that is what the
    skill must list. Naming a provider the wizard rejects wastes the one
    question the loop will not let the operator skip."""

    from mac.providers import ROUTER_PROVIDERS

    offered = [p.id for p in ROUTER_PROVIDERS]
    assert offered == ["nvidia", "openai", "anthropic", "perplexity"]
    assert " / ".join(offered) in text, (
        "the skill must list the router's providers, in registry order"
    )


# ---------------------------------------------------------------------------
# Supervisor selection: the skill states an order, so the order is pinned
# ---------------------------------------------------------------------------


def test_supervisor_autodetect_lives_where_the_skill_says_and_ranks_as_it_says():
    """The skill used to attribute this to deploy-mac-fleet.sh, where no such
    logic exists. It is `detect_supervisor()` on the node, and the order it
    tries matters: a container with systemd absent must reach supervisord."""

    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    assert "detect_supervisor() {" in script
    body = script.split("detect_supervisor() {", 1)[1].split("\nrun_supervisorctl", 1)[0]
    fallback = body.split('auto|""', 1)[1]
    launchd = fallback.index("launchd")
    systemd = fallback.index("systemd")
    supervisord = fallback.index("supervisorctl")
    assert launchd < systemd < supervisord, (
        "the skill documents darwin/launchd -> linux+systemd -> supervisord"
    )
    assert "/run/systemd/system" in fallback, (
        "systemd detection no longer requires an ACTIVE systemd, so the skill's "
        "'systemctl AND /run/systemd/system' claim is stale"
    )
    assert "MAC_DEPLOY_SUPERVISOR" in body, (
        "the skill tells the reader to set MAC_DEPLOY_SUPERVISOR when detection fails"
    )


# ---------------------------------------------------------------------------
# Coding agents, the sandbox BOM, and the per-worker dispatch gate
# ---------------------------------------------------------------------------


def test_the_skill_lists_the_router_priority_in_order(text):
    from mac.coding_agent import AGENT_PRIORITY

    assert AGENT_PRIORITY == ("opencode", "pi", "claude", "codex", "cursor")
    listed = "**" + ", ".join(AGENT_PRIORITY) + "**"
    assert listed in text, "the skill must list AGENT_PRIORITY in router order: %s" % listed


def test_every_routed_agent_has_a_sandbox_binary():
    """The skill tells the reader adding a router agent changes the image BOM.
    That only holds while the BOM is derived from AGENT_PRIORITY."""

    from mac.coding_agent import AGENT_PRIORITY
    from mac.sandbox_bom import CODING_AGENT_SANDBOX_BINARY, MAC_CORE_COMMANDS

    assert set(AGENT_PRIORITY) <= set(CODING_AGENT_SANDBOX_BINARY)
    assert CODING_AGENT_SANDBOX_BINARY["cursor"] == "cursor-agent", (
        "the skill calls out cursor as the one binary that differs from its key"
    )
    for agent in AGENT_PRIORITY:
        assert CODING_AGENT_SANDBOX_BINARY[agent] in MAC_CORE_COMMANDS


def test_allowed_projects_is_still_a_dispatch_gate_with_a_named_diagnosis():
    """`mac task why-unclaimed` is the skill's remedy for an idle worker. It is
    only a remedy while it names this gate."""

    from mac.cli import _WHY_UNCLAIMED_HINTS

    hint = _WHY_UNCLAIMED_HINTS["agent_project_not_allowed"]
    assert "allowed_projects" in hint
    deploy_env = (ROOT / "src" / "mac" / "deploy_env.py").read_text(encoding="utf-8")
    assert "MAC_WORKER_ALLOWED_PROJECTS" in deploy_env
    fleet_setup = (ROOT / "src" / "mac" / "fleet_setup.py").read_text(encoding="utf-8")
    assert "allowed_projects" in fleet_setup


# ---------------------------------------------------------------------------
# The worked container example
# ---------------------------------------------------------------------------


def test_the_gke_sample_still_has_the_shape_the_skill_describes():
    sample = (ROOT / "deploy" / "fleet" / "samples" / "gke.fleet.yaml").read_text(
        encoding="utf-8"
    )
    for node in ("gke-hub", "gke-worker-1", "gke-worker-2"):
        assert node in sample, "the skill names %s in the GKE sample" % node
    assert "ssh_jump" in sample, "the bastion ProxyJump is the point of this sample"
    assert "sample: true" in sample, (
        "the skill tells the reader the shipped samples are refused by the deploy"
    )
