"""soul_seed: a session start queries the soul only when the context is new."""

from __future__ import annotations

import time

from mac.soul_graph import SoulGraph
from mac.soul_seed import format_inject, seed


def _soul(path):
    """One old node about sandboxes, under ten recent ones about deploys, so
    the sandbox node is out of the hot set that seed() treats as familiar."""
    clock = [time.time() - 30 * 86400]
    g = SoulGraph(name="t", clock=lambda: clock[0])
    g.add("Sandbox mounts need the image WorkingDir free.", tags={"openshell"}, node_id="o1")
    clock[0] = time.time()
    for i in range(10):
        g.add("Fleet deploys go through fleet-update, host %d at a time." % i, tags={"deploy"})
    g.save(path)
    return path


def test_no_soul_file_injects_nothing(tmp_path):
    assert seed("anything", soul_path=str(tmp_path / "none.json"))["reason"] == "no soul file"


def test_familiar_context_skips_the_query(tmp_path):
    path = _soul(tmp_path / "soul.json")
    result = seed("fleet deploys fleet-update host", soul_path=str(path))
    assert result["queried"] is False
    assert format_inject(result) is None


def test_novel_context_injects_the_matching_node(tmp_path):
    path = _soul(tmp_path / "soul.json")
    result = seed("sandbox mounts failing workingdir image", soul_path=str(path))
    assert result["queried"] is True
    assert result["inject"][0]["content"].startswith("Sandbox mounts")
    assert format_inject(result).startswith("[soul context]")


def test_the_default_path_is_in_the_agent_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _soul(tmp_path / "soul.json")
    assert seed("sandbox mounts failing workingdir image")["queried"] is True
