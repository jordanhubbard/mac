"""Fresh-volume qualification for ``deploy/openshell/hgx-fungible-bootstrap.py``.

Every case starts from a pristine (or deliberately damaged) persistent volume
and proves the bootstrap makes it deployable and self-validating, per the task
contract:

1. ``~/.mac`` is provisioned owner-only (no group/other bits).
2. A supported Python is exposed via an exec wrapper, NOT a base_prefix-breaking
   symlink.
3. mac / codegraph / gh links exist before deployment.
4. The OpenShell storage layout invariants are validated.
5. A failure yields a precise remediation receipt, never a partial venv.
6. The whole flow runs from a fresh persistent volume.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "openshell" / "hgx-fungible-bootstrap.py"


def _module():
    spec = importlib.util.spec_from_file_location("hgx_fungible_bootstrap", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _module()


def _fresh_home(tmp_path: Path) -> Path:
    """A pristine persistent-volume home with nothing under it yet."""

    home = tmp_path / "runtime-home"
    home.mkdir()
    return home


def _fake_interpreter(tmp_path: Path) -> Path:
    interp = tmp_path / "uv" / "cpython-3.12.11" / "bin" / "python3.12"
    interp.parent.mkdir(parents=True)
    interp.write_text("#!/bin/sh\nexit 0\n")
    interp.chmod(0o755)
    return interp


def test_helper_exists_and_is_executable():
    assert HELPER.is_file()
    assert os.access(HELPER, os.X_OK)


def test_provision_from_fresh_volume_is_deployable(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)

    receipt = mod.provision(layout, interpreter=interp, apply_ownership=False, instance="worker1")

    assert receipt["status"] == "deployable"
    assert receipt["schema"] == mod.RECEIPT_SCHEMA
    assert receipt["instance"] == "worker1"
    assert all(check["ok"] for check in receipt["checks"])
    # Receipt persisted owner-only.
    assert layout.receipt.is_file()
    assert stat.S_IMODE(layout.receipt.stat().st_mode) == mod.FILE_MODE


def test_mac_home_is_owner_only_after_provision(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    mod.provision(layout, interpreter=interp, apply_ownership=False)

    for current in mod._walk(layout.mac_home):
        if current.is_symlink():
            continue
        bits = stat.S_IMODE(current.lstat().st_mode)
        assert not (bits & (stat.S_IRWXG | stat.S_IRWXO)), current


def test_group_readable_volume_is_hardened(mod, tmp_path):
    """A volume that ships group/world readable is repaired in place."""

    home = _fresh_home(tmp_path)
    mac_home = home / ".mac"
    (mac_home / "state").mkdir(parents=True)
    (mac_home / "state" / "secret.json").write_text("{}")
    # Simulate the broken persistent-volume modes.
    os.chmod(mac_home, 0o755)
    os.chmod(mac_home / "state", 0o755)
    os.chmod(mac_home / "state" / "secret.json", 0o644)

    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    # Secret content must be preserved, only modes tightened.
    mod.provision(layout, interpreter=interp, apply_ownership=False)

    assert (mac_home / "state" / "secret.json").read_text() == "{}"
    for current in mod._walk(mac_home):
        if current.is_symlink():
            continue
        bits = stat.S_IMODE(current.lstat().st_mode)
        assert not (bits & (stat.S_IRWXG | stat.S_IRWXO)), current


def test_python_is_exec_wrapper_not_symlink(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    mod.provision(layout, interpreter=interp, apply_ownership=False)

    wrapper = layout.python_wrapper
    assert wrapper.is_file()
    assert not wrapper.is_symlink()
    assert os.access(wrapper, os.X_OK)
    text = wrapper.read_text()
    assert mod.wrapper_target(text) == str(interp)


def test_symlink_wrapper_preserves_base_prefix_semantics(mod, tmp_path):
    """The wrapper must exec the real interpreter so sys.base_prefix is intact."""

    interp = _fake_interpreter(tmp_path)
    text = mod.render_python_wrapper(interp)
    assert text.startswith("#!/bin/sh")
    assert 'exec "%s" "$@"' % interp in text
    # An exec wrapper is NOT a symlink target chain.
    assert "ln -s" not in text


def test_existing_python_symlink_is_replaced_with_wrapper(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    # The known-broken state: a direct symlink to the uv interpreter.
    layout.python_wrapper.parent.mkdir(parents=True)
    os.symlink(interp, layout.python_wrapper)
    assert layout.python_wrapper.is_symlink()

    mod.provision(layout, interpreter=interp, apply_ownership=False)

    assert not layout.python_wrapper.is_symlink()
    assert layout.python_wrapper.is_file()


def test_tool_links_present_after_provision(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    mod.provision(layout, interpreter=interp, apply_ownership=False)

    for link in (layout.mac_bin, layout.codegraph_bin, layout.gh_bin):
        assert link.exists() or link.is_symlink(), link


def test_provision_does_not_fabricate_a_venv(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    mod.provision(layout, interpreter=interp, apply_ownership=False)

    # Nothing should have created a half-real successor venv.
    assert not layout.venv.exists()
    assert not mod.is_partial_venv(layout.venv)


def test_partial_venv_is_cleared_not_trusted(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    # An interrupted successor venv: a bin/python but no pyvenv.cfg marker.
    (layout.venv / "bin").mkdir(parents=True)
    (layout.venv / "bin" / "python").write_text("broken")
    assert mod.is_partial_venv(layout.venv)

    receipt = mod.provision(layout, interpreter=interp, apply_ownership=False)

    assert receipt["status"] == "deployable"
    assert not layout.venv.exists()
    assert any("cleared_partial_venv" in a for a in receipt["actions"])


def test_missing_interpreter_fails_with_remediation_not_partial_venv(mod, tmp_path):
    home = _fresh_home(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    missing = tmp_path / "nope" / "python3.12"
    # Leave a partial venv to prove it is removed on failure.
    (layout.venv / "bin").mkdir(parents=True)
    (layout.venv / "bin" / "python").write_text("broken")

    with pytest.raises(mod.BootstrapError) as excinfo:
        mod.provision(layout, interpreter=missing, apply_ownership=False)

    err = excinfo.value
    payload = err.as_dict()
    assert payload["status"] == "failed"
    assert payload["schema"] == mod.REMEDIATION_SCHEMA
    assert payload["remediation"]
    # No partial venv left behind.
    assert not layout.venv.exists()


def test_validate_on_fresh_volume_reports_precise_remediation(mod, tmp_path):
    home = _fresh_home(tmp_path)
    layout = mod.VolumeLayout.for_home(home)

    with pytest.raises(mod.BootstrapError) as excinfo:
        mod.validate(layout)

    payload = excinfo.value.as_dict()
    assert payload["status"] == "failed"
    assert "provision" in payload["remediation"]


def test_validate_passes_after_provision(mod, tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)
    layout = mod.VolumeLayout.for_home(home)
    mod.provision(layout, interpreter=interp, apply_ownership=False)

    result = mod.validate(layout)
    assert result["status"] == "deployable"
    assert all(check["ok"] for check in result["checks"])


def test_instance_name_is_not_a_static_hostname(mod):
    # Optional metadata, and hostile input is rejected before it reaches a
    # receipt or command line.
    assert mod._validate_instance(None) is None
    assert mod._validate_instance("worker2") == "worker2"
    for bad in ("../etc", "a b", "$(rm -rf /)", "-flag", ""):
        with pytest.raises(mod.BootstrapError):
            mod._validate_instance(bad)


def test_layout_mirrors_hgx_provision_paths(mod, tmp_path):
    home = tmp_path / "acct"
    home.mkdir()
    layout = mod.VolumeLayout.for_home(home)
    assert layout.mac_home == home / ".mac"
    assert layout.venv == home / ".mac" / "venv"
    assert layout.source == home / ".mac" / "src" / "mac"
    assert layout.codegraph_bin == home / ".mac" / "bin" / "codegraph"
    assert layout.gh_bin == home / ".mac" / "bin" / "gh"
    assert layout.mac_bin == home / ".local" / "bin" / "mac"


def test_toolchain_pins_match_onboarding_contract(mod):
    assert mod.UV_VERSION == "0.8.22"
    assert mod.PYTHON_VERSION == "3.12.11"
    assert mod.CODEGRAPH_VERSION == "v1.5.0"


def test_cli_provision_then_validate_from_fresh_volume(tmp_path):
    home = _fresh_home(tmp_path)
    interp = _fake_interpreter(tmp_path)

    provision = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--home",
            str(home),
            "--no-ownership",
            "--interpreter",
            str(interp),
            "provision",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert provision.returncode == 0, provision.stderr
    receipt = json.loads(provision.stdout)
    assert receipt["status"] == "deployable"

    validate = subprocess.run(
        [sys.executable, str(HELPER), "--home", str(home), "--no-ownership", "validate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert validate.returncode == 0, validate.stderr
    assert json.loads(validate.stdout)["status"] == "deployable"


def test_cli_validate_failure_exits_nonzero_with_remediation(tmp_path):
    home = _fresh_home(tmp_path)
    result = subprocess.run(
        [sys.executable, str(HELPER), "--home", str(home), "--no-ownership", "validate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["schema"]
    assert payload["remediation"]
