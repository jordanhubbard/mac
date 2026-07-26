"""Tests for Phase 1 soul snapshot (pull/edit/push of the agent soul text layer)."""

from __future__ import annotations

import pytest
import yaml

from mac import soul_snapshot as fs
from mac.fleet_ssh import FleetSshSpec

# imports relocated from test_soul_snapshot_edges.py
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from mac import soul_snapshot as snapshot


class FakeTransport:
    """In-memory transport: {target: {relpath: content}}."""

    def __init__(self, store):
        self.store = store
        self.backups = []
        self.writes = []

    def read_text(self, target, relpath):
        return self.store.get(target, {}).get(relpath)

    def backup(self, target, relpath, *, stamp):
        if relpath in self.store.get(target, {}):
            bp = "%s.bak.%s" % (relpath, stamp)
            self.backups.append((target, relpath, bp))
            return bp
        return None

    def write_text(self, target, relpath, content):
        self.store.setdefault(target, {})[relpath] = content
        self.writes.append((target, relpath))

    def stat(self, target, relpath, *, checksum=False):
        content = self.store.get(target, {}).get(relpath)
        if content is None:
            return None
        meta = {"present": True, "bytes": len(content.encode("utf-8")), "mtime": 1234567890}
        if checksum:
            meta["sha256"] = fs._sha256(content)
        return meta


def _agents():
    return [("natasha", "u@sparky"), ("rocky", "u@do1")]


# -- roster -----------------------------------------------------------------


def test_load_fleet_agents():
    cfg = {"fleets": {"rocky": {"agents": [
        {"name": "rocky", "target": "u@do1", "os": "linux"},
        {"name": "natasha", "target": "u@sparky"},
        {"name": "", "target": "skip"},
    ]}}}
    assert fs.load_fleet_agents(cfg, "rocky") == [("rocky", "u@do1"), ("natasha", "u@sparky")]
    with pytest.raises(KeyError):
        fs.load_fleet_agents(cfg, "ghost")


def test_ssh_transport_uses_canonical_route(monkeypatch, tmp_path):
    seen = {}

    class Result:
        returncode = 0
        stdout = "soul"
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Result()

    route = FleetSshSpec(
        fleet="rocky",
        fleet_name="mac",
        agent="rocky",
        target="ops@hub",
        port=2201,
        proxy_jump="ops@bastion:2222",
        identity_file=str(tmp_path / "id"),
        identity_ref=None,
        known_hosts_file=str(tmp_path / "known_hosts"),
        host_key_policy="strict",
        host_key_fingerprint=None,
        host_ca=None,
        supervisor="systemd",
        os_kind="linux",
        control_port=8789,
    )
    monkeypatch.setattr(fs.subprocess, "run", fake_run)

    assert fs.SSHTransport(routes={"ops@hub": route}).read_text(
        "ops@hub", "SOUL.md"
    ) == "soul"
    assert seen["argv"][:3] == ["ssh", "-F", "/dev/null"]
    assert "ProxyJump=ops@bastion:2222" in seen["argv"]


# -- pull -------------------------------------------------------------------


def test_pull_writes_tree_manifest_and_sha(tmp_path):
    t = FakeTransport({
        "u@sparky": {"USER.md": "I am Natasha", "MEMORY.md": "notes"},  # no SOUL.md
        "u@do1": {"SOUL.md": "rocky soul"},
    })
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="rocky", pulled_at="T0")
    assert (tmp_path / "agents/natasha/soul/USER.md").read_text() == "I am Natasha"
    assert (tmp_path / "agents/rocky/soul/SOUL.md").read_text() == "rocky soul"
    nm = manifest["agents"]["natasha"]
    assert nm["target"] == "u@sparky"
    assert nm["files"]["USER.md"]["present"] is True
    assert nm["files"]["USER.md"]["sha256"] == fs._sha256("I am Natasha")
    assert nm["files"]["SOUL.md"] == {"present": False}
    assert manifest["schema"] == fs.SNAPSHOT_SCHEMA and manifest["fleet"] == "rocky"


# -- memory references (Phase 2) --------------------------------------------


