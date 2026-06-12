"""The deploy bakes a never-prompt approval posture into Hermes config
(sandbox-01): approvals.mode=off + cron_mode=approve, default-if-absent.

OpenShell is the enforcement layer; Hermes must never block on a prompt (the
old gateway prompts went to an open Slack channel — no real security).
"""

from __future__ import annotations

import yaml

import mac.hermes_config_surface as hcs


def test_defaults_added_to_empty_config():
    cfg: dict = {}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["mode"] == "off"
    assert cfg["approvals"]["cron_mode"] == "approve"


def test_explicit_mode_not_clobbered():
    cfg = {"approvals": {"mode": "manual"}}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["mode"] == "manual"        # operator choice wins
    assert cfg["approvals"]["cron_mode"] == "approve"  # missing key still filled


def test_explicit_cron_mode_not_clobbered():
    cfg = {"approvals": {"cron_mode": "deny"}}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["cron_mode"] == "deny"
    assert cfg["approvals"]["mode"] == "off"


def test_non_dict_approvals_replaced():
    cfg = {"approvals": "bogus"}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["mode"] == "off"


def test_apply_payload_writes_never_prompt(tmp_path):
    home = tmp_path
    (home / "config.yaml").write_text("{}\n")
    hcs.apply_hermes_surface_payload({}, target_home=home)
    written = yaml.safe_load((home / "config.yaml").read_text()) or {}
    appr = written.get("approvals") or {}
    # 'off' may round-trip as the YAML 1.1 bool False; both are accepted by the
    # approval reader (approval.py treats False as the off mode).
    assert appr.get("mode") in ("off", False)
    assert appr.get("cron_mode") == "approve"
