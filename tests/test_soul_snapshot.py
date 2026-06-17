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
    return [("hostc", "devuser@hostf"), ("hosta", "devuser@hosta")]


# -- roster -----------------------------------------------------------------


def test_load_fleet_agents():
    cfg = {"fleets": {"hosta": {"agents": [
        {"name": "hosta", "target": "devuser@hosta", "os": "linux"},
        {"name": "hostc", "target": "devuser@hostf"},
        {"name": "", "target": "skip"},
    ]}}}
    assert fs.load_fleet_agents(cfg, "hosta") == [("hosta", "devuser@hosta"), ("hostc", "devuser@hostf")]
    with pytest.raises(KeyError):
        fs.load_fleet_agents(cfg, "ghost")


# -- pull -------------------------------------------------------------------


def test_pull_writes_tree_manifest_and_sha(tmp_path):
    t = FakeTransport({
        "devuser@hostf": {"USER.md": "I am Hostc", "MEMORY.md": "notes"},  # no SOUL.md
        "devuser@hosta": {"SOUL.md": "hosta soul"},
    })
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="hosta", pulled_at="T0")
    assert (tmp_path / "agents/hostc/soul/USER.md").read_text() == "I am Hostc"
    assert (tmp_path / "agents/hosta/soul/SOUL.md").read_text() == "hosta soul"
    nm = manifest["agents"]["hostc"]
    assert nm["target"] == "devuser@hostf"
    assert nm["files"]["USER.md"]["present"] is True
    assert nm["files"]["USER.md"]["sha256"] == fs._sha256("I am Hostc")
    assert nm["files"]["SOUL.md"] == {"present": False}
    assert manifest["schema"] == fs.SNAPSHOT_SCHEMA and manifest["fleet"] == "hosta"


# -- memory references (Phase 2) --------------------------------------------


def test_pull_captures_memory_refs_not_content(tmp_path):
    t = FakeTransport({
        "devuser@hostf": {"USER.md": "n", "state.db": "x" * 5000, "memory_store.db": "y" * 99},
        "devuser@hosta": {"SOUL.md": "r"},
    })
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="hosta", pulled_at="T0")
    mem = manifest["agents"]["hostc"]["memory"]
    assert mem["state.db"]["present"] is True
    assert mem["state.db"]["bytes"] == 5000
    assert "mtime" in mem["state.db"]
    assert "sha256" not in mem["state.db"]
    assert mem["memory_store.db"]["bytes"] == 99
    assert manifest["agents"]["hosta"]["memory"]["state.db"] == {"present": False}
    assert not (tmp_path / "agents/hostc/soul/state.db").exists()
    assert not (tmp_path / "agents/hostc/memory").exists()


def test_pull_memory_checksum_opt_in(tmp_path):
    t = FakeTransport({"devuser@hostf": {"state.db": "blob"}, "devuser@hosta": {}})
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="hosta", pulled_at="T0",
                                memory_checksum=True)
    assert manifest["agents"]["hostc"]["memory"]["state.db"]["sha256"] == fs._sha256("blob")


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
    personas = [{"name": "persona_hostc"}, {"name": "hosta"}]
    assert fs._persona_for(personas, "hostc")["name"] == "persona_hostc"
    assert fs._persona_for(personas, "hosta")["name"] == "hosta"
    assert fs._persona_for(personas, "ghost") is None


def test_capture_hub_state_writes_persona_and_mood(tmp_path):
    hub = FakeHub(
        personas=[{"name": "persona_hostc", "soul_ref": "s", "metadata": {"x": 1}}],
        moods={"agent_hostc": {"label": "focused", "intensity": 3}, "agent_hosta": None},
    )
    out = fs.capture_hub_state(hub, [("hostc", "agent_hostc"), ("hosta", "agent_hosta")],
                               tmp_path, pulled_at="T0")
    import yaml
    persona = yaml.safe_load((tmp_path / "agents/hostc/persona.yaml").read_text())
    assert persona["name"] == "persona_hostc"
    mood = yaml.safe_load((tmp_path / "agents/hostc/mood.yaml").read_text())
    assert mood["label"] == "focused"
    assert out["agents"]["hostc"]["persona"]["present"] is True
    assert out["agents"]["hostc"]["mood"]["present"] is True
    # hosta: no persona match, no mood -> referenced absent, no files
    assert out["agents"]["hosta"]["persona"]["present"] is False
    assert out["agents"]["hosta"]["mood"]["present"] is False
    assert not (tmp_path / "agents/hosta/mood.yaml").exists()


