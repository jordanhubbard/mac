"""mac-73cz: circuit-breaker on the self-repair task spawn rate.

The 2026-05-27 incident: a worker fails on an upstream provider 429, the
control plane spawns a 'Repair X checkout' task, that repair hits the same
429 and fails, and the next poll spawns another — 10+ identical repair tasks
in one minute. The breaker bounds the spawn rate per repository.
"""

from __future__ import annotations

import types

from mac.services import ControlPlane


def _repo(repo_id: str = "repo-1"):
    """Minimal stand-in carrying just the fields the breaker / ref need."""
    return types.SimpleNamespace(
        id=repo_id,
        name="demo",
        path="/tmp/demo",
        source="git",
        project="demo",
        required_capabilities=[],
        enabled=True,
        poll_interval_seconds=60,
    )


def _spawn_remediation(cp: ControlPlane, repo_id: str, count: int) -> None:
    for _ in range(count):
        cp.create_task(
            "Repair demo checkout before Beads polling",
            metadata={
                "remediation": {
                    "type": "beads_source_refresh",
                    "repository_id": repo_id,
                }
            },
            actor="test",
        )


def test_repair_breaker_bounds_spawn_rate(monkeypatch):
    monkeypatch.setenv("MAC_REPAIR_BREAKER_MAX_SPAWNS", "3")
    monkeypatch.setenv("MAC_REPAIR_BREAKER_WINDOW_SECONDS", "300")
    cp = ControlPlane.in_memory()
    repo = _repo()

    # Under the cap: breaker stays closed.
    _spawn_remediation(cp, repo.id, 2)
    assert cp._beads_remediation_breaker_open(repo) is False

    # Reaching the cap (3 within the window) opens the breaker — no 4th spawn.
    _spawn_remediation(cp, repo.id, 1)
    assert cp._beads_remediation_breaker_open(repo) is True


def test_repair_breaker_is_per_repository(monkeypatch):
    monkeypatch.setenv("MAC_REPAIR_BREAKER_MAX_SPAWNS", "3")
    cp = ControlPlane.in_memory()
    repo_a = _repo("repo-a")
    repo_b = _repo("repo-b")

    _spawn_remediation(cp, repo_a.id, 3)
    # One repo storming doesn't trip the breaker for an unrelated repo.
    assert cp._beads_remediation_breaker_open(repo_a) is True
    assert cp._beads_remediation_breaker_open(repo_b) is False


def test_repair_breaker_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MAC_REPAIR_BREAKER_MAX_SPAWNS", "0")
    cp = ControlPlane.in_memory()
    repo = _repo()
    _spawn_remediation(cp, repo.id, 10)
    # max=0 disables the breaker entirely (escape hatch).
    assert cp._beads_remediation_breaker_open(repo) is False
