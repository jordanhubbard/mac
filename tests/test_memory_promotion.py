"""Medium → long promotion: the writer ``mac_memory_long`` never had.

The 2026-08-21 audit of the live instance found ``mac_memory_long`` holding
zero points nearly three months after the collection was created, because no
code path anywhere passed ``tier="long"`` to the vector writer. These tests
pin the writer that closes that: what counts as settled, that a bounded pass
drains oldest-first, that promotion is idempotent, and that retiring the
medium copy only ever happens after the long-tier write succeeded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from mac.memory_promotion import (
    DEFAULT_MAX_PER_PASS,
    DEFAULT_MIN_AGE_DAYS,
    MemoryPromotionService,
    promotion_enabled,
    promotion_settings,
)
from mac.models import MAC_MEMORY_COLLECTIONS, MacMemoryTier, ValidationError
from mac.services import ControlPlane
from mac.vector_writer_service import VectorWriterService

MEDIUM = MAC_MEMORY_COLLECTIONS[MacMemoryTier.MEDIUM.value]
LONG = MAC_MEMORY_COLLECTIONS[MacMemoryTier.LONG.value]


class _FakeQdrant:
    """Enough of Qdrant's REST surface for the promotion path.

    Beyond upsert this needs scroll and delete, which the mem-07 fake never
    exercised: promotion reads a collection back and can retire points from
    it.
    """

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict]] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_on_collection: str | None = None

    def __call__(self, method, url, body=None, token=None):
        self.calls.append((method, url))
        path = url.split("?", 1)[0]
        coll, _, suffix = path.rsplit("/collections/", 1)[1].partition("/")
        self.collections.setdefault(coll, {})
        if coll == self.fail_on_collection:
            raise RuntimeError("qdrant refused %s on %s" % (method, coll))
        if method == "GET" and not suffix:
            points = self.collections[coll]
            size = len(next(iter(points.values()))["vector"]) if points else None
            return {
                "result": {
                    "points_count": len(points),
                    "config": {
                        "params": {
                            "vectors": ({"size": size} if size else {}),
                        }
                    },
                }
            }
        if method == "PUT" and suffix.startswith("points"):
            for point in (body or {}).get("points", []):
                self.collections[coll][str(point["id"])] = {
                    "id": str(point["id"]),
                    "vector": list(point.get("vector") or []),
                    "payload": point.get("payload") or {},
                }
            return {"result": {"status": "ok"}}
        if method == "POST" and suffix == "points/scroll":
            points = [
                {"id": p["id"], "payload": p["payload"]} for p in self.collections[coll].values()
            ]
            return {"result": {"points": points, "next_page_offset": None}}
        if method == "POST" and suffix == "points/delete":
            for point_id in (body or {}).get("points", []):
                self.collections[coll].pop(str(point_id), None)
            return {"result": {"status": "ok"}}
        raise AssertionError("FakeQdrant: unhandled %s %s" % (method, url))


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


@pytest.fixture()
def fake_qdrant():
    return _FakeQdrant()


@pytest.fixture()
def writer(cp, fake_qdrant):
    return VectorWriterService(
        memory=cp.memory,
        qdrant_url="http://fake.invalid:6333",
        embedding_dim=32,
        transport=fake_qdrant,
    )


@pytest.fixture()
def promoter(cp, writer):
    return MemoryPromotionService(memory=cp.memory, vector_writer=writer)


def _embed_aged(cp, writer, content, *, age_days: float):
    """Embed a memory into the medium tier, then backdate its ref.

    Promotion selects on the *ledger's* timestamp rather than Qdrant's,
    because vector_refs is the durable record of what was embedded when —
    and reading Qdrant to decide what to write to Qdrant makes the fix
    depend on the store it is trying to fill.
    """

    record = cp.add_memory(
        task_id=None,
        subject_type="topic",
        subject_id=None,
        record_type="note",
        content=content,
        evidence_id=None,
        created_by="agent_one",
    )
    ref = writer.embed_memory(record.id, tier=MacMemoryTier.MEDIUM.value)
    stamp = (datetime.now(tz=timezone.utc) - timedelta(days=age_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cp.store.execute("UPDATE vector_refs SET created_at = ? WHERE id = ?", (stamp, ref.id))
    return record


def test_settled_medium_memories_land_in_the_long_tier(cp, writer, promoter, fake_qdrant):
    """The whole point: mac_memory_long stops being empty."""
    record = _embed_aged(cp, writer, "an old lesson", age_days=90)

    report = promoter.promote()

    assert report["promoted"] == 1
    assert report["promoted_memory_ids"] == [record.id]
    assert report["target_collection"] == LONG
    assert len(fake_qdrant.collections[LONG]) == 1
    point = next(iter(fake_qdrant.collections[LONG].values()))
    assert point["payload"]["tier"] == "long"
    assert point["payload"]["memory_id"] == record.id
    # The ledger records the long-tier copy too, so the next pass skips it.
    refs = cp.memory.list_vector_refs(memory_id=record.id, collection=LONG)
    assert [ref.metadata["tier"] for ref in refs] == ["long"]


def test_working_set_is_left_alone(cp, writer, promoter, fake_qdrant):
    """A memory embedded yesterday is working set, not history."""
    _embed_aged(cp, writer, "still hot", age_days=1)

    report = promoter.promote()

    assert report["candidates"] == 0
    assert report["promoted"] == 0
    assert fake_qdrant.collections.get(LONG, {}) == {}


def test_promotion_is_idempotent(cp, writer, promoter):
    """Re-running must not re-promote what is already in the long tier.

    Point ids are a deterministic hash of the memory id, so a second write
    would upsert the same point — but it would also burn an embedding call
    per already-promoted memory on every nap, which at fleet scale is the
    difference between a no-op and a bill.
    """
    _embed_aged(cp, writer, "an old lesson", age_days=90)

    first = promoter.promote()
    second = promoter.promote()

    assert first["promoted"] == 1
    assert second["candidates"] == 0
    assert second["promoted"] == 0


def test_a_bounded_pass_drains_oldest_first(cp, writer, promoter):
    """Bounded so one nap does not hold an agent down for a whole sweep."""
    oldest = _embed_aged(cp, writer, "oldest", age_days=200)
    middle = _embed_aged(cp, writer, "middle", age_days=100)
    _embed_aged(cp, writer, "newest settled", age_days=40)

    report = promoter.promote(limit=2)

    assert report["promoted_memory_ids"] == [oldest.id, middle.id]


def test_dry_run_writes_nothing(cp, writer, promoter, fake_qdrant):
    record = _embed_aged(cp, writer, "an old lesson", age_days=90)

    report = promoter.promote(dry_run=True)

    assert report["promoted_memory_ids"] == [record.id]
    assert fake_qdrant.collections.get(LONG, {}) == {}
    assert cp.memory.list_vector_refs(collection=LONG) == []


def test_drop_medium_retires_the_source_point_and_its_ref(cp, writer, promoter, fake_qdrant):
    record = _embed_aged(cp, writer, "an old lesson", age_days=90)
    assert len(fake_qdrant.collections[MEDIUM]) == 1

    report = promoter.promote(drop_medium=True)

    assert report["dropped_from_medium"] == [record.id]
    assert fake_qdrant.collections[MEDIUM] == {}
    # A ref naming a point that no longer exists would be a ledger lie.
    assert cp.memory.list_vector_refs(memory_id=record.id, collection=MEDIUM) == []
    assert len(cp.memory.list_vector_refs(memory_id=record.id, collection=LONG)) == 1


def test_medium_is_never_dropped_when_the_long_write_failed(cp, writer, promoter, fake_qdrant):
    """Copy-then-verify: a failed promotion must not destroy the only copy."""
    _embed_aged(cp, writer, "an old lesson", age_days=90)
    fake_qdrant.fail_on_collection = LONG

    report = promoter.promote(drop_medium=True)

    assert report["promoted"] == 0
    assert report["dropped_from_medium"] == []
    assert len(report["failures"]) == 1
    assert len(fake_qdrant.collections[MEDIUM]) == 1


def test_one_bad_record_does_not_stop_the_backlog(cp, writer, promoter, monkeypatch):
    """A single unembeddable memory must not strand every one behind it."""
    first = _embed_aged(cp, writer, "first", age_days=200)
    second = _embed_aged(cp, writer, "second", age_days=100)

    real_embed = writer.embed_memory

    def _explode(memory_id, **kwargs):
        if memory_id == first.id and kwargs.get("tier") == "long":
            raise RuntimeError("embedding backend said no")
        return real_embed(memory_id, **kwargs)

    monkeypatch.setattr(writer, "embed_memory", _explode)

    report = promoter.promote()

    assert report["promoted_memory_ids"] == [second.id]
    assert [f["memory_id"] for f in report["failures"]] == [first.id]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_promotion_defaults_to_on(monkeypatch):
    """Defaulting off would reproduce the disease it fixes: a promotion path
    that exists and never runs leaves the long tier empty exactly as before."""
    monkeypatch.delenv("MAC_MEMORY_PROMOTION_ENABLED", raising=False)
    assert promotion_enabled({}) is True
    settings = promotion_settings({})
    assert settings["enabled"] is True
    assert settings["min_age_days"] == DEFAULT_MIN_AGE_DAYS
    assert settings["max_per_pass"] == DEFAULT_MAX_PER_PASS


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF"])
def test_promotion_can_be_switched_off_without_a_redeploy(raw):
    assert promotion_enabled({"MAC_MEMORY_PROMOTION_ENABLED": raw}) is False


def test_out_of_range_settings_fall_back_and_say_so():
    settings = promotion_settings({"MAC_MEMORY_PROMOTION_MIN_AGE_DAYS": "-5"})
    assert settings["min_age_days"] == DEFAULT_MIN_AGE_DAYS
    assert settings["configuration_errors"]


# ---------------------------------------------------------------------------
# Control-plane wiring
# ---------------------------------------------------------------------------


def test_control_plane_promotes_through_the_configured_writer(cp, writer, fake_qdrant, monkeypatch):
    monkeypatch.delenv("MAC_MEMORY_PROMOTION_ENABLED", raising=False)
    record = _embed_aged(cp, writer, "an old lesson", age_days=90)

    report = cp.promote_memory_tier(vector_writer=writer)

    assert report["skipped"] is False
    assert report["promoted_memory_ids"] == [record.id]
    assert len(fake_qdrant.collections[LONG]) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_age_days": "sample"},
        {"min_age_days": -1.0},
        {"limit": "sample"},
        {"limit": 0},
    ],
)
def test_control_plane_rejects_unusable_promotion_arguments(cp, writer, kwargs):
    """A junk threshold is a rejected request, not a ValueError from float()."""
    with pytest.raises(ValidationError):
        cp.promote_memory_tier(vector_writer=writer, **kwargs)


def test_control_plane_rejects_a_writer_that_cannot_promote(cp):
    """Promotion needs embed_memory; say so instead of failing mid-pass."""
    with pytest.raises(ValidationError):
        cp.promote_memory_tier(vector_writer=SimpleNamespace(recall=lambda **_: []))


def test_dropping_medium_additionally_requires_delete_point(cp, writer, monkeypatch):
    """drop_medium retires source points, so the writer must be able to."""
    monkeypatch.delenv("MAC_MEMORY_PROMOTION_ENABLED", raising=False)
    embed_only = SimpleNamespace(embed_memory=writer.embed_memory)

    cp.promote_memory_tier(vector_writer=embed_only)  # copy-only: accepted

    with pytest.raises(ValidationError):
        cp.promote_memory_tier(vector_writer=embed_only, drop_medium=True)


def test_control_plane_honours_the_off_switch(cp, writer, monkeypatch):
    monkeypatch.setenv("MAC_MEMORY_PROMOTION_ENABLED", "0")
    _embed_aged(cp, writer, "an old lesson", age_days=90)

    report = cp.promote_memory_tier(vector_writer=writer)

    assert report["skipped"] is True
    assert report["promoted"] == 0


def test_nap_cycle_runs_promotion(cp, writer, fake_qdrant, monkeypatch):
    """Promotion rides the nap because the nap is the thing that already runs
    on a schedule. Giving it its own timer would repeat the failure that
    killed ingestion on 2026-07-25 — a second scheduled thing nobody
    remembers to migrate."""
    monkeypatch.delenv("MAC_MEMORY_PROMOTION_ENABLED", raising=False)
    machine = cp.register_machine("h1")
    agent = cp.register_agent(machine.id, "agent-napper", capabilities=[])
    record = _embed_aged(cp, writer, "an old lesson", age_days=90)

    result = cp.run_nap_cycle(agent.id, vector_writer=writer)

    assert result["promotion_error"] is None
    assert result["promotion"]["promoted_memory_ids"] == [record.id]
    assert len(fake_qdrant.collections[LONG]) == 1


def test_nap_cycle_survives_a_promotion_failure(cp, writer, fake_qdrant, monkeypatch):
    """A promotion failure must never strand an agent in DRAINING."""
    monkeypatch.delenv("MAC_MEMORY_PROMOTION_ENABLED", raising=False)
    machine = cp.register_machine("h1")
    agent = cp.register_agent(machine.id, "agent-napper", capabilities=[])
    _embed_aged(cp, writer, "an old lesson", age_days=90)
    fake_qdrant.fail_on_collection = LONG

    result = cp.run_nap_cycle(agent.id, vector_writer=writer)

    assert result["skipped"] is False
    assert result["nap_run"]["status"] in {"completed", "COMPLETED"}
    assert result["promotion"]["failures"]


def test_nap_cycle_can_opt_out_of_promotion(cp, writer, fake_qdrant, monkeypatch):
    monkeypatch.delenv("MAC_MEMORY_PROMOTION_ENABLED", raising=False)
    machine = cp.register_machine("h1")
    agent = cp.register_agent(machine.id, "agent-napper", capabilities=[])
    _embed_aged(cp, writer, "an old lesson", age_days=90)

    result = cp.run_nap_cycle(agent.id, vector_writer=writer, promote_into_long=False)

    assert result["promotion"] == {}
    assert fake_qdrant.collections.get(LONG, {}) == {}
