"""An operator must be able to declare a host's container runtimes.

``discover_working_runtimes`` enumerates every docker/podman binary it can find
and requires ``info`` to return 0 on each. The principle is right and is kept:
an installed-but-uninspectable daemon can own restart-managed containers that
reappear later, so it is not an absence proof.

What it cannot do is tell a runtime that is STOPPED AND HOLDING CONTAINERS from
one that has never been started. On the hub, 2026-08-05:

    docker info  -> rc=0    (the real runtime; OpenShell builds through it)
    podman info  -> rc=125  (unable to connect to Podman socket)
    podman machine list -> created 3 months ago, LAST UP: Never
    podman ps -a (once started) -> 0 containers

A vestigial podman made phase1-prepare fail and the hub undeployable. The
workaround was ``podman machine start``, and the machine was LEFT RUNNING
because stopping it re-breaks the next deploy: a 2GiB VM with zero containers,
running permanently to satisfy a probe (task_0b164136).

WHY NOT THE FIX THE TICKET PREFERRED. Its first option was to treat a machine
whose LastUp is "Never" as a positive absence proof. Measured on that same host
2026-08-07, with the machine RUNNING at that moment:

    State  = running
    LastUp = 0001-01-01T00:00:00Z

The applehv provider never maintains LastUp, so the Go zero time means nothing:
"never up", "up right now" and "stopped, holding containers" are
indistinguishable through it. Implementing that option would manufacture the
false absence proof the guard exists to prevent, on a currently live runtime.

So the gate keeps failing closed on ambiguity, and the ambiguity is resolved by
an operator DECLARING which runtimes the host uses -- authority the gate does
not have and cannot infer. The env plumbing already existed
(``MAC_DEPLOY_DAEMON_RUNTIME_PATHS`` / ``_CONFIGURED``); nothing on the fleet
deploy path ever fed it.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"
#: The installer's own interpreter. Its shebang is /bin/bash, which on the
#: macOS fleet hosts is bash 3.2 -- and 3.2 differs from 5.x in exactly the
#: way this code can trip over (empty-array expansion under `set -u`). Running
#: these through whatever "bash" PATH resolves to would test a shell the
#: installer never runs under; on this machine that is 5.2 from homebrew, and
#: it silently accepts the form 3.2 aborts on.
INSTALLER_SHELL = "/bin/bash"
FLEET_DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"


# --------------------------------------------------------------------------
# The declaration: env -> CONTAINER_RUNTIME_PATHS -> the gate's env
# --------------------------------------------------------------------------


def _run_gate_env(env: Dict[str, str], *, preset: str = "") -> Dict[str, str]:
    """Run the installer's declaration block and report what the gate would see.

    The block is executed as shipped rather than reimplemented, so a change to
    the installer is visible here. Only the two runtime-path variables are
    reported; the gate's other inputs are not what this file is about.
    """
    block = INSTALLER.read_text(encoding="utf-8")
    start = block.index("  # An operator may declare the host's real container runtimes")
    end = block.index("  # The deploy process carries repository, hub,", start)
    # Run it inside a function under `set -u`, because that is where it lives
    # (daemon_resource_quiescence_gate) and both matter: the block uses `local`,
    # which is an error at top level, and `set -u` is what turns an empty-array
    # expansion into an aborted deploy on the bash macOS ships.
    script = "\n".join(
        [
            "set -u",
            "gate() {",
            "  local runtime_path='' runtime_paths='' runtime_paths_configured=0",
            "  local runtime_paths_declaration=''",
            "  " + preset if preset else "  :",
            block[start:end],
            '  printf "CONFIGURED=%s\\n" "$runtime_paths_configured"',
            '  printf "PATHS<<%s>>\\n" "$runtime_paths"',
            "}",
            "gate",
        ]
    )
    completed = subprocess.run(
        [INSTALLER_SHELL, "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    configured = re.search(r"CONFIGURED=(\d+)", completed.stdout).group(1)
    paths = re.search(r"PATHS<<(.*?)>>", completed.stdout, re.S).group(1)
    return {
        "configured": configured,
        "paths": [line for line in paths.splitlines() if line.strip()],
    }


def test_no_declaration_leaves_discovery_in_charge():
    """The default must not change: unset means discover, as it always did."""
    result = _run_gate_env({})

    assert result["configured"] == "0"
    assert result["paths"] == []


def test_a_declared_runtime_reaches_the_gate():
    """The hub's case: this host uses docker, do not go looking for podman."""
    result = _run_gate_env(
        {"MAC_DEPLOY_CONTAINER_RUNTIME_PATHS": "/Applications/Docker.app/Contents/Resources/bin/docker"}
    )

    assert result["configured"] == "1", (
        "the declaration did not switch the gate into configured-only mode, so "
        "discovery still runs and a vestigial podman still blocks the deploy"
    )
    assert result["paths"] == ["/Applications/Docker.app/Contents/Resources/bin/docker"]


