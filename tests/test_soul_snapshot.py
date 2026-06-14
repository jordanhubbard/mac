"""Tests for Phase 1 soul snapshot (pull/edit/push of the agent soul text layer)."""

from __future__ import annotations

import pytest
import yaml

from mac import soul_snapshot as fs


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
