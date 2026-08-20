"""A from-scratch first-hub bootstrap must not demand phase-1 cohort quiescence.

Phase-1 quiescence is evidence about a *prior* generation: the cohort was
drained, its topology recorded, and it can be restored.  A first-hub bootstrap
installs onto a node with no prior generation, so it runs no phase-1
transaction and writes no receipt.  Every layer that consumes the requirement
has to agree about that, or the documented from-scratch path fails on a file it
never creates.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"

AGENT = "hub"
TS = "20260820T000000Z"
REV = "a" * 40
GENERATION = "deployment_0123456789abcdef"
DIGEST = hashlib.sha256(b"receipt").hexdigest()


def _function(source: str, name: str) -> str:
    """Return the body text of a top-level shell function."""
    return source.split("%s() {" % name, 1)[1].split("\n}\n", 1)[0]


def _run_bash(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **(env or {})},
    )


@pytest.mark.parametrize(
    ("first_hub_bootstrap", "expected"),
    [
        (None, "1"),
        ("", "1"),
        ("0", "1"),
        ("false", "1"),
        ("1", "0"),
        ("true", "0"),
        ("TRUE", "0"),
        ("yes", "0"),
        ("on", "0"),
    ],
)
def test_remote_quiescence_flag_follows_first_hub_bootstrap(first_hub_bootstrap, expected) -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    harness = "\n".join(
        [
            "set -euo pipefail",
            "normalize_boolean_token() {%s\n}" % _function(source, "normalize_boolean_token"),
            "phase1_quiescence_remote_flag() {%s\n}" % _function(source, "phase1_quiescence_remote_flag"),
            "phase1_quiescence_remote_flag",
        ]
    )
    env = {} if first_hub_bootstrap is None else {"FIRST_HUB_BOOTSTRAP": first_hub_bootstrap}
    result = _run_bash(harness, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_deploy_never_hardcodes_the_phase1_requirement() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    # The bug this guards: one unconditional `1` in the shared remote-env path
    # made every install action -- including the from-scratch bootstrap --
    # promise the node a phase-1 receipt that only an upgrade can produce.
    assert "add_remote_env MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE 1" not in source
    assert (
        'add_remote_env MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE "$(phase1_quiescence_remote_flag)"'
        in source
    )
    # Reconciliation validates the manifest the install was actually asked to
    # produce, so it must be told the same thing the install was told.
    assert (
        'MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE=$(shell_quote "$(phase1_quiescence_remote_flag)")'
        in source
    )


def test_node_install_skips_prior_topology_when_quiescence_is_not_required(tmp_path) -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    body = source.split("capture_phase1_prior_worker_topology() {", 1)[1]
    guard = body.split('topology="$("$PY" -', 1)[0]
    # The receipt-reading remainder of the function is represented by a sentinel
    # return: this asserts *whether* the strict path is entered, without running
    # the embedded receipt validator.
    harness = "\n".join(
        [
            "set -uo pipefail",
            "truthy() {%s\n}" % _function(source, "truthy"),
            'log() { printf "LOG %s\\n" "$*"; }',
            'write_rollback_script() { printf "ROLLBACK_WRITTEN\\n"; }',
            "capture_phase1_prior_worker_topology() {",
            guard,
            "  return 9",
            "}",
            "capture_phase1_prior_worker_topology",
            "rc=$?",
            'printf "GATEWAY=%s AGENT_PRIOR=%s\\n" "${ROLLBACK_ACTIVE_GATEWAY:-unset}" "${ROLLBACK_AGENT_PRIOR_STATE:-unset}"',
            "exit $rc",
        ]
    )

    for env in ({}, {"MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE": "0"}):
        result = _run_bash(harness, env)
        assert result.returncode == 0, result.stderr
        assert "ROLLBACK_WRITTEN" in result.stdout
        assert "GATEWAY=none AGENT_PRIOR=absent" in result.stdout

    required = _run_bash(harness, {"MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE": "1"})
    assert required.returncode == 9, required.stdout
    assert "ROLLBACK_WRITTEN" not in required.stdout


def test_node_install_launchd_prestate_is_gated_on_the_same_requirement() -> None:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    body = source.split("capture_darwin_launchd_prestate() {", 1)[1]
    guard = body.split('if ! truthy "${MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE:-0}"; then', 1)
    assert len(guard) == 2, "launchd prestate no longer honours the phase-1 requirement"
    skipped, remainder = guard[1].split("\n  else\n", 1)
    assert "assuming no prior launchd gateway or worker" in skipped
    assert 'phase1-cohort-quiescence-${DEPLOY_GENERATION}.json' not in skipped
    assert 'phase1-cohort-quiescence-${DEPLOY_GENERATION}.json' in remainder


def _reconcile_validator() -> str:
    source = DEPLOY.read_text(encoding="utf-8")
    start = source.index('"$python_bin" - "$manifest" "$latest"')
    body = source[source.index("<<'PY'\n", start) + len("<<'PY'\n"):]
    return body[: body.index("\nPY\n")]


def _manifest(*, phase1: dict, media: dict) -> dict:
    return {
        "stage": "post",
        "agent": AGENT,
        "deploy": {"timestamp": TS, "mac_git_rev": REV},
        "daemon_resource_quiescence": {
            "schema": "mac.daemon_resource_quiescence_manifest.v1",
            "status": "proved",
            "generation": GENERATION,
            "revision": REV,
            "sha256": DIGEST,
            "required_phases": ["pre_source", "pre_install", "post_install"],
            "proved_phases": ["pre_source", "pre_install", "post_install"],
            "container_runtimes": [],
        },
        "phase1_cohort_quiescence": phase1,
        "media_runtime_readiness": media,
        "gateway_readiness": {
            "schema": "mac.gateway_readiness_manifest.v1",
            "status": "proved",
            "generation": GENERATION,
            "revision": REV,
            "stable_observations": 2,
            "implementation": "openclaw",
            "supervisor": "launchd",
            "sha256": DIGEST,
            "identities": {},
            "state": {},
        },
    }


_NOT_REQUIRED_PHASE1 = {
    "schema": "mac.phase1_cohort_quiescence_manifest.v1",
    "status": "not_required",
}
_NOT_REQUIRED_MEDIA = {
    "schema": "mac.media_runtime_readiness_manifest.v1",
    "status": "not_required",
}
_PROVED_PHASE1 = {
    "schema": "mac.phase1_cohort_quiescence_manifest.v1",
    "status": "proved",
    "path": "/home/agent/.mac/phase1-cohort-quiescence-%s.json" % GENERATION,
    "generation": GENERATION,
    "revision": REV,
    "sha256": DIGEST,
    "supervisor": {"manager": "launchd"},
    "daemon_resource_receipt": {
        "schema": "mac.daemon_resource_quiescence.v1",
        "proof_phase": "pre_source",
        "sha256": DIGEST,
        "function_block_sha256": DIGEST,
    },
}
_PROVED_MEDIA = {
    "schema": "mac.media_runtime_readiness_manifest.v1",
    "status": "not_applicable",
    "manager": "launchd",
    "sha256": DIGEST,
    "source_contract_sha256": DIGEST,
    "resources": [],
}


def _reconcile(tmp_path: Path, manifest: dict, require_phase1: str) -> subprocess.CompletedProcess:
    post = tmp_path / "deploy-manifest-post.json"
    latest = tmp_path / "deploy-manifest-latest.json"
    for path in (post, latest):
        path.write_text(json.dumps(manifest), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _reconcile_validator(),
            str(post),
            str(latest),
            AGENT,
            TS,
            REV,
            GENERATION,
            require_phase1,
        ],
        capture_output=True,
        text=True,
    )


def test_reconciliation_accepts_a_from_scratch_manifest(tmp_path) -> None:
    manifest = _manifest(phase1=_NOT_REQUIRED_PHASE1, media=_NOT_REQUIRED_MEDIA)
    result = _reconcile(tmp_path, manifest, "0")
    assert result.returncode == 0, result.stderr


def test_reconciliation_still_requires_phase1_proof_for_normal_installs(tmp_path) -> None:
    manifest = _manifest(phase1=_NOT_REQUIRED_PHASE1, media=_NOT_REQUIRED_MEDIA)
    result = _reconcile(tmp_path, manifest, "1")
    assert result.returncode != 0
    assert "invalid phase-1 evidence" in result.stderr

    proved = _manifest(phase1=_PROVED_PHASE1, media=_PROVED_MEDIA)
    assert _reconcile(tmp_path, proved, "1").returncode == 0


def test_reconciliation_rejects_an_unexpected_phase1_proof(tmp_path) -> None:
    # A node that proves phase-1 for a from-scratch install ran something other
    # than the install we asked for; that divergence stays fail-closed.
    manifest = _manifest(phase1=_PROVED_PHASE1, media=_PROVED_MEDIA)
    result = _reconcile(tmp_path, manifest, "0")
    assert result.returncode != 0
    assert "invalid phase-1 evidence" in result.stderr