def test_pull_captures_memory_refs_not_content(tmp_path):
    t = FakeTransport({
        "u@sparky": {"USER.md": "n", "state.db": "x" * 5000, "memory_store.db": "y" * 99},
        "u@do1": {"SOUL.md": "r"},
    })
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="rocky", pulled_at="T0")
    mem = manifest["agents"]["natasha"]["memory"]
    assert mem["state.db"]["present"] is True
    assert mem["state.db"]["bytes"] == 5000
    assert "mtime" in mem["state.db"]
    assert "sha256" not in mem["state.db"]
    assert mem["memory_store.db"]["bytes"] == 99
    assert manifest["agents"]["rocky"]["memory"]["state.db"] == {"present": False}
    assert not (tmp_path / "agents/natasha/soul/state.db").exists()
    assert not (tmp_path / "agents/natasha/memory").exists()


def test_pull_memory_checksum_opt_in(tmp_path):
    t = FakeTransport({"u@sparky": {"state.db": "blob"}, "u@do1": {}})
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="rocky", pulled_at="T0",
                                memory_checksum=True)
    assert manifest["agents"]["natasha"]["memory"]["state.db"]["sha256"] == fs._sha256("blob")


# -- hub persona + mood capture (Phase 3) -----------------------------------


class FakeHub:
    def __init__(self, personas, moods):
        self._personas = personas        # list of dicts
        self._moods = moods              # {agent_id: mood dict or None}

    def list_personas(self):
        return self._personas

    def get_current_mood(self, agent_id):
        return self._moods.get(agent_id)


def test_persona_for_matches_name_conventions():
    personas = [{"name": "persona_natasha"}, {"name": "rocky"}]
    assert fs._persona_for(personas, "natasha")["name"] == "persona_natasha"
    assert fs._persona_for(personas, "rocky")["name"] == "rocky"
    assert fs._persona_for(personas, "ghost") is None


def test_capture_hub_state_writes_persona_and_mood(tmp_path):
    hub = FakeHub(
        personas=[{"name": "persona_natasha", "soul_ref": "s", "metadata": {"x": 1}}],
        moods={"agent_natasha": {"label": "focused", "intensity": 3}, "agent_rocky": None},
    )
    out = fs.capture_hub_state(hub, [("natasha", "agent_natasha"), ("rocky", "agent_rocky")],
                               tmp_path, pulled_at="T0")
    import yaml
    persona = yaml.safe_load((tmp_path / "agents/natasha/persona.yaml").read_text())
    assert persona["name"] == "persona_natasha"
    mood = yaml.safe_load((tmp_path / "agents/natasha/mood.yaml").read_text())
    assert mood["label"] == "focused"
    assert out["agents"]["natasha"]["persona"]["present"] is True
    assert out["agents"]["natasha"]["mood"]["present"] is True
    # rocky: no persona match, no mood -> referenced absent, no files
    assert out["agents"]["rocky"]["persona"]["present"] is False
    assert out["agents"]["rocky"]["mood"]["present"] is False
    assert not (tmp_path / "agents/rocky/mood.yaml").exists()


def test_capture_hub_state_survives_hub_errors(tmp_path):
    class BoomHub:
        def list_personas(self): raise RuntimeError("hub down")
        def get_current_mood(self, a): raise RuntimeError("hub down")
    out = fs.capture_hub_state(BoomHub(), [("natasha", "agent_natasha")], tmp_path, pulled_at="T0")
    assert out["agents"]["natasha"]["persona"]["present"] is False
    assert out["agents"]["natasha"]["mood"]["present"] is False


# -- push: diff / dry-run / apply -------------------------------------------


def _pulled(tmp_path, store):
    t = FakeTransport(store)
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="rocky", pulled_at="T0")
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    return manifest


def test_push_dry_run_detects_change_and_writes_nothing(tmp_path):
    store = {"u@sparky": {"USER.md": "old"}, "u@do1": {"SOUL.md": "rsoul"}}
    manifest = _pulled(tmp_path, store)
    (tmp_path / "agents/natasha/soul/USER.md").write_text("NEW natasha")
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=True)
    by = {(c.agent, c.relpath): c for c in res.changes}
    assert by[("natasha", "USER.md")].status == "changed"
    assert by[("rocky", "SOUL.md")].status == "unchanged"
    assert t.writes == [] and t.backups == []
    assert store["u@sparky"]["USER.md"] == "old"