def test_several_runtimes_may_be_declared():
    result = _run_gate_env(
        {"MAC_DEPLOY_CONTAINER_RUNTIME_PATHS": "/usr/bin/docker:/usr/bin/podman"}
    )

    assert result["paths"] == ["/usr/bin/docker", "/usr/bin/podman"]


def test_newlines_are_accepted_as_well_as_colons():
    result = _run_gate_env(
        {"MAC_DEPLOY_CONTAINER_RUNTIME_PATHS": "/usr/bin/docker\n/usr/bin/podman"}
    )

    assert result["paths"] == ["/usr/bin/docker", "/usr/bin/podman"]


def test_blank_and_whitespace_entries_are_dropped():
    """A trailing colon or a stray indent must not become an empty path.

    An empty entry would reach discover_working_runtimes, which classifies by
    filename and raises "configured container runtime kind is unknown" -- a
    deploy failure caused purely by formatting.
    """
    result = _run_gate_env(
        {"MAC_DEPLOY_CONTAINER_RUNTIME_PATHS": "  /usr/bin/docker  ::\n\n   \n/usr/bin/podman:"}
    )

    assert result["paths"] == ["/usr/bin/docker", "/usr/bin/podman"]


def test_an_empty_declaration_is_not_a_declaration():
    """Empty must mean "I said nothing", not "I declare there are none".

    The second reading would skip discovery entirely and certify absence on a
    host nobody actually inspected.
    """
    result = _run_gate_env({"MAC_DEPLOY_CONTAINER_RUNTIME_PATHS": "   "})

    assert result["configured"] == "0"
    assert result["paths"] == []


def test_a_caller_supplied_array_still_wins():
    """install-qdrant-service.sh sets the array directly; do not override it."""
    result = _run_gate_env(
        {"MAC_DEPLOY_CONTAINER_RUNTIME_PATHS": "/env/docker"},
        preset='declare -a CONTAINER_RUNTIME_PATHS=("/caller/docker")',
    )

    assert result["paths"] == ["/caller/docker"]


def test_a_caller_supplied_empty_array_does_not_abort_the_deploy():
    """install-qdrant-service.sh:148 does exactly ``CONTAINER_RUNTIME_PATHS=()``.

    Under ``set -u``, bash 3.2 -- the /bin/bash this script's shebang selects,
    and what the macOS fleet hosts run -- treats expanding an empty array as an
    unbound variable and kills the script. bash 5.x does not, so this only
    reproduces on the shell that matters:

        $ /bin/bash -c 'set -u; f(){ local -a a=(); for x in "${a[@]}"; do :; done; }; f'
        /bin/bash: a[@]: unbound variable

    On a Linux runner /bin/bash is 5.x and this passes either way, which is why
    ``test_the_empty_array_expansion_is_guarded_in_source`` asserts the form
    directly as well.
    """
    result = _run_gate_env({}, preset="declare -a CONTAINER_RUNTIME_PATHS=()")

    assert result["configured"] == "1", "an explicitly empty declaration is still a declaration"
    assert result["paths"] == []


def test_the_empty_array_expansion_is_guarded_in_source():
    """The portable half of the assertion above.

    Asserted on the text so it holds on runners whose /bin/bash is 5.x and
    cannot reproduce the abort.
    """
    text = INSTALLER.read_text(encoding="utf-8")

    assert 'in ${CONTAINER_RUNTIME_PATHS[@]+"${CONTAINER_RUNTIME_PATHS[@]}"}' in text, (
        "the empty-array guard is gone; on bash 3.2 under set -u an empty "
        "CONTAINER_RUNTIME_PATHS aborts the deploy"
    )


