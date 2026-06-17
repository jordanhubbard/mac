"""Phase 3 — the pure ACP permission evaluator (ADR 0006)."""

from __future__ import annotations

from mac.acp.permission import (
    PermissionMode,
    evaluate_permission,
    load_openshell_policy,
    permission_mode,
)


# A policy that allows egress + writes (networked production-style profile).
_OPEN_POLICY = {
    "network_policies": {"mac_hub": {"name": "mac-hub", "endpoints": [{"host": "h", "port": 1}]}},
    "filesystem_policy": {"read_only": ["/usr"], "read_write": ["/tmp", "/work"]},
}

# A lockdown policy: empty network_policies (deny egress), read-only filesystem.
_LOCKDOWN_POLICY = {
    "network_policies": {},
    "filesystem_policy": {"read_only": ["/usr"], "read_write": []},
}


def _tc(kind):
    return {"kind": kind, "title": "t", "toolCallId": "tc1"}


# -- mode resolution ---------------------------------------------------------


def test_permission_mode_defaults_to_policy(monkeypatch):
    monkeypatch.delenv("MAC_ACP_PERMISSION_MODE", raising=False)
    assert permission_mode() == PermissionMode.POLICY
    monkeypatch.setenv("MAC_ACP_PERMISSION_MODE", "DENY")
    assert permission_mode() == PermissionMode.DENY
    monkeypatch.setenv("MAC_ACP_PERMISSION_MODE", "nonsense")
    assert permission_mode() == PermissionMode.POLICY  # unknown -> default


# -- explicit allow / deny modes ---------------------------------------------


def test_mode_allow_always_allows():
    d = evaluate_permission(_tc("execute"), policy=_LOCKDOWN_POLICY, mode="allow")
    assert d.allow is True and d.reason == "mode-allow"


def test_mode_deny_always_denies():
    d = evaluate_permission(_tc("read"), policy=_OPEN_POLICY, mode="deny")
    assert d.allow is False and d.reason == "mode-deny"


# -- sandbox short-circuit ---------------------------------------------------


def test_sandboxed_short_circuits_to_allow_even_under_lockdown():
    # the kernel sandbox is the real gate; the ACP prompt is advisory
    d = evaluate_permission(_tc("execute"), policy=_LOCKDOWN_POLICY, sandboxed=True, mode="policy")
    assert d.allow is True and d.reason == "sandbox-enforced"
    # sandbox even overrides mode=deny
    d2 = evaluate_permission(_tc("execute"), sandboxed=True, mode="deny")
    assert d2.allow is True and d2.reason == "sandbox-enforced"


# -- no-policy default-allow (Phase-1 parity) --------------------------------


def test_policy_mode_without_policy_defaults_to_allow():
    d = evaluate_permission(_tc("execute"), policy=None, sandboxed=False, mode="policy")
    assert d.allow is True and d.reason == "no-policy-default-allow"


def test_policy_mode_without_policy_can_be_flipped_to_deny():
    d = evaluate_permission(_tc("execute"), policy=None, sandboxed=False, mode="deny")
    assert d.allow is False and d.reason == "mode-deny"


# -- benign kinds always allowed ---------------------------------------------


def test_benign_kinds_allowed_under_lockdown():
    for kind in ("read", "search", "think"):
        d = evaluate_permission(_tc(kind), policy=_LOCKDOWN_POLICY, mode="policy")
        assert d.allow is True, kind
        assert d.reason.startswith("benign-kind")


# -- network intent ----------------------------------------------------------


def test_network_lockdown_denies_egress():
    d = evaluate_permission(_tc("fetch"), policy=_LOCKDOWN_POLICY, mode="policy")
    assert d.allow is False and d.reason == "policy-network-lockdown"


def test_network_allowed_when_policy_has_network_policies():
    d = evaluate_permission(_tc("fetch"), policy=_OPEN_POLICY, mode="policy")
    assert d.allow is True and d.reason == "policy-network-allowed"


# -- fs-write intent ---------------------------------------------------------


def test_fs_write_denied_when_read_write_empty():
    for kind in ("edit", "delete", "move", "create", "write"):
        d = evaluate_permission(_tc(kind), policy=_LOCKDOWN_POLICY, mode="policy")
        assert d.allow is False, kind
        assert d.reason == "policy-fs-write-readonly"


def test_fs_write_allowed_when_read_write_nonempty():
    d = evaluate_permission(_tc("write"), policy=_OPEN_POLICY, mode="policy")
    assert d.allow is True and d.reason == "policy-fs-write-allowed"


# -- execute / unknown unsandboxed under policy ------------------------------


def test_execute_unsandboxed_denied_under_policy():
    d = evaluate_permission(_tc("execute"), policy=_OPEN_POLICY, sandboxed=False, mode="policy")
    assert d.allow is False and d.reason.startswith("policy-unsandboxed-execute")


def test_unknown_kind_unsandboxed_denied_under_policy():
    d = evaluate_permission(_tc("teleport"), policy=_OPEN_POLICY, sandboxed=False, mode="policy")
    assert d.allow is False and d.reason.startswith("policy-unsandboxed-execute")


# -- policy loader (best-effort) ---------------------------------------------


def test_load_openshell_policy_reads_yaml(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "version: 1\nnetwork_policies:\n  mac_hub:\n    name: mac-hub\n",
        encoding="utf-8",
    )
    parsed = load_openshell_policy(str(policy_file))
    assert isinstance(parsed, dict)
    assert "mac_hub" in parsed["network_policies"]


def test_load_openshell_policy_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_POLICY", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert load_openshell_policy() is None


def test_load_openshell_policy_uses_env(tmp_path, monkeypatch):
    policy_file = tmp_path / "env-policy.yaml"
    policy_file.write_text("network_policies: {}\nfilesystem_policy: {read_write: []}\n", encoding="utf-8")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(policy_file))
    parsed = load_openshell_policy()
    assert parsed == {"network_policies": {}, "filesystem_policy": {"read_write": []}}