def test_push_apply_backs_up_and_writes_only_changed(tmp_path):
    store = {"u@sparky": {"USER.md": "old"}, "u@do1": {"SOUL.md": "rsoul"}}
    manifest = _pulled(tmp_path, store)
    (tmp_path / "agents/natasha/soul/USER.md").write_text("NEW natasha")
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=False)
    assert store["u@sparky"]["USER.md"] == "NEW natasha"
    assert ("u@sparky", "USER.md", "USER.md.bak.S1") in t.backups
    assert t.writes == [("u@sparky", "USER.md")]
    applied = [c for c in res.changes if c.applied]
    assert len(applied) == 1 and applied[0].agent == "natasha"


def test_push_new_file_when_absent_remote(tmp_path):
    store = {"u@sparky": {"USER.md": "x"}, "u@do1": {}}
    src = {"u@sparky": {"USER.md": "x"}, "u@do1": {"SOUL.md": "fresh"}}
    manifest = _pulled(tmp_path, src)
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=False)
    by = {(c.agent, c.relpath): c for c in res.changes}
    assert by[("rocky", "SOUL.md")].status == "new"
    assert store["u@do1"]["SOUL.md"] == "fresh"
    assert by[("rocky", "SOUL.md")].backup_path is None


def test_push_only_agents_scopes(tmp_path):
    store = {"u@sparky": {"USER.md": "old"}, "u@do1": {"SOUL.md": "rsoul"}}
    manifest = _pulled(tmp_path, store)
    (tmp_path / "agents/natasha/soul/USER.md").write_text("NEW")
    (tmp_path / "agents/rocky/soul/SOUL.md").write_text("NEWROCKY")
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=False, only_agents=["natasha"])
    assert store["u@sparky"]["USER.md"] == "NEW"
    assert store["u@do1"]["SOUL.md"] == "rsoul"
    assert all(c.agent == "natasha" for c in res.changes)


# --- relocated from test_soul_snapshot_edges.py (coverage companion folded in) ---

def _result(returncode: int, stdout: str='', stderr: str='') -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_ssh_transport_read_and_fallback_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = snapshot.SSHTransport(ssh_extra=['-v'])
    assert transport._argv('host', 'echo ok')[-2:] == ['host', 'echo ok']
    assert transport._remote('SOUL.md') == '"$HOME/.hermes/SOUL.md"'
    replies = iter([_result(7), _result(2, stderr='denied'), _result(0, 'soul')])
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *args, **kwargs: next(replies))
    assert transport.read_text('host', 'SOUL.md') is None
    with pytest.raises(RuntimeError, match='denied'):
        transport.read_text('host', 'SOUL.md')
    assert transport.read_text('host', 'SOUL.md') == 'soul'


def test_ssh_transport_backup_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = snapshot.SSHTransport()
    replies = iter([_result(2, stderr='backup denied'), _result(0), _result(0, 'COPIED\n'), _result(3, stderr='write denied'), _result(0)])
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *args, **kwargs: next(replies))
    with pytest.raises(RuntimeError, match='backup denied'):
        transport.backup('host', 'USER.md', stamp='T')
    assert transport.backup('host', 'USER.md', stamp='T') is None
    assert transport.backup('host', 'USER.md', stamp='T') == 'USER.md.bak.T'
    with pytest.raises(RuntimeError, match='write denied'):
        transport.write_text('host', 'USER.md', 'text')
    transport.write_text('host', 'USER.md', 'text')


def test_ssh_transport_stat_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = snapshot.SSHTransport()
    replies = iter([_result(7), _result(4, stderr='stat denied'), _result(0, '12 34 abc123\n'), _result(0, 'malformed\n')])
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *args, **kwargs: next(replies))
    assert transport.stat('host', 'state.db') is None
    with pytest.raises(RuntimeError, match='stat denied'):
        transport.stat('host', 'state.db')
    assert transport.stat('host', 'state.db', checksum=True) == {'present': True, 'bytes': 12, 'mtime': 34, 'sha256': 'abc123'}
    assert transport.stat('host', 'state.db') == {'present': True}


