"""Standalone helper/client installation, including actual repeat install calls."""

import json
from pathlib import Path
import sys
import types

import pytest

from mac import world4d_client as client
from mac import world4d_service as service


def install_context(tmp_path, monkeypatch):
    import yaml

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": {}, "model": "unchanged"}))
    skill = home / "skills/world4d-coordination/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("shared")
    refresh = tmp_path / "refresh"
    refresh.write_text("bin/mac-world4d-presence")
    manifest = {
        "realm_id": "r",
        "world": "fleet",
        "backend": "prototype",
        "agent": "agent_bullwinkle",
        "command": ["adapter"],
        "hermes_home": str(home),
        "manifest_path": str(tmp_path / "declared" / "binding.json"),
        "client_module": "manager.py",
        "python": sys.executable,
        "hook": str(tmp_path / "hook"),
        "refresh_launcher": str(refresh),
        "work_jobs": [],
        "report_jobs": [],
        "monitor_url": "http://example.invalid/snapshot",
    }
    jobs = types.ModuleType("cron.jobs")
    jobs.get_job = lambda _id: None
    jobs.load_jobs = lambda: []
    jobs.update_job = lambda *_a: True
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", jobs)
    monkeypatch.setattr(client, "verify_realm", lambda _manifest: None)
    return manifest


def test_client_relative_helper_follows_manifest_and_real_reinstall(tmp_path, monkeypatch):
    manifest = install_context(tmp_path, monkeypatch)
    unrelated = tmp_path / "other-cwd"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    source = Path(client.__file__)
    client.install(manifest)
    target = Path(manifest["manifest_path"]).parent / "manager.py"
    helper = target.with_name("world4d_owned_child.py")
    assert target.read_bytes() == source.read_bytes()
    assert helper.read_bytes() == source.with_name("world4d_owned_child.py").read_bytes()
    assert not (unrelated / "world4d_owned_child.py").exists()
    # Execute the install FUNCTION again with its actual installed source path;
    # both payload and helper are now source==target, rather than a tautology.
    monkeypatch.setattr(client, "__file__", str(target))
    client.install(manifest)
    assert target.is_file() and helper.is_file()


def test_client_absolute_target_is_used(tmp_path, monkeypatch):
    manifest = install_context(tmp_path, monkeypatch)
    manifest["client_module"] = str(tmp_path / "absolute" / "manager.py")
    client.install(manifest)
    assert (tmp_path / "absolute/world4d_owned_child.py").is_file()


def test_validation_failure_writes_no_client_or_configuration(tmp_path, monkeypatch):
    manifest = install_context(tmp_path, monkeypatch)
    manifest["work_jobs"] = ["missing"]
    original = (Path(manifest["hermes_home"]) / "config.yaml").read_bytes()
    with pytest.raises(ValueError, match="managed job missing"):
        client.install(manifest)
    assert not (tmp_path / "declared").exists()
    assert (Path(manifest["hermes_home"]) / "config.yaml").read_bytes() == original


def test_prepare_copies_helper_and_reinstall_from_installed_service(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    source = tmp_path / "external"
    source.mkdir()
    for name in ("world4d.py", "world4d_daemon.py", "world4d_slack_post.py"):
        (source / name).write_text("# external fixture\n")
    environment = tmp_path / "environment.json"
    environment.write_text("{}")
    environment.chmod(0o600)
    config = {
        "root": str(root),
        "source": str(source),
        "python": sys.executable,
        "environment_file": str(environment),
        "monitor_port": 39001,
        "worlds": [{"name": "fleet", "port": 39002}],
        "home": str(tmp_path),
        "user": "example",
    }
    path = tmp_path / "input.json"
    path.write_text(json.dumps(config))
    service.prepare(path)
    helper = root / "world4d_owned_child.py"
    assert (
        helper.read_bytes()
        == Path(service.__file__).with_name("world4d_owned_child.py").read_bytes()
    )
    monkeypatch.setattr(service, "__file__", str(root / "service.py"))
    service.prepare(path)
    assert helper.is_file() and (root / "world4d_presence.py").is_file()
