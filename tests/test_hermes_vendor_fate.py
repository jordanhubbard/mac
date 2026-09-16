"""Lock the post-removal fate of the vendored Hermes tree.

PR #377 deleted ``src/mac/_hermes`` and the re-vendor machinery. These tests
keep the four pre-deletion answers (a)–(d) from drifting back into a live
dependency without an explicit decision.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from mac.hermes_config_surface import _hermes_config_module

ROOT = Path(__file__).resolve().parents[1]
SRC_MAC = ROOT / "src" / "mac"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CONTAINERFILE = ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
ADR_0001 = ROOT / "docs" / "adr" / "0001-unify-hermes-runtime-into-mac.md"
FATE_DOC = ROOT / "docs" / "hermes-vendor-fate.md"


def _live_hermes_cli_import_lines(source: str) -> list[int]:
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        if any(module.split(".")[0] == "hermes_cli" for module in modules):
            lines.append(node.lineno)
    return sorted(lines)


def test_vendored_hermes_tree_is_gone() -> None:
    assert not (SRC_MAC / "_hermes").exists()
    assert not (SRC_MAC / "hermes_vendor.py").exists()
    assert not (SRC_MAC / "hermes_gateway.py").exists()
    # deploy/hermes/ itself came back (2026-09-05) to hold
    # install-hermes-gateway.sh, the host-level lifecycle script that shells
    # out to an externally-installed `hermes` CLI -- see
    # docs/hermes-vendor-fate.md. What must stay gone is the vendoring
    # machinery it used to hold: the pinned snapshot and the plugin/tool
    # overlay applied on top of it. Pinned external compatibility patches
    # are governed separately; they do not install an in-process runtime.
    deploy_hermes = ROOT / "deploy" / "hermes"
    assert not (deploy_hermes / "SNAPSHOT.md").exists()
    assert not (deploy_hermes / "HERMES_TREE_SHA256").exists()
    assert not (deploy_hermes / "LOCAL_PATCHES.md").exists()
    assert not (deploy_hermes / "overlay").exists()


def test_external_hermes_patches_match_their_source_manifests() -> None:
    for patch in (ROOT / "deploy" / "hermes").glob("*.patch"):
        manifest = json.loads(patch.with_name(patch.stem + "-source.json").read_text())
        assert manifest["patch"] == patch.name
        assert re.fullmatch(r"[0-9a-f]{40}", manifest["upstream_commit"])
        assert hashlib.sha256(patch.read_bytes()).hexdigest() == manifest["patch_sha256"]
        numstat = subprocess.check_output(
            ["git", "apply", "--numstat", str(patch)], cwd=ROOT, text=True
        )
        changed_paths = {line.split("\t", 2)[2] for line in numstat.splitlines()}
        assert changed_paths == set(manifest["files"])
        for hashes in manifest["files"].values():
            assert re.fullmatch(r"[0-9a-f]{64}", hashes["original_sha256"])
            assert re.fullmatch(r"[0-9a-f]{64}", hashes["patched_sha256"])


def test_a_no_live_hermes_cli_imports_in_mac_sources() -> None:
    offenders: list[str] = []
    for path in SRC_MAC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Qualification scripts are data passed to the external interpreter.
        # Inspect executable imports, not examples or subprocess script strings.
        for lineno in _live_hermes_cli_import_lines(text):
            line = text.splitlines()[lineno - 1]
            offenders.append("%s:%d:%s" % (path.relative_to(ROOT), lineno, line.strip()))
    assert offenders == [], "live hermes_cli imports returned:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("import hermes_cli.main as main", [1]),
        ("import os, hermes_cli.main", [1]),
        ("from hermes_cli import main", [1]),
        ("def start():\n    from hermes_cli.main import main", [2]),
        ('PROBE = """\nimport hermes_cli.main\n"""', []),
        ("# import hermes_cli\nimport hermes_cli_tools", []),
    ],
)
def test_live_import_guard_distinguishes_code_from_script_data(source, lines):
    assert _live_hermes_cli_import_lines(source) == lines


def test_a_hermes_config_surface_degrades_without_hermes_cli() -> None:
    with pytest.raises(ModuleNotFoundError, match="vendored hermes_cli was removed"):
        _hermes_config_module()


def test_b_agent_command_has_no_hermes_cli_main_branch() -> None:
    source = (SRC_MAC / "agent_command.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "hermes_cli.main" not in node.value


def test_c_openclaw_continuity_does_not_point_at_vendor_plugins_or_skills() -> None:
    migrate = ROOT / "deploy" / "openclaw" / "migrate-hermes-continuity.py"
    text = migrate.read_text(encoding="utf-8")
    assert "_hermes/plugins" not in text
    assert "_hermes/skills" not in text
    assert "src/mac/_hermes" not in text


def test_d_snapshot_obligation_and_revendor_job_are_gone() -> None:
    assert not (ROOT / "deploy" / "hermes" / "SNAPSHOT.md").exists()
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert "hermes-revendor" not in jobs
    watched = set(jobs["report-main-red"]["needs"])
    assert "hermes-revendor" not in watched
    # Load-bearing check: no .pth injection remains. Stale narrative comments in
    # the Containerfile may still *mention* hermes_cli; they do not reintroduce it.
    containerfile = CONTAINERFILE.read_text(encoding="utf-8")
    assert "zz_hermes_vendor.pth" not in containerfile
    assert not re.search(r"^\s*(?:COPY|RUN|ADD).*\.pth", containerfile, flags=re.MULTILINE)


def test_deploy_env_does_not_default_agent_dir_to_removed_vendor_tree() -> None:
    source = (SRC_MAC / "deploy_env.py").read_text(encoding="utf-8")
    path_values = source.split("def _path_values", 1)[1].split("\ndef ", 1)[0]
    assert '"MAC_HERMES_AGENT_DIR"' not in path_values
    assert "'MAC_HERMES_AGENT_DIR'" not in path_values
    assert "/_hermes" not in path_values


def test_adr_0001_records_vendoring_premise_ended() -> None:
    text = ADR_0001.read_text(encoding="utf-8")
    assert "Superseded" in text
    assert "vendoring premise ended" in text.lower()
    assert FATE_DOC.is_file()
    fate = FATE_DOC.read_text(encoding="utf-8")
    assert "**Verdict: removed.**" in fate
    for label in ("**(a)**", "**(b)**", "**(c)**", "**(d)**"):
        assert label in fate
