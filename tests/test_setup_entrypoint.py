from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_setup_module():
    spec = importlib.util.spec_from_file_location("mac_root_setup", ROOT / "setup.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_py_routes_modes_without_bash_arrays():
    setup = _load_setup_module()

    args, config_only, dry_run, deploy_direct = setup.parse_setup_args(
        ["--configure-only", "--spec", "fleet.yaml", "--force"]
    )
    assert args == ["--spec", "fleet.yaml", "--force"]
    assert config_only is True
    assert dry_run is False
    assert deploy_direct is False

    args, config_only, dry_run, deploy_direct = setup.parse_setup_args(
        ["--hub", "dev", "worker-a"]
    )
    assert args == ["--hub", "dev", "worker-a"]
    assert config_only is False
    assert dry_run is False
    assert deploy_direct is True


def test_setup_py_parses_generated_env_file(tmp_path):
    setup = _load_setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "MAC_API_TOKEN='tok with spaces'",
                'NVIDIA_API_KEY="nv-token"',
                "export MAC_SECRET_KEY=secret-value",
                "# ignored",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    values = setup.parse_env_file(env_file)

    assert values["MAC_API_TOKEN"] == "tok with spaces"
    assert values["NVIDIA_API_KEY"] == "nv-token"
    assert values["MAC_SECRET_KEY"] == "secret-value"


def test_setup_py_builds_deploy_args_from_plan():
    setup = _load_setup_module()

    assert setup.deploy_args_from_plan({"hub": "dev", "agents": ["worker-a", ""]}) == [
        str(ROOT / "deploy" / "deploy-mac-fleet.sh"),
        "--hub",
        "dev",
        "worker-a",
    ]


def test_setup_py_deploy_env_loads_generated_env_file(tmp_path):
    setup = _load_setup_module()
    env_file = tmp_path / ".env"
    env_file.write_text("MAC_API_TOKEN=from-file\n", encoding="utf-8")

    values = setup.deploy_env(env_file)

    assert values["MAC_API_TOKEN"] == "from-file"
    assert values["PYTHON"] == sys.executable
    assert values.get("PATH") == os.environ.get("PATH")
