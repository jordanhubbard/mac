from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "deploy" / "install-qdrant-service.sh"
SYSTEMD_UNIT = ROOT / "deploy" / "systemd" / "mac-qdrant.service"


def _extract_function(path: Path, name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        path.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"function {name} not found"
    return match.group(0)


def test_install_script_is_valid_bash() -> None:
    result = subprocess.run(["bash", "-n", str(INSTALL_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_systemd_unit_uses_the_selected_container_runtime(tmp_path: Path) -> None:
    template = SYSTEMD_UNIT.read_text(encoding="utf-8")
    assert template.count("@QDRANT_CONTAINER_RUNTIME@") == 3
    function = _extract_function(INSTALL_SCRIPT, "render_systemd_unit")
    for runtime in ("/usr/bin/docker", "/usr/bin/podman"):
        rendered = tmp_path / (Path(runtime).name + ".service")
        result = subprocess.run(
            ["bash", "-c", function + '\nrender_systemd_unit "$OUTPUT"'],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "UNIT_TEMPLATE": str(SYSTEMD_UNIT),
                "ENV_DEST": "/etc/ovswarm/qdrant.env",
                "CONTAINER_CMD_ABS": runtime,
                "OUTPUT": str(rendered),
            },
        )
        assert result.returncode == 0, result.stderr
        text = rendered.read_text(encoding="utf-8")
        assert "@QDRANT_CONTAINER_RUNTIME@" not in text
        assert text.count(runtime) == 3
        assert "EnvironmentFile=-/etc/ovswarm/qdrant.env" in text
        other_runtime = "/usr/bin/podman" if runtime.endswith("docker") else "/usr/bin/docker"
        assert other_runtime not in text
