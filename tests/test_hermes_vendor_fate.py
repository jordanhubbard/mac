"""Lock the post-removal fate of the vendored Hermes tree.

PR #377 deleted ``src/mac/_hermes`` and the re-vendor machinery. These tests
keep the four pre-deletion answers (a)–(d) from drifting back into a live
dependency without an explicit decision.
"""

from __future__ import annotations

import ast
import re
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

_IMPORT_HERMES_CLI = re.compile(r"(?:^|\s)(?:from\s+hermes_cli\b|import\s+hermes_cli\b)")


def test_vendored_hermes_tree_is_gone() -> None:
    assert not (SRC_MAC / "_hermes").exists()
    assert not (SRC_MAC / "hermes_vendor.py").exists()
    assert not (SRC_MAC / "hermes_gateway.py").exists()
    # deploy/hermes/ itself came back (2026-09-05) to hold
    # install-hermes-gateway.sh, the host-level lifecycle script that shells
    # out to an externally-installed `hermes` CLI -- see
    # docs/hermes-vendor-fate.md. What must stay gone is the vendoring
    # machinery it used to hold: the pinned snapshot, its local patch set, and
    # the plugin/tool overlay applied on top of it.
    deploy_hermes = ROOT / "deploy" / "hermes"
    assert not (deploy_hermes / "SNAPSHOT.md").exists()
    assert not (deploy_hermes / "HERMES_TREE_SHA256").exists()
    assert not (deploy_hermes / "LOCAL_PATCHES.md").exists()
    assert not (deploy_hermes / "overlay").exists()
    assert not list(deploy_hermes.glob("*.patch"))


def test_a_no_live_hermes_cli_imports_in_mac_sources() -> None:
    offenders: list[str] = []
    for path in SRC_MAC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if _IMPORT_HERMES_CLI.search(line):
                offenders.append("%s:%d:%s" % (path.relative_to(ROOT), lineno, line.strip()))
    assert offenders == [], "live hermes_cli imports returned:\n" + "\n".join(offenders)


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


def test_deploy_env_defaults_gateway_home_to_openclaw() -> None:
    source = (SRC_MAC / "deploy_env.py").read_text(encoding="utf-8")
    path_values = source.split("def _path_values", 1)[1].split("\ndef ", 1)[0]
    assert 'paths.home / ".hermes"' not in path_values
    assert 'paths.mac_home / "openclaw"' in path_values


def test_adr_0001_records_vendoring_premise_ended() -> None:
    text = ADR_0001.read_text(encoding="utf-8")
    assert "Superseded" in text
    assert "vendoring premise ended" in text.lower()
    assert FATE_DOC.is_file()
    fate = FATE_DOC.read_text(encoding="utf-8")
    assert "**Verdict: removed.**" in fate
    for label in ("**(a)**", "**(b)**", "**(c)**", "**(d)**"):
        assert label in fate
