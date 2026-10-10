"""mac.fleet_context_service: keep the periodic fleet-context refresh installed."""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path

from mac import fleet_context_service as svc

ROOT = Path(__file__).resolve().parents[1]


class Runner:
    """Records commands; ``sudo -n install`` copies, as the real one would."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = tuple(fail)

    def __call__(self, cmd):
        cmd = list(cmd)
        self.calls.append(" ".join(cmd))
        joined = " ".join(cmd)
        if any(f in joined for f in self.fail):
            return 1, "boom: %s" % joined
        if cmd[:3] == ["sudo", "-n", "install"]:
            shutil.copyfile(cmd[-2], cmd[-1])
        return 0, ""


def _linux(tmp_path, runner):
    return svc.install(
        source=ROOT,
        mac_home=Path("/home/op/.mac"),
        system="Linux",
        unit_dir=tmp_path,
        systemd_running=True,
        runner=runner,
    )


def test_linux_installs_the_units_for_this_user_and_runs_one_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.getpass, "getuser", lambda: "op")
    runner = Runner()

    result = _linux(tmp_path, runner)

    assert result == {
        "status": "ok",
        "units": "updated: mac-fleet-context.service, mac-fleet-context.timer",
        "refresh": "ok",
    }
    service = (tmp_path / "mac-fleet-context.service").read_text()
    assert "User=op" in service and "/home/op/.mac/venv/bin/mac admin fleet refresh-context" in service
    assert "__MAC_" not in service
    assert runner.calls[-4:] == [
        "sudo -n systemctl daemon-reload",
        "sudo -n systemctl reset-failed mac-fleet-context.timer mac-fleet-context.service",
        "sudo -n systemctl enable --now mac-fleet-context.timer",
        "sudo -n systemctl start mac-fleet-context.service",
    ]


def test_linux_rerun_changes_no_units_but_still_resets_a_failed_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.getpass, "getuser", lambda: "op")
    _linux(tmp_path, Runner())
    runner = Runner()

    result = _linux(tmp_path, runner)

    assert result["units"] == "unchanged"
    assert not any(" install " in c or "daemon-reload" in c for c in runner.calls)
    assert "sudo -n systemctl reset-failed mac-fleet-context.timer mac-fleet-context.service" in runner.calls


def test_linux_replaces_a_unit_running_the_removed_command(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.getpass, "getuser", lambda: "op")
    stale = tmp_path / "mac-fleet-context.service"
    stale.write_text("ExecStart=/home/op/.mac/venv/bin/mac fleet refresh-context\n")

    result = _linux(tmp_path, Runner())

    assert result["units"].startswith("updated: mac-fleet-context.service")
    assert "mac admin fleet refresh-context" in stale.read_text()


def test_linux_reports_a_failing_first_refresh_with_the_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(svc.getpass, "getuser", lambda: "op")

    def runner(cmd):
        if cmd[:1] == ["journalctl"]:
            return 0, "line one\nunknown bearer token"
        if "start" in cmd:
            return 1, "Job failed"
        return Runner()(cmd)

    result = _linux(tmp_path, runner)

    assert result["status"] == "error"
    assert result["error"] == "first refresh failed: unknown bearer token"


def test_linux_without_systemd_is_skipped(tmp_path):
    result = svc.install(source=ROOT, system="Linux", systemd_running=False, runner=Runner())
    assert result["status"] == "skipped"


def _darwin(tmp_path, runner):
    return svc.install(
        source=ROOT,
        mac_home=tmp_path / ".mac",
        system="Darwin",
        agents_dir=tmp_path / "LaunchAgents",
        runner=runner,
    )


def test_darwin_replaces_the_obsolete_launcher_and_reloads_the_job(tmp_path):
    launcher = tmp_path / ".mac" / "bin" / "fleet-context"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/bash\nexec "$HOME/.mac/venv/bin/mac" fleet refresh-context\n')
    runner = Runner(fail=["launchctl print"])  # not loaded yet

    result = _darwin(tmp_path, runner)

    assert result == {"status": "ok", "units": "updated: launcher, plist", "refresh": "ok"}
    assert "/mac\" admin fleet refresh-context" in launcher.read_text()
    assert launcher.stat().st_mode & 0o777 == 0o755
    plist = plistlib.loads((tmp_path / "LaunchAgents" / "com.mac.fleet-context.plist").read_bytes())
    assert plist["ProgramArguments"] == [str(launcher)]
    assert plist["StartInterval"] == 180
    assert any(c.startswith("launchctl bootstrap gui/") for c in runner.calls)
    assert runner.calls[-1] == str(launcher)


def test_darwin_rerun_leaves_a_loaded_job_alone(tmp_path):
    _darwin(tmp_path, Runner(fail=["launchctl print"]))
    runner = Runner()

    result = _darwin(tmp_path, runner)

    assert result["units"] == "unchanged"
    assert not any("bootout" in c or "bootstrap" in c for c in runner.calls)


def test_darwin_reloads_a_loaded_job_whose_plist_changed(tmp_path):
    plist = tmp_path / "LaunchAgents" / "com.mac.fleet-context.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("old")
    runner = Runner()

    _darwin(tmp_path, runner)

    bootout = next(i for i, c in enumerate(runner.calls) if "bootout" in c)
    bootstrap = next(i for i, c in enumerate(runner.calls) if "bootstrap" in c)
    assert bootout < bootstrap


def test_main_prints_one_json_line_and_fails_on_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(svc, "install", lambda **_: {"status": "error", "error": "x"})
    assert svc.main(["--source", str(tmp_path)]) == 1
    assert capsys.readouterr().out.strip() == '{"error": "x", "status": "error"}'