def test_plain_conversion_and_push_result_filter() -> None:

    class Dictish:

        def to_dict(self) -> dict[str, int]:
            return {'x': 1}

    class IterablePairs:

        def __iter__(self):
            return iter([('y', 2)])
    opaque = object()
    assert snapshot._as_plain(Dictish()) == {'x': 1}
    assert snapshot._as_plain(IterablePairs()) == {'y': 2}
    assert snapshot._as_plain(opaque) is opaque
    result = snapshot.PushResult(changes=[snapshot.FileChange('a', 'h', 'SOUL.md', 'new'), snapshot.FileChange('a', 'h', 'USER.md', 'unchanged')])
    assert [item.relpath for item in result.to_apply] == ['SOUL.md']


def test_plan_push_skips_missing_local_snapshot(tmp_path: Path) -> None:

    class Transport:

        def read_text(self, *args: Any) -> str:
            raise AssertionError('missing local file must not read remote')
    result = snapshot.plan_and_push(tmp_path, {'agents': {'agent': {'target': 'host', 'files': {'SOUL.md': {'present': True}}}}}, Transport(), stamp='T')
    assert result.changes == []


def test_ssh_transport_list_dir_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_dir returns relative paths stripped of the ~/.hermes/ prefix."""
    transport = snapshot.SSHTransport()
    output = '/home/agent/.hermes/SOUL.md\n/home/agent/.hermes/USER.md\n/home/agent/.hermes/subdir/file.txt\n'
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(0, output))
    paths = transport.list_dir('host')
    assert len(paths) == 3
    assert 'SOUL.md' in paths
    assert 'USER.md' in paths
    assert 'subdir/file.txt' in paths


def test_ssh_transport_list_dir_non_zero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_dir raises RuntimeError on a non-zero SSH exit code."""
    transport = snapshot.SSHTransport()
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(1, stderr='connection refused'))
    with pytest.raises(RuntimeError, match='connection refused'):
        transport.list_dir('host')


def test_hermes_salvage_audit_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """hermes_salvage_audit returns the v1 manifest on success."""
    transport = snapshot.SSHTransport()
    output = '/home/agent/.hermes/SOUL.md\n/home/agent/.hermes/USER.md\n'
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(0, output))
    result = snapshot.hermes_salvage_audit('agent_test', 'host', transport, audited_at='2026-01-01T00:00:00Z')
    assert result['schema'] == 'mac.hermes_salvage_audit.v1'
    assert result['agent'] == 'agent_test'
    assert result['target'] == 'host'
    assert result['audited_at'] == '2026-01-01T00:00:00Z'
    assert set(result['files']) == {'SOUL.md', 'USER.md'}
    assert result['file_count'] == 2
    assert result['file_count'] == len(result['files'])
    assert result['error'] is None


def test_hermes_salvage_audit_ssh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """hermes_salvage_audit captures RuntimeError from list_dir in the error field."""
    transport = snapshot.SSHTransport()
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(255, stderr='ssh: no route to host'))
    result = snapshot.hermes_salvage_audit('agent_test', 'unreachable', transport, audited_at='2026-01-01T00:00:00Z')
    assert result['schema'] == 'mac.hermes_salvage_audit.v1'
    assert result['agent'] == 'agent_test'
    assert result['target'] == 'unreachable'
    assert result['files'] == []
    assert result['file_count'] == 0
    assert result['error'] is not None
    assert 'no route to host' in result['error']


def test_ssh_transport_list_dir_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_dir returns an empty list when find returns no output."""
    transport = snapshot.SSHTransport()
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(0, ''))
    assert transport.list_dir('host') == []


def test_ssh_transport_list_dir_skips_blank_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_dir silently skips empty/blank lines in find output (line 181 branch)."""
    transport = snapshot.SSHTransport()
    output = '/home/agent/.hermes/SOUL.md\n\n  \n/home/agent/.hermes/USER.md\n'
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(0, output))
    paths = transport.list_dir('host')
    assert paths == ['SOUL.md', 'USER.md']


def test_ssh_transport_list_dir_passthrough_without_hermes_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_dir passes through a path unchanged when the .hermes/ prefix is absent (line 187 branch)."""
    transport = snapshot.SSHTransport()
    output = '/some/other/path/file.txt\n'
    monkeypatch.setattr(snapshot.subprocess, 'run', lambda *a, **kw: _result(0, output))
    paths = transport.list_dir('host')
    assert paths == ['/some/other/path/file.txt']
