"""A fleet's own hub cannot be required to answer on its hub endpoint before the
deploy that starts the hub service has run.

The gate that used to enforce that made a first self-hosted hub deploy
impossible: ``classify_network_prerequisites`` dialled ``http://<hub>:8789``
as a *precondition* of the operation responsible for making it reachable, and
``provider=none`` has no repair action, so the only advice the failure could
offer ("run --prepare-network-prerequisites") led to the same wall.

These tests pin the narrow exemption that resolves it -- hub agent, fresh node,
route deferred and then re-proved -- and, just as importantly, pin that it stays
narrow: a worker, a redeployed hub, and a node whose state could not be
classified are all still refused.
"""

import os
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"

HARNESS_FUNCTIONS = (
    "probe_remote_first_deploy_state",
    "hub_route_prerequisite_is_deferrable",
    "classify_network_prerequisites",
    "reprove_deferred_hub_route",
    "prepare_network_prerequisites",
)


def deploy_text():
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def extract_function(name):
    """Slice one top-level ``name() { ... }`` block out of the deploy script."""
    text = deploy_text()
    opening = "\n%s() {\n" % name
    start = text.index(opening)
    end = text.index("\n}\n", start)
    return text[start + 1 : end + 2]


def spec(agent, hub_url="http://hazel.example:8789", provider="none", install="auto"):
    fields = [""] * 56
    fields[0] = agent
    fields[7] = hub_url
    fields[23] = "watership-down"
    fields[31] = provider
    fields[32] = install
    fields[34] = "MAC_DEPLOY_TAILSCALE_AUTH_KEY"
    return "|".join(fields)