def test_the_variable_survives_the_env_file_source():
    """Without this the deploy's env file silently overwrites the operator.

    deploy-mac-fleet.sh sources $HOME/.mac/mac.env over the caller's
    environment; only names in _PRECEDENCE_VARS are restored afterwards.
    """
    text = FLEET_DEPLOY.read_text(encoding="utf-8")
    precedence = text[text.index("_PRECEDENCE_VARS=(") : text.index(")", text.index("_PRECEDENCE_VARS=("))]

    assert "MAC_DEPLOY_CONTAINER_RUNTIME_PATHS" in precedence


# --------------------------------------------------------------------------
# The diagnostic: which runtime, not just "a" runtime
# --------------------------------------------------------------------------


def _load_gate_python() -> Dict[str, Any]:
    """Execute the installer's embedded gate helpers as shipped."""
    text = INSTALLER.read_text(encoding="utf-8")
    start = text.index("def describe_runtime(runtime):")
    end = text.index("def docker_endpoints(path, clean_env, ambient):", start)
    namespace: Dict[str, Any] = {"json": json, "re": re}
    exec(compile(text[start:end], str(INSTALLER), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture(scope="module")
def gate():
    ns = _load_gate_python()
    assert "describe_runtime" in ns and "first_line_hint" in ns
    return ns


class _Result:
    def __init__(self, stderr="", stdout=""):
        self.stderr = stderr
        self.stdout = stdout


def test_the_description_names_the_binary_and_the_endpoint(gate):
    """The old message named neither, and that was most of the diagnosis."""
    described = gate["describe_runtime"](
        {
            "kind": "podman",
            "path": "/opt/homebrew/bin/podman",
            "endpoint": "podman-machine://podman-machine-default@127.0.0.1:55580",
        }
    )

    assert "podman" in described
    assert "/opt/homebrew/bin/podman" in described
    assert "127.0.0.1:55580" in described


def test_a_malformed_runtime_does_not_break_the_failure_it_describes(gate):
    """A diagnostic must never replace the fault it is reporting."""
    assert gate["describe_runtime"](None) == "unidentified container runtime"


def test_the_hint_carries_the_runtimes_own_complaint(gate):
    """"unable to connect to Podman socket" is the entire answer on that host."""
    hint = gate["first_line_hint"](_Result(stderr="Cannot connect to Podman socket\ntrace..."))

    assert hint == ": Cannot connect to Podman socket"


def test_the_hint_falls_back_to_stdout(gate):
    assert gate["first_line_hint"](_Result(stdout="something on stdout")) == ": something on stdout"


def test_the_hint_is_empty_when_the_runtime_said_nothing(gate):
    assert gate["first_line_hint"](_Result()) == ""


def test_the_hint_is_bounded_and_single_line(gate):
    """This lands in a JSON failure record and in log lines.

    Unbounded it is a way to bloat a failure record; multi-line it is a way to
    forge structure in whatever reads the log.
    """
    hint = gate["first_line_hint"](_Result(stderr="x" * 5000 + "\nsecond line"))

    assert len(hint) <= 202
    assert "\n" not in hint


def test_the_unreadable_failure_names_the_runtime():
    """The message an operator actually receives.

    Asserted against the installer text because the raise sits inside the probe
    loop, which needs a live daemon to reach.
    """
    text = INSTALLER.read_text(encoding="utf-8")
    raise_site = text[text.index('"container runtime is unreadable') :][:400]

    assert "describe_runtime(runtime)" in raise_site
    assert "result.returncode" in raise_site


def test_never_up_is_not_treated_as_an_absence_proof():
    """The guard must stay closed on the ambiguity LastUp cannot resolve.

    Measured 2026-08-07 on a RUNNING machine: LastUp = 0001-01-01T00:00:00Z.
    A future change keying absence off that field would certify a live runtime
    as empty, so the reasoning is recorded at the raise site and asserted here.
    """
    text = INSTALLER.read_text(encoding="utf-8")

    assert "0001-01-01T00:00:00Z" in text, (
        "the measurement showing LastUp is unmaintained has been dropped; "
        "without it the 'treat never-up as absence' idea looks sound again"
    )
    assert re.search(r"if result\.returncode == 0:\s*\n\s*working\.append\(runtime\)", text), (
        "a runtime is being accepted on something other than a successful info probe"
    )
