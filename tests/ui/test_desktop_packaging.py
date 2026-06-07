from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"


def test_desktop_package_declares_electron_packaging_targets():
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))

    assert package["main"] == "main.js"
    assert package["scripts"]["start"] == "electron ."
    assert package["scripts"]["check"] == "node --check main.js && node --check preload.js"
    assert package["scripts"]["package"] == "electron-builder --dir"
    assert package["scripts"]["dist"] == "electron-builder"
    assert "js-yaml" in package["dependencies"]
    assert "electron" in package["devDependencies"]
    assert "electron-builder" in package["devDependencies"]
    assert package["build"]["files"] == [
        "main.js",
        "preload.js",
        "README.md",
        "profiles.example.json",
        "node_modules/**/*",
    ]
    assert package["build"]["extraResources"] == [
        {
            "from": "../src/mac/ui",
            "to": "ui",
            "filter": ["index.html", "app.js", "dashboard_api.js", "styles.css"],
        }
    ]
    assert package["build"]["mac"]["target"] == ["dmg", "zip"]


def test_desktop_package_is_not_a_root_node_toolchain():
    assert not (ROOT / "package.json").exists()
    assert not (ROOT / "package-lock.json").exists()
    assert (DESKTOP / "package.json").exists()
    assert (DESKTOP / "package-lock.json").exists()


def test_desktop_main_owns_ssh_proxy_and_service_tunnels():
    main = (DESKTOP / "main.js").read_text(encoding="utf-8")
    preload = (DESKTOP / "preload.js").read_text(encoding="utf-8")

    assert "startProxyServer" in main
    assert "startSshTunnel" in main
    assert "BatchMode=yes" in main
    assert "ExitOnForwardFailure=yes" in main
    assert "serviceTunnels" in main
    assert "profileToken" in main
    assert "~/.mac/fleets.yaml" in main
    assert "MAC_API_TOKEN__" in main
    assert "MAC_DEPLOY_HUB_TOKEN__" in main
    assert "tokenSourcesForTarget" in main
    assert "tokenSourceId" in main
    assert "mac-dashboard:targets" in main
    assert "mac-dashboard:select-target" in main
    assert "mac-dashboard:disconnect" in main
    assert "connected" in main
    assert "process.argv.slice(1)" in main
    assert "serveLocalUi" in main
    assert "process.resourcesPath" in main
    assert "/ui/assets/" in main
    assert "contextBridge.exposeInMainWorld" in preload
    assert "macDashboard" in preload
    assert "targets()" in preload
    assert "selectTarget" in preload
    assert "disconnect" in preload


def test_desktop_profiles_show_direct_ssh_and_local_modes():
    profiles = json.loads((DESKTOP / "profiles.example.json").read_text(encoding="utf-8"))

    assert profiles["directRemote"]["apiUrl"].startswith("https://")
    assert profiles["sshHub"]["ssh"]["target"]
    assert profiles["sshHub"]["serviceTunnels"]["qdrant"]["remotePort"] == 6333
    assert profiles["localDev"]["localServer"]["command"][0] == "uv"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_desktop_main_and_preload_are_valid_javascript():
    subprocess.run(["node", "--check", "main.js"], cwd=DESKTOP, check=True)
    subprocess.run(["node", "--check", "preload.js"], cwd=DESKTOP, check=True)