def test_capture_hub_state_survives_hub_errors(tmp_path):
    class BoomHub:
        def list_personas(self): raise RuntimeError("hub down")
        def get_current_mood(self, a): raise RuntimeError("hub down")
    out = fs.capture_hub_state(BoomHub(), [("hostc", "agent_hostc")], tmp_path, pulled_at="T0")
    assert out["agents"]["hostc"]["persona"]["present"] is False
    assert out["agents"]["hostc"]["mood"]["present"] is False


# -- push: diff / dry-run / apply -------------------------------------------


def _pulled(tmp_path, store):
    t = FakeTransport(store)
    manifest = fs.pull_snapshot(_agents(), tmp_path, t, fleet="hosta", pulled_at="T0")
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    return manifest


def test_push_dry_run_detects_change_and_writes_nothing(tmp_path):
    store = {"devuser@hostf": {"USER.md": "old"}, "devuser@hosta": {"SOUL.md": "rsoul"}}
    manifest = _pulled(tmp_path, store)
    (tmp_path / "agents/hostc/soul/USER.md").write_text("NEW hostc")
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=True)
    by = {(c.agent, c.relpath): c for c in res.changes}
    assert by[("hostc", "USER.md")].status == "changed"
    assert by[("hosta", "SOUL.md")].status == "unchanged"
    assert t.writes == [] and t.backups == []
    assert store["devuser@hostf"]["USER.md"] == "old"


def test_push_apply_backs_up_and_writes_only_changed(tmp_path):
    store = {"devuser@hostf": {"USER.md": "old"}, "devuser@hosta": {"SOUL.md": "rsoul"}}
    manifest = _pulled(tmp_path, store)
    (tmp_path / "agents/hostc/soul/USER.md").write_text("NEW hostc")
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=False)
    assert store["devuser@hostf"]["USER.md"] == "NEW hostc"
    assert ("devuser@hostf", "USER.md", "USER.md.bak.S1") in t.backups
    assert t.writes == [("devuser@hostf", "USER.md")]
    applied = [c for c in res.changes if c.applied]
    assert len(applied) == 1 and applied[0].agent == "hostc"


def test_push_new_file_when_absent_remote(tmp_path):
    store = {"devuser@hostf": {"USER.md": "x"}, "devuser@hosta": {}}
    src = {"devuser@hostf": {"USER.md": "x"}, "devuser@hosta": {"SOUL.md": "fresh"}}
    manifest = _pulled(tmp_path, src)
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=False)
    by = {(c.agent, c.relpath): c for c in res.changes}
    assert by[("hosta", "SOUL.md")].status == "new"
    assert store["devuser@hosta"]["SOUL.md"] == "fresh"
    assert by[("hosta", "SOUL.md")].backup_path is None


def test_push_only_agents_scopes(tmp_path):
    store = {"devuser@hostf": {"USER.md": "old"}, "devuser@hosta": {"SOUL.md": "rsoul"}}
    manifest = _pulled(tmp_path, store)
    (tmp_path / "agents/hostc/soul/USER.md").write_text("NEW")
    (tmp_path / "agents/hosta/soul/SOUL.md").write_text("NEWHOSTA")
    t = FakeTransport(store)
    res = fs.plan_and_push(tmp_path, manifest, t, stamp="S1", dry_run=False, only_agents=["hostc"])
    assert store["devuser@hostf"]["USER.md"] == "NEW"
    assert store["devuser@hosta"]["SOUL.md"] == "rsoul"
    assert all(c.agent == "hostc" for c in res.changes)
