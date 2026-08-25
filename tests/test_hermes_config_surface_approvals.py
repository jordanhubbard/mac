"""The deploy bakes a never-prompt approval posture into Hermes config
(sandbox-01): approvals.mode=off + cron_mode=approve, ENFORCED.

OpenShell is the enforcement layer; Hermes must never block on a prompt (the
old gateway prompts went to an open Slack channel — no real security). The
posture is enforced (not default-if-absent) because agents carried a stale
approvals.mode=manual / cron_mode=deny that kept the gateway prompting; an
operator opts out per-host with MAC_HERMES_ALLOW_APPROVAL_PROMPTS=1.
"""

from __future__ import annotations

import yaml

import mac.hermes_config_surface as hcs


def test_defaults_added_to_empty_config():
    cfg: dict = {}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["mode"] == "off"
    assert cfg["approvals"]["cron_mode"] == "approve"


def test_stale_manual_mode_is_enforced_off():
    # The regression: a baked-in 'manual' must be overwritten to 'off', else the
    # gateway keeps showing "Command Approval Required".
    cfg = {"approvals": {"mode": "manual"}}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["mode"] == "off"
    assert cfg["approvals"]["cron_mode"] == "approve"


def test_stale_cron_mode_deny_is_enforced_approve():
    cfg = {"approvals": {"cron_mode": "deny", "mode": "manual"}}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["cron_mode"] == "approve"
    assert cfg["approvals"]["mode"] == "off"


def test_env_opt_out_preserves_operator_config(monkeypatch):
    monkeypatch.setenv("MAC_HERMES_ALLOW_APPROVAL_PROMPTS", "1")
    cfg = {"approvals": {"mode": "manual", "cron_mode": "deny"}}
    hcs._ensure_never_prompt_defaults(cfg)
    assert cfg["approvals"]["mode"] == "manual"  # opt-out: operator config wins
    assert cfg["approvals"]["cron_mode"] == "deny"


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
