"""Tests for the vendored-Hermes bootstrap (ADR 0001, hu-02/hu-03).

These verify the sys.path bootstrap contract. When no snapshot is vendored
(e.g. a lean checkout), the import-dependent assertions skip rather than fail.
"""

import sys

import pytest

from mac import hermes_vendor


def test_ensure_on_path_raises_when_not_vendored(monkeypatch, tmp_path):
    # Point VENDOR_DIR at an empty dir to simulate "not vendored".
    monkeypatch.setattr(hermes_vendor, "VENDOR_DIR", str(tmp_path / "_hermes"))
    assert hermes_vendor.is_vendored() is False
    with pytest.raises(RuntimeError):
        hermes_vendor.ensure_on_path()


@pytest.mark.skipif(not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present")
def test_vendored_snapshot_is_importable():
    pin = hermes_vendor.snapshot_pin()
    assert pin and len(pin) >= 12

    d = hermes_vendor.ensure_on_path()
    assert d.endswith("_hermes")
    assert d in sys.path
    # Idempotent: a second call must not add a duplicate path entry.
    before = sys.path.count(d)
    hermes_vendor.ensure_on_path()
    assert sys.path.count(d) == before

    # Hermes' flat top-level packages import unchanged from the vendored tree.
    import hermes_constants  # noqa: F401

    import hermes_cli.runtime_provider as rp

    assert hasattr(rp, "resolve_runtime_provider")


@pytest.mark.skipif(not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present")
def test_quiet_single_query_exit_code_fails_on_incomplete_turn():
    hermes_vendor.ensure_on_path()
    import cli as hermes_cli

    assert hermes_cli._quiet_result_exit_code({"completed": True, "failed": False}) == 0
    assert hermes_cli._quiet_result_exit_code({"completed": False, "failed": False}) == 1
    assert hermes_cli._quiet_result_exit_code({"completed": True, "partial": True}) == 1
    assert hermes_cli._quiet_result_exit_code({"completed": True, "failed": True}) == 1


@pytest.mark.skipif(not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present")
def test_terminal_login_shell_reasserts_sandbox_path(monkeypatch):
    hermes_vendor.ensure_on_path()
    from tools.environments.local import _prepend_sandbox_path

    monkeypatch.setenv("MAC_SANDBOX_PATH_PREFIX", "/sandbox/task/.mac-toolchain/bin")
    monkeypatch.setenv("MAC_SANDBOX_BASE_PATH", "/opt/mac-venv/bin:/usr/bin:/bin")

    command = _prepend_sandbox_path("command -v python")

    assert command.startswith(
        "export PATH=/sandbox/task/.mac-toolchain/bin:/opt/mac-venv/bin:/usr/bin:/bin\n"
    )
    assert command.endswith("command -v python")


def _gateway_extra_installed() -> bool:
    if not hermes_vendor.is_vendored():
        return False
    hermes_vendor.ensure_on_path()
    try:
        import slack_bolt  # noqa: F401  (the hermes-gateway extra)
    except ImportError:
        return False
    return True


@pytest.mark.skipif(
    not _gateway_extra_installed(),
    reason="hermes-gateway extra not installed (pip install -e '.[hermes-gateway]')",
)
def test_full_gateway_imports_in_process():
    """hu-03 premise: the gateway + agent runtime import in-process from the
    vendored tree (no separate venv, no string surgery)."""
    hermes_vendor.ensure_on_path()
    import gateway.run  # noqa: F401
    import gateway.session  # noqa: F401
    import agent.conversation_loop  # noqa: F401
    import agent.agent_init  # noqa: F401
    import plugins  # noqa: F401


@pytest.mark.skipif(
    not _gateway_extra_installed(),
    reason="hermes-gateway extra not installed",
)
def test_vendored_gateway_honors_mac_provider_override(monkeypatch):
    """hu-03: the vendored gateway resolves the per-agent provider/model via
    mac.agent_provider in-process (the owned replacement for the string-surgery
    shim), and preserves upstream behavior when no override is set."""
    hermes_vendor.ensure_on_path()
    import gateway.run as gr

    for k in (
        "MAC_HERMES_GATEWAY_MODEL", "ACC_HERMES_GATEWAY_MODEL", "HERMES_INFERENCE_MODEL",
        "ACC_LLM_MODEL", "MAC_HERMES_GATEWAY_PROVIDER", "MAC_HERMES_GATEWAY_BASE_URL",
        "TOKENHUB_URL", "OPENAI_BASE_URL",
        # A deployed hub/worker also exports the ACC_* base_url and the gateway
        # API keys; without clearing these the "no override" case picks up the
        # live deployment and _mac_provider_decision() returns non-None, failing
        # this test in the contract sandbox. Clear every input the decision reads.
        "ACC_HERMES_GATEWAY_BASE_URL", "MAC_HERMES_GATEWAY_API_KEY",
        "ACC_HERMES_GATEWAY_API_KEY", "ACC_HERMES_GATEWAY_PROVIDER",
        "MAC_LLM_MODEL", "MAC_LLM_PROVIDER", "ACC_LLM_PROVIDER",
    ):
        monkeypatch.delenv(k, raising=False)

    # No override -> standalone upstream behavior (config wins).
    assert gr._mac_provider_decision() is None
    assert gr._resolve_gateway_model({"model": {"default": "config-model"}}) == "config-model"

    # mac override -> wins (anti-monoculture lever), in-process, no string surgery.
    monkeypatch.setenv("MAC_HERMES_GATEWAY_MODEL", "mac-override-model")
    decision = gr._mac_provider_decision()
    assert decision is not None and decision.model == "mac-override-model"
    assert gr._resolve_gateway_model({"model": {"default": "config-model"}}) == "mac-override-model"


@pytest.mark.skipif(not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present")
def test_hermes_gateway_launcher_delegates_without_booting(monkeypatch):
    """hu-03/hu-04: the in-process launcher bootstraps the vendored tree, logs
    the provider decision, and invokes the SAME CLI entry as the deployed
    `hermes gateway run --replace` (injected so we don't boot the real gateway)."""
    import sys as _sys

    from mac import hermes_gateway

    seen = {}

    def fake_cli_main():
        seen["argv"] = list(_sys.argv)
        return 0

    rc = hermes_gateway.main(_cli_main=fake_cli_main)
    assert rc == 0
    # Must reproduce the deployed gateway invocation exactly.
    assert seen["argv"] == ["admin", "hermes", "gateway", "run", "--replace"]
    # provider-decision logging is best-effort and must return the observable dict
    observable = hermes_gateway.log_provider_decision()
    assert observable is not None and observable["schema"] == "mac.agent_provider.decision.v1"
