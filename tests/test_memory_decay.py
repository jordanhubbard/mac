"""dream-04: salience-aware memory decay (forgetting + bloat control)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from mac.services import ControlPlane


def _add(cp, record_type, *, age_days, content="x", subject_type="project", subject_id="demo"):
    rec = cp.add_memory(None, subject_type, subject_id, record_type, content, None, "test")
    # backdate created_at to simulate age
    old = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat(timespec="microseconds")
    cp.store.execute("UPDATE memory_records SET created_at = ? WHERE id = ?", (old, rec.id))
    return rec


def test_decay_dry_run_reports_but_deletes_nothing():
    cp = ControlPlane.in_memory()
    _add(cp, "session_log", age_days=200)            # stale + transient → forgettable
    _add(cp, "deployment_learning:demo", age_days=200)  # curated → protected
    _add(cp, "beads_memory:k", age_days=200)         # curated → protected
    _add(cp, "session_log", age_days=10)             # recent → kept

    report = cp.decay_memory(ttl_days=90, dry_run=True)
    assert report["dry_run"] is True
    assert report["forgettable"] == 1            # only the stale session_log
    assert report["deleted"] == 0                # dry-run deletes nothing
    assert "session_log" in report["by_type"]
    # nothing actually removed
    assert len(cp.search_memory(subject_type="project", subject_id="demo")) == 4


def test_decay_apply_forgets_only_stale_uncurated():
    cp = ControlPlane.in_memory()
    _add(cp, "session_log", age_days=200)
    _add(cp, "deployment_learning:demo", age_days=200)
    _add(cp, "user", age_days=200)
    report = cp.decay_memory(ttl_days=90, dry_run=False)
    assert report["deleted"] == 1
    remaining = {r.record_type for r in cp.search_memory(subject_type="project", subject_id="demo")}
    assert "session_log" not in remaining
    assert "deployment_learning:demo" in remaining and "user" in remaining


def test_decay_protects_curated_even_when_ancient():
    cp = ControlPlane.in_memory()
    _add(cp, "deployment_learning:demo", age_days=9999)
    _add(cp, "fleet_learning:repository_access", age_days=9999)
    _add(cp, "dream:knowledge_snippet", age_days=9999, subject_type="dream", subject_id="project:demo")
    _add(cp, "project", age_days=9999)
    report = cp.decay_memory(ttl_days=1, dry_run=False)
    assert report["forgettable"] == 0 and report["deleted"] == 0
