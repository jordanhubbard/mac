"""Ownership and compatibility tests for the executor sandbox extraction."""

from __future__ import annotations

import importlib
from pathlib import Path


def test_task_executor_aliases_canonical_sandbox_module() -> None:
    task_executor = importlib.import_module("mac.task_executor")
    executor_sandbox = importlib.import_module("mac.executor_sandbox")

    assert task_executor is executor_sandbox
    assert callable(task_executor.main)


def test_compatibility_monkeypatch_changes_canonical_module(monkeypatch) -> None:
    task_executor = importlib.import_module("mac.task_executor")
    executor_sandbox = importlib.import_module("mac.executor_sandbox")
    sentinel = object()

    monkeypatch.setattr(task_executor, "_openshell_bin", sentinel)

    assert executor_sandbox._openshell_bin is sentinel


def test_generated_sandbox_name_fits_openshells_length_limit(monkeypatch) -> None:
    """openshell rejects sandbox names over 19 chars.

    Live-reproduced: "mac-task-" + 12 hex chars (21 total) failed with
    "name exceeds maximum length (21 > 19)", which then cascaded into every
    later sandbox operation (upload, download, cleanup) failing with
    "sandbox not found" -- the executor never even got a real sandbox to
    fail *in*.
    """
    sandbox = importlib.import_module("mac.executor_sandbox")
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX_NAME", raising=False)

    name = sandbox._sandbox_name()

    assert len(name) <= 19
    assert name.startswith("mac-task-")


def test_coding_agent_preflight_probe_sandbox_name_fits_length_limit() -> None:
    """A second, separate name-generation site with the same 19-char bug.

    "mac-codingcap-<agent>-<12 hex>" was 29-35 chars depending on agent
    (e.g. "mac-codingcap-opencode-...") -- every coding-agent preflight
    failed to even create its probe sandbox with "name exceeds maximum
    length", surfacing as an opaque "probe_failed"/"route verification
    failed" for every configured CLI. Live-reproduced on a real fleet node
    after the primary _sandbox_name() fix was already deployed.
    """
    sandbox = importlib.import_module("mac.executor_sandbox")

    name = sandbox._coding_agent_probe_sandbox_name()

    assert len(name) <= 19


def test_read_only_verifier_sandbox_name_fits_length_limit() -> None:
    """A third name-generation site with the same 19-char bug.

    "<task-sandbox-name>-verify-<8 hex>" derived from an already-shortened
    17-char task sandbox name was still 33+ chars.
    """
    sandbox = importlib.import_module("mac.executor_sandbox")

    name = sandbox._read_only_verifier_sandbox_name()

    assert len(name) <= 19


def test_loopback_urls_are_rewritten_for_the_sandbox_host() -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")

    assert (
        sandbox._rewrite_host_local_url("http://127.0.0.1:8789/v1", "host.openshell.internal")
        == "http://host.openshell.internal:8789/v1"
    )
    assert (
        sandbox._rewrite_host_local_url("https://hub.example.test/v1", "host.openshell.internal")
        == "https://hub.example.test/v1"
    )


def test_workspace_paths_map_only_children_into_the_sandbox(tmp_path: Path) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    workspace = tmp_path / "task-worktree"
    child = workspace / "repo" / "src"
    child.mkdir(parents=True)

    assert sandbox._workspace_basename(workspace) == "task-worktree"
    assert (
        sandbox._sandbox_path_for_workspace_child(workspace, "/sandbox/task", str(child))
        == "/sandbox/task/repo/src"
    )
    assert (
        sandbox._sandbox_path_for_workspace_child(
            workspace, "/sandbox/task", str(tmp_path / "outside")
        )
        is None
    )