def run_harness(tmp_path, body, first_deploy_mode, *, stubs="", specs=None, env=None):
    """Execute the real deploy-script functions against stubbed SSH and probes.

    Only the transport is faked. ``probe_remote_first_deploy_state`` runs for
    real against a fake ``ssh`` so its fail-closed classification is exercised
    rather than described.
    """
    case = tmp_path / first_deploy_mode / str(abs(hash(body)) % 100000)
    fake_bin = case / "bin"
    fake_bin.mkdir(parents=True)
    mode_file = case / "mode"
    mode_file.write_text(first_deploy_mode + "\n", encoding="utf-8")

    ssh = fake_bin / "ssh"
    ssh.write_text(
        """#!/bin/sh
IFS= read -r mode < "$FAKE_SSH_MODE"
case "$mode" in
  fresh) echo fresh ;;
  deployed) echo deployed ;;
  garbage) echo 'maybe?' ;;
  multiline) echo fresh; echo deployed ;;
  empty) : ;;
  transport) echo 'ssh: connect to host port 22: Connection refused' >&2; exit 255 ;;
  *) echo "unhandled fake ssh mode: $mode" >&2; exit 2 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    ssh.chmod(0o755)

    specs_file = case / "specs"
    specs_file.write_text(
        "".join(line + "\n" for line in (specs or [spec("hazel")])), encoding="utf-8"
    )

    script = case / "harness.sh"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'PATH="%s:$PATH"' % fake_bin,
                "HUB_SELECTOR=watership-down",
                'DEFERRED_HUB_ROUTE_AGENT=""',
                'LAST_FIRST_DEPLOY_STATE="unknown"',
                'shell_quote() { printf "\'%s\'" "$1"; }',
                # NUL-delimited, target last -- the contract the real helper honours.
                "ssh_target_args() { printf '%s\\0' -o BatchMode=yes \"user@$1\"; }",
                "",
                *(extract_function(name) for name in HARNESS_FUNCTIONS),
                "",
                stubs,
                "",
                "SPECS=%s" % shlex.quote(str(specs_file)),
                body,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    environment = dict(os.environ)
    environment["FAKE_SSH_MODE"] = str(mode_file)
    environment.update(env or {})
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(case),
    )


UNREACHABLE_HUB = "probe_remote_hub_tcp() { return 1; }"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("fresh", "fresh"),
        ("deployed", "deployed"),
        # Everything that is not an unambiguous answer is "may already be
        # deployed", never "fresh".
        ("transport", "unknown"),
        ("garbage", "unknown"),
        ("multiline", "unknown"),
        ("empty", "unknown"),
    ],
)
def test_first_deploy_state_is_classified_fail_closed(tmp_path, mode, expected):
    result = run_harness(tmp_path, "probe_remote_first_deploy_state hazel", mode)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_first_deploy_marker_is_the_installer_written_mac_env():
    probe = extract_function("probe_remote_first_deploy_state")

    assert "$HOME/.mac/mac.env" in probe
    assert "echo deployed" in probe and "echo fresh" in probe


@pytest.mark.parametrize(
    ("agent", "hub_agent", "mode", "deferrable"),
    [
        ("hazel", "hazel", "fresh", True),
        # A worker must still reach a hub that is supposed to already be up.
        ("fiver", "hazel", "fresh", False),
        # A hub that has been deployed before owes a live endpoint.
        ("hazel", "hazel", "deployed", False),
        ("hazel", "hazel", "transport", False),
        # No hub agent named: nothing is exempt.
        ("hazel", "", "fresh", False),
    ],
)
def test_only_a_fresh_hub_agent_may_defer_its_own_route(
    tmp_path, agent, hub_agent, mode, deferrable
):
    result = run_harness(
        tmp_path,
        "if hub_route_prerequisite_is_deferrable %s %s; then echo defer; else echo refuse; fi"
        % (shlex.quote(agent), shlex.quote(hub_agent)),
        mode,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("defer" if deferrable else "refuse")


def test_fresh_hub_passes_its_own_unreachable_route_gate(tmp_path):
    result = run_harness(
        tmp_path,
        'classify_network_prerequisites "$SPECS" hazel; echo "deferred=$DEFERRED_HUB_ROUTE_AGENT"',
        "fresh",
        stubs=UNREACHABLE_HUB,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "hub route prerequisite deferred to this first hub deploy" in result.stdout
    assert "deferred=hazel" in result.stdout


@pytest.mark.parametrize("mode", ["deployed", "transport"])
def test_gate_still_refuses_a_hub_that_is_not_on_a_fresh_node(tmp_path, mode):
    result = run_harness(
        tmp_path,
        'if classify_network_prerequisites "$SPECS" hazel; then echo passed; else'
        ' echo "refused deferred=$DEFERRED_HUB_ROUTE_AGENT"; fi',
        mode,
        stubs=UNREACHABLE_HUB,
    )

    assert result.returncode == 0, result.stderr
    assert "refused deferred=" in result.stdout
    assert "passed" not in result.stdout
    assert "hub route prerequisite is unreachable" in result.stderr
    assert (
        "first-deploy state: %s" % ("deployed" if mode == "deployed" else "unknown")
        in result.stderr
    )


def test_gate_still_refuses_an_unreachable_worker_on_a_fresh_node(tmp_path):
    result = run_harness(
        tmp_path,
        'if classify_network_prerequisites "$SPECS" hazel; then echo passed; else echo refused; fi',
        "fresh",
        stubs=UNREACHABLE_HUB,
        specs=[spec("fiver")],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("refused")
    assert "fiver: hub route prerequisite is unreachable" in result.stderr


def test_one_unreachable_worker_clears_the_hub_deferral(tmp_path):
    """A refused cohort must not leave a deferral armed for the next phase."""
    result = run_harness(
        tmp_path,
        'if classify_network_prerequisites "$SPECS" hazel; then echo passed; else'
        ' echo "refused deferred=[$DEFERRED_HUB_ROUTE_AGENT]"; fi',
        "fresh",
        stubs=UNREACHABLE_HUB,
        specs=[spec("hazel"), spec("fiver")],
    )

    assert result.returncode == 0, result.stderr
    assert "refused deferred=[]" in result.stdout


def test_reachable_route_is_never_deferred(tmp_path):
    result = run_harness(
        tmp_path,
        'classify_network_prerequisites "$SPECS" hazel;'
        ' echo "deferred=[$DEFERRED_HUB_ROUTE_AGENT]"',
        "fresh",
        stubs="probe_remote_hub_tcp() { return 0; }",
    )

    assert result.returncode == 0, result.stderr
    assert "hub route prerequisite ready" in result.stdout
    assert "deferred=[]" in result.stdout
    assert "deferred to this first hub deploy" not in result.stdout


def test_deferred_route_is_re_proved_after_the_hub_service_starts(tmp_path):
    """Deferred, not deleted: the promise is collected once the hub is running."""
    result = run_harness(
        tmp_path,
        "DEFERRED_HUB_ROUTE_AGENT=hazel;"
        ' reprove_deferred_hub_route "$SPECS";'
        ' echo "attempts=$(cat attempts)";'
        ' echo "deferred=[$DEFERRED_HUB_ROUTE_AGENT]"',
        "fresh",
        stubs=(
            "echo 0 > attempts\n"
            "probe_remote_hub_tcp() {\n"
            "  local n; IFS= read -r n < attempts; n=$((n + 1));\n"
            '  printf "%s\\n" "$n" > attempts\n'
            '  [ "$n" -ge 3 ]\n'
            "}"
        ),
        env={
            "MAC_DEPLOY_HUB_ROUTE_PROOF_ATTEMPTS": "6",
            "MAC_DEPLOY_HUB_ROUTE_PROOF_INTERVAL_SECONDS": "0",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "hub route prerequisite proved after first hub deploy" in result.stdout
    assert "attempts=3" in result.stdout
    assert "deferred=[]" in result.stdout


def test_a_hub_that_never_answers_is_a_failed_deploy(tmp_path):
    result = run_harness(
        tmp_path,
        "DEFERRED_HUB_ROUTE_AGENT=hazel;"
        ' if reprove_deferred_hub_route "$SPECS"; then echo passed; else echo failed; fi',
        "fresh",
        stubs=UNREACHABLE_HUB,
        env={
            "MAC_DEPLOY_HUB_ROUTE_PROOF_ATTEMPTS": "2",
            "MAC_DEPLOY_HUB_ROUTE_PROOF_INTERVAL_SECONDS": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("failed")
    assert "deferred hub route is still unreachable" in result.stderr


def test_re_proof_is_a_no_op_when_nothing_was_deferred(tmp_path):
    result = run_harness(
        tmp_path,
        'reprove_deferred_hub_route "$SPECS"; echo done',
        "fresh",
        stubs='probe_remote_hub_tcp() { echo "probe must not run" >&2; return 1; }',
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"
    assert "probe must not run" not in result.stderr


def test_preparation_reports_nothing_to_repair_for_a_fresh_hub(tmp_path):
    """provider=none on a fresh hub is complete, not unrepairable."""
    result = run_harness(
        tmp_path,
        'prepare_network_prerequisites "$SPECS" hazel',
        "fresh",
        stubs=UNREACHABLE_HUB
        + '\nprepare_remote_tailscale_prerequisite() { echo "must not repair" >&2; return 1; }',
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "needs no repair" in result.stderr
    assert "cannot repair provider=" not in result.stderr
    assert "must not repair" not in result.stderr


def test_unrepairable_verdict_names_the_classified_cause(tmp_path):
    result = run_harness(
        tmp_path,
        'if prepare_network_prerequisites "$SPECS" hazel; then echo passed; else echo blocked; fi',
        "deployed",
        stubs=UNREACHABLE_HUB,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("blocked")
    assert "no repair action for provider=none, install=auto" in result.stderr
    assert "first-deploy state: deployed" in result.stderr
    assert "refusing every network mutation" in result.stderr


def test_phase1_route_tunnel_check_honours_the_single_deferral_verdict():
    """The phase-1 bundle is the same circularity one phase later.

    It runs "before hub or service mutation" and dials the hub URL, so a first
    hub deploy would fail there even after the network gate let it through. It
    must consume the gate's verdict rather than re-deciding it.
    """
    deploy = deploy_text()
    builder = deploy.split("prepare_remote_prerequisite_bundle() {", 1)[1].split(
        "\n}\n\nprerequisite_bundle_digests", 1
    )[0]

    assert "route_hub_required=1" in builder
    assert '[ "$agent" = "$DEFERRED_HUB_ROUTE_AGENT" ]' in builder
    assert "route_hub_required=0" in builder
    assert 'MAC_PREREQ_ROUTE_HUB_REQUIRED=$(shell_quote "$route_hub_required")' in builder
    assert 'os.environ["MAC_PREREQ_ROUTE_HUB_REQUIRED"]' in builder
    # Still a real TCP proof for every node the gate did not exempt.
    assert "service_check(" in builder
    assert '"route-hub",' in builder
    assert 'os.environ["MAC_PREREQ_HUB_URL"]' in builder


def test_deferral_is_armed_by_the_gate_and_defaults_to_disarmed():
    deploy = deploy_text()
    preamble = deploy.split("\nmain() {", 1)[0]
    classify = extract_function("classify_network_prerequisites")

    assert 'DEFERRED_HUB_ROUTE_AGENT=""' in preamble
    # The gate owns both arming and disarming.
    assert classify.index('DEFERRED_HUB_ROUTE_AGENT=""') < classify.index(
        'DEFERRED_HUB_ROUTE_AGENT="$agent"'
    )


def test_both_gates_receive_the_hub_agent_and_the_route_is_re_proved_after_cutover():
    deploy = deploy_text()
    main = deploy.split("\nmain() {", 1)[1]

    assert 'classify_network_prerequisites "$selected_specs_file" "$hub_agent"' in main
    assert 'prepare_network_prerequisites "$selected_specs_file" "$hub_agent"' in main
    assert main.index("run_typed_cohort ") < main.index(
        'reprove_deferred_hub_route "$selected_specs_file"'
    )


def test_quickdemo_documents_the_tailscale_enrollment_credential_prerequisite():
    quickdemo = (ROOT / "QUICKDEMO.md").read_text(encoding="utf-8")
    gaps = quickdemo.split("## Known gaps in this script", 1)[1]

    assert "MAC_DEPLOY_TAILSCALE_AUTH_KEY__WATERSHIP_DOWN" in gaps
    assert "4. **" in gaps
    assert "tailscale ip -4" in gaps