def test_reap_orphans_best_effort_applies_by_default(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc

    calls = {}

    def fake_reap(*, openshell_bin, apply):
        calls["openshell_bin"] = openshell_bin
        calls["apply"] = apply
        return {
            "schema": "mac.openshell.sandbox_orphan_reap.v1",
            "dry_run": not apply,
            "scanned": 1,
            "protected": 0,
            "candidates": [{"name": "mac-task-dead"}],
            "deleted": ["mac-task-dead"],
            "failures": [],
        }

    monkeypatch.setattr(gc, "reap_orphaned_task_sandboxes", fake_reap)
    monkeypatch.delenv("MAC_OPENSHELL_REAP_ORPHANS", raising=False)
    monkeypatch.setattr(sandbox, "emit_telemetry", lambda *a, **k: None)

    sandbox._reap_orphaned_task_sandboxes_best_effort("task_1")

    assert calls["apply"] is True


def test_reap_orphans_best_effort_can_be_disabled(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc

    called = {"count": 0}

    def fake_reap(**_kwargs):
        called["count"] += 1
        return {"candidates": [], "deleted": [], "failures": [], "scanned": 0, "protected": 0}

    monkeypatch.setattr(gc, "reap_orphaned_task_sandboxes", fake_reap)
    monkeypatch.setenv("MAC_OPENSHELL_REAP_ORPHANS", "0")

    sandbox._reap_orphaned_task_sandboxes_best_effort()

    assert called["count"] == 0


def test_reap_orphans_best_effort_swallows_errors(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc

    def boom(**_kwargs):
        raise RuntimeError("openshell exploded")

    monkeypatch.setattr(gc, "reap_orphaned_task_sandboxes", boom)
    monkeypatch.delenv("MAC_OPENSHELL_REAP_ORPHANS", raising=False)

    # Best-effort: a reap failure must never propagate into the guarded run.
    sandbox._reap_orphaned_task_sandboxes_best_effort()


def test_sandbox_identity_labels_stamp_task_and_lease(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    monkeypatch.setenv("MAC_TASK_ID", "task_xyz")
    monkeypatch.setenv("MAC_LEASE_ID", "lease_xyz")

    argv = sandbox._sandbox_label_argv("task")

    assert "mac.task.id=task_xyz" in argv
    assert "mac.lease.id=lease_xyz" in argv
    assert "mac.owner=mac" in argv


def test_sandbox_identity_labels_omitted_when_absent(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    monkeypatch.delenv("MAC_TASK_ID", raising=False)
    monkeypatch.delenv("MAC_LEASE_ID", raising=False)

    argv = sandbox._sandbox_label_argv("task")

    assert not any(a.startswith("mac.task.id=") for a in argv)
    assert not any(a.startswith("mac.lease.id=") for a in argv)


def test_lease_reconcile_best_effort_uses_hub_lookup(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc
    import mac.executor_hub_io as hub_io

    monkeypatch.delenv("MAC_OPENSHELL_RECONCILE_LEASES", raising=False)
    monkeypatch.setattr(hub_io, "_hub_env", lambda: ("http://hub", "token"))
    monkeypatch.setattr(hub_io, "_hub_get", lambda path: {"task": {"state": "completed"}})
    monkeypatch.setattr(sandbox, "emit_telemetry", lambda *a, **k: None)

    seen = {}

    def fake_reconcile(lookup_task, *, openshell_bin, apply):
        seen["apply"] = apply
        seen["task"] = lookup_task("task_done")
        return {
            "schema": "mac.openshell.sandbox_lease_reconcile.v1",
            "dry_run": not apply,
            "scanned": 1,
            "protected": 0,
            "candidates": [{"name": "mac-task-done"}],
            "deleted": ["mac-task-done"],
            "failures": [],
        }

    monkeypatch.setattr(gc, "reconcile_task_sandboxes_from_lease_authority", fake_reconcile)

    sandbox._reconcile_task_sandboxes_from_lease_authority_best_effort("task_1")

    assert seen["apply"] is True
    assert seen["task"] == {"state": "completed"}


def test_lease_reconcile_best_effort_skips_without_hub(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc
    import mac.executor_hub_io as hub_io

    monkeypatch.delenv("MAC_OPENSHELL_RECONCILE_LEASES", raising=False)
    monkeypatch.setattr(hub_io, "_hub_env", lambda: ("", ""))

    called = {"count": 0}

    def fake_reconcile(*_a, **_k):
        called["count"] += 1
        return {"candidates": [], "deleted": [], "failures": [], "scanned": 0, "protected": 0}

    monkeypatch.setattr(gc, "reconcile_task_sandboxes_from_lease_authority", fake_reconcile)

    sandbox._reconcile_task_sandboxes_from_lease_authority_best_effort()

    assert called["count"] == 0


def test_lease_reconcile_best_effort_can_be_disabled(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc

    called = {"count": 0}

    def fake_reconcile(*_a, **_k):
        called["count"] += 1
        return {"candidates": [], "deleted": [], "failures": [], "scanned": 0, "protected": 0}

    monkeypatch.setattr(gc, "reconcile_task_sandboxes_from_lease_authority", fake_reconcile)
    monkeypatch.setenv("MAC_OPENSHELL_RECONCILE_LEASES", "0")

    sandbox._reconcile_task_sandboxes_from_lease_authority_best_effort()

    assert called["count"] == 0


def test_lease_reconcile_best_effort_swallows_errors(monkeypatch) -> None:
    sandbox = importlib.import_module("mac.executor_sandbox")
    import mac.openshell_sandbox_gc as gc
    import mac.executor_hub_io as hub_io

    monkeypatch.delenv("MAC_OPENSHELL_RECONCILE_LEASES", raising=False)
    monkeypatch.setattr(hub_io, "_hub_env", lambda: ("http://hub", "token"))
    monkeypatch.setattr(hub_io, "_hub_get", lambda path: None)

    def boom(*_a, **_k):
        raise RuntimeError("openshell exploded")

    monkeypatch.setattr(gc, "reconcile_task_sandboxes_from_lease_authority", boom)

    # Best-effort: a reconcile failure must never propagate into the guarded run.
    sandbox._reconcile_task_sandboxes_from_lease_authority_best_effort()


def test_coding_agent_sandbox_which_declares_every_reviewed_cli() -> None:
    """Regression for a stale allow-list that silently excluded two shipped CLIs.

    coding_agent_sandbox_which is a DECLARED inventory, not a live probe (its
    own docstring). ``deploy/openshell/mac-hermes.Containerfile`` installs
    opencode and pi at the same standard PATH locations as claude/codex/cursor
    and its build gates on ``command -v opencode`` / ``pi --version`` -- so a
    working opencode/pi is a proven property of every published sandbox
    image. Before this fix the frozenset omitted them, so routing rejected
    opencode as "not on PATH" before any real in-sandbox preflight ran, even
    though opencode is coding_agent.AGENT_PRIORITY's first choice (observed
    live on the fleet 2026-09-03: bullwinkle and natasha both failed every
    task -- including read-only ones -- because claude/codex/cursor all had
    real credential problems and opencode was rejected outright rather than
    tried).
    """
    sandbox = importlib.import_module("mac.executor_sandbox")
    coding_agent = importlib.import_module("mac.coding_agent")

    for name in coding_agent.AGENT_PRIORITY:
        assert sandbox.coding_agent_sandbox_which(name) == name, (
            "%s is a reviewed coding agent but is missing from "
            "_SANDBOX_CODING_AGENT_BINARIES" % name
        )
    assert sandbox.coding_agent_sandbox_which("not-a-real-cli") is None
