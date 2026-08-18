"""mac's own merge queue: ordering, speculation, the window, and the land gate.

GitHub merge queues are organization-only, so on every User-owned repository the
operator has, there is no forge queue to borrow serialization from. These tests
pin the queue mac provides instead.

The invariant every test here exists to protect is *never land an untested
tree*. The queue may be slow, may defer, may throw away speculation -- all of
that is negotiable. Landing a tree nobody tested is not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac import gitops
from mac.merge_capability import (
    MergeCapability,
    merge_serialization_mode,
    resolve_merge_capability,
    stored_capability,
)
from mac.models import TaskState, ValidationError
from mac.native_merge_queue import (
    MODE_FORGE_QUEUE,
    MODE_NATIVE_QUEUE,
    STATE_EVICTED,
    STATE_QUEUED,
    STATE_TESTED,
    NativeMergeQueue,
    QueueEntry,
    WindowBounds,
    landing_is_safe,
    next_window,
    plan_eviction,
    speculation_plan,
)
from mac.services import ControlPlane
from tests.test_publication_pull_request import (
    FakeForge,
    build_repo,
    drive_to_approval,
    git,
    install_forge,
    published_detail,
)

REPO = "https://github.invalid/acme/widgets.git"
BRANCH = "main"


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


@pytest.fixture()
def queue(cp):
    return NativeMergeQueue(cp.store, bounds=WindowBounds(floor=1, ceiling=4))


def _admit(queue, task_id: str, head: str, **kwargs):
    return queue.admit(
        repository=REPO, branch=BRANCH, task_id=task_id, head_sha=head, **kwargs
    )


def _claim(queue, task_id: str, head: str, owner: str = "hub-a"):
    return queue.claim_slot(
        repository=REPO, branch=BRANCH, task_id=task_id, head_sha=head, owner=owner
    )


def _entry(entry_id: str, position: int, *, state=STATE_QUEUED, head="", preds=()):
    return QueueEntry(
        id=entry_id,
        repository=REPO,
        branch=BRANCH,
        task_id="task_" + entry_id,
        pull_request_number=0,
        head_sha=head or ("sha" + entry_id),
        state=state,
        position=position,
        speculation_epoch=0,
        predecessors=tuple(preds),
    )


# ---------------------------------------------------------------------------
# The window controller: additive increase, multiplicative decrease.
# ---------------------------------------------------------------------------


def test_window_grows_additively_on_success_and_halves_on_failure():
    bounds = WindowBounds(floor=1, ceiling=8, increment=1)
    size = 1
    for expected in (2, 3, 4, 5):
        size = next_window(size, outcome="landed", bounds=bounds)
        assert size == expected
    # Multiplicative decrease.
    assert next_window(8, outcome="failed", bounds=bounds) == 4
    assert next_window(5, outcome="failed", bounds=bounds) == 2


def test_window_respects_its_floor_and_ceiling():
    bounds = WindowBounds(floor=2, ceiling=3, increment=1)
    # Ceiling: success cannot grow past it.
    assert next_window(3, outcome="landed", bounds=bounds) == 3
    assert next_window(99, outcome="landed", bounds=bounds) == 3
    # Floor: failure cannot shrink below it, and a queue already at the floor
    # stays there rather than going to zero (which would wedge the queue).
    assert next_window(2, outcome="failed", bounds=bounds) == 2
    assert next_window(3, outcome="failed", bounds=bounds) == 2


def test_window_bounds_reject_nonsense():
    with pytest.raises(ValueError):
        WindowBounds(floor=0)
    with pytest.raises(ValueError):
        WindowBounds(floor=3, ceiling=2)
    with pytest.raises(ValueError):
        WindowBounds(increment=0)


def test_the_durable_window_moves_with_real_landings_and_evictions(queue):
    first = _admit(queue, "task_a", "a" * 40)
    assert queue.window(REPO, BRANCH) == 1

    queue.record_landed(first.id, landed_sha="f" * 40)
    assert queue.window(REPO, BRANCH) == 2

    second = _admit(queue, "task_b", "b" * 40)
    queue.record_landed(second.id, landed_sha="e" * 40)
    assert queue.window(REPO, BRANCH) == 3

    third = _admit(queue, "task_c", "c" * 40)
    queue.evict(third.id, reason="contract gate failed")
    # Halved, not decremented.
    assert queue.window(REPO, BRANCH) == 1

    snapshot = queue.snapshot(REPO, BRANCH)
    assert snapshot["landed_count"] == 2
    assert snapshot["failure_count"] == 1
    assert snapshot["window_floor"] == 1
    assert snapshot["window_ceiling"] == 4


# ---------------------------------------------------------------------------
# Speculation: plan, evict, discard.
# ---------------------------------------------------------------------------


def test_speculation_plans_each_entry_on_top_of_the_ones_ahead_of_it():
    entries = [
        _entry("a", 1, head="A" * 40),
        _entry("b", 2, head="B" * 40),
        _entry("c", 3, head="C" * 40),
    ]
    plan = speculation_plan(entries, window=3)
    assert [slot.entry_id for slot in plan] == ["a", "b", "c"]
    assert plan[0].predecessors == ()
    assert plan[1].predecessors == ("A" * 40,)
    assert plan[2].predecessors == ("A" * 40, "B" * 40)


def test_the_window_bounds_how_many_entries_may_speculate():
    entries = [_entry(name, index + 1) for index, name in enumerate("abcde")]
    assert [slot.entry_id for slot in speculation_plan(entries, window=2)] == ["a", "b"]
    assert len(speculation_plan(entries, window=5)) == 5


def test_evicting_entry_k_discards_every_speculative_result_behind_it():
    entries = [
        _entry("a", 1, state=STATE_TESTED),
        _entry("b", 2, state=STATE_TESTED, preds=("A" * 40,)),
        _entry("c", 3, state=STATE_TESTED, preds=("A" * 40, "B" * 40)),
    ]
    plan = plan_eviction(entries, "a", "gate failed")
    assert plan.evicted_id == "a"
    assert plan.survivors == ("b", "c")
    # b and c were green -- against a tree that will never exist.
    assert plan.discarded == ("b", "c")


def test_a_failed_entry_is_evicted_and_the_survivors_are_retested_without_it(queue):
    """The core speculative-batching property, end to end on the ledger.

    Three entries speculate on each other. Entry A fails. B and C were tested
    against `tip + A`, a state that will now never exist, so their results are
    discarded, they return to `queued` in a NEW speculation epoch, and nothing
    behind A can present its old result as a pass.
    """

    first = _claim(queue, "task_a", "A" * 40)
    assert first.admitted and first.predecessors == ()
    queue.record_tested(
        first.entry.id,
        owner="hub-a",
        base_sha="T" * 40,
        base_tree="tip-tree",
        merge_tree="tree-a",
    )
    # Landing A widens the window, which is what lets B speculate at all.
    queue.record_landed(first.entry.id, landed_sha="L" * 40)
    assert queue.window(REPO, BRANCH) == 2

    second = _claim(queue, "task_b", "B" * 40)
    third = _claim(queue, "task_c", "C" * 40)
    assert second.admitted and third.admitted
    assert third.predecessors == ("B" * 40,)
    for decision, tree in ((second, "tree-b"), (third, "tree-c")):
        assert queue.record_tested(
            decision.entry.id,
            owner="hub-a",
            base_sha="S" * 40,
            base_tree="spec-tree-%s" % tree,
            merge_tree=tree,
        )

    outcome = queue.evict(second.entry.id, reason="contract gate failed")

    assert queue.entry(second.entry.id).state == STATE_EVICTED
    # C survives, but its speculative result is gone.
    survivor = queue.entry(third.entry.id)
    assert survivor.state == STATE_QUEUED
    assert survivor.tested_base_tree == ""
    assert survivor.tested_merge_tree == ""
    assert survivor.predecessors == ()
    assert survivor.speculation_epoch > third.entry.speculation_epoch
    assert outcome["speculation_discarded"] == 1
    assert list(outcome["discarded_entries"]) == [third.entry.id]

    # And nothing behind K can land on the discarded result: even presented
    # with the exact tree it was tested against, the land gate refuses.
    allowed, why, _ = queue.may_land(
        third.entry.id, canonical_tip_tree="spec-tree-tree-c"
    )
    assert allowed is False
    assert "no recorded test result" in why

    # Re-testing without the evicted entry puts C at the front, on the real tip.
    replanned = _claim(queue, "task_c", "C" * 40)
    assert replanned.admitted
    assert replanned.predecessors == ()

    snapshot = queue.snapshot(REPO, BRANCH)
    assert snapshot["speculation_discarded"] == 1
    assert snapshot["recent_evictions"][-1]["task_id"] == "task_b"
    assert "contract gate failed" in snapshot["recent_evictions"][-1]["reason"]


def test_an_entry_outside_the_window_is_deferred_not_dropped(queue):
    _claim(queue, "task_a", "A" * 40)
    blocked = _claim(queue, "task_b", "B" * 40)
    assert blocked.admitted is False
    assert blocked.defer_seconds > 0
    assert "#2 in line" in blocked.reason
    # It kept its place; it is still queued.
    assert queue.entry(blocked.entry.id).state == STATE_QUEUED
    assert queue.snapshot(REPO, BRANCH)["queue_depth"] == 2


# ---------------------------------------------------------------------------
# The land gate: never land an untested tree.
# ---------------------------------------------------------------------------


def test_landing_requires_the_front_position_a_result_and_a_matching_tree():
    tested = QueueEntry(
        id="a",
        repository=REPO,
        branch=BRANCH,
        task_id="t",
        pull_request_number=1,
        head_sha="A" * 40,
        state=STATE_TESTED,
        position=1,
        speculation_epoch=0,
        tested_base_tree="base-tree",
        tested_merge_tree="merged-tree",
    )
    assert landing_is_safe(tested, canonical_tip_tree="base-tree", front_entry_id="a") == (
        True,
        "",
    )
    # Not at the front.
    ok, why = landing_is_safe(
        tested, canonical_tip_tree="base-tree", front_entry_id="b"
    )
    assert (ok, "front of the queue" in why) == (False, True)
    # The tip moved: the tested projection is stale.
    ok, why = landing_is_safe(
        tested, canonical_tip_tree="other-tree", front_entry_id="a"
    )
    assert (ok, "stale" in why) == (False, True)
    # An unreadable tip is a refusal, never a pass.
    ok, why = landing_is_safe(tested, canonical_tip_tree="", front_entry_id="a")
    assert (ok, "could not be read" in why) == (False, True)


def test_a_missing_entry_defers_rather_than_landing(queue):
    allowed, why, entry = queue.may_land("mergeq_nope", canonical_tip_tree="t")
    assert allowed is False
    assert entry is None
    assert "disappeared" in why


# ---------------------------------------------------------------------------
# Crash safety.
# ---------------------------------------------------------------------------


def test_a_crash_between_validate_and_land_does_not_double_land(cp, queue):
    """A hub that dies after merging must not merge again on restart."""

    decision = _claim(queue, "task_a", "A" * 40)
    queue.record_tested(
        decision.entry.id,
        owner="hub-a",
        base_sha="T" * 40,
        base_tree="tip-tree",
        merge_tree="merged",
    )
    first = queue.record_landed(decision.entry.id, landed_sha="L" * 40)
    assert first["changed"] is True
    window_after_first = queue.window(REPO, BRANCH)

    # The hub dies here. A new process rebuilds the queue from the ledger.
    restarted = NativeMergeQueue(cp.store, bounds=WindowBounds(floor=1, ceiling=4))
    again = restarted.record_landed(decision.entry.id, landed_sha="L" * 40)
    assert again["changed"] is False
    assert again["reason"] == "already landed"
    # And the window did not get credited twice for one land.
    assert restarted.window(REPO, BRANCH) == window_after_first
    # A landed entry can never re-enter the land gate.
    allowed, why, _ = restarted.may_land(decision.entry.id, canonical_tip_tree="tip-tree")
    assert allowed is False
    assert "landed" in why


def test_an_expired_lease_is_reclaimed_and_its_stale_result_is_dropped(cp):
    """A dead hub's slot must come back -- WITHOUT its unverifiable result."""

    queue = NativeMergeQueue(
        cp.store, bounds=WindowBounds(floor=1, ceiling=4), lease_seconds=60
    )
    decision = queue.claim_slot(
        repository=REPO,
        branch=BRANCH,
        task_id="task_a",
        head_sha="A" * 40,
        owner="hub-dead",
    )
    queue.record_tested(
        decision.entry.id,
        owner="hub-dead",
        base_sha="T" * 40,
        base_tree="tip-tree",
        merge_tree="merged",
    )
    # Expire the lease the way wall-clock time would.
    cp.store.execute(
        "UPDATE merge_queue_entries SET lease_expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00.000000+00:00", decision.entry.id),
    )
    successor = queue.claim_slot(
        repository=REPO,
        branch=BRANCH,
        task_id="task_a",
        head_sha="A" * 40,
        owner="hub-fresh",
    )
    assert successor.admitted is True
    reclaimed = queue.entry(decision.entry.id)
    assert reclaimed.lease_owner == "hub-fresh"
    assert reclaimed.position == decision.entry.position  # order is preserved
    assert reclaimed.tested_base_tree == ""  # the dead hub's result is not reused


def test_a_reviewed_head_that_moved_supersedes_the_old_entry(queue):
    first = _admit(queue, "task_a", "A" * 40)
    second = _admit(queue, "task_a", "Z" * 40)
    assert second.id != first.id
    assert queue.entry(first.id).state == "superseded"
    assert queue.snapshot(REPO, BRANCH)["queue_depth"] == 1


def test_admission_is_idempotent_across_publication_retries(queue):
    first = _admit(queue, "task_a", "A" * 40)
    again = _admit(queue, "task_a", "A" * 40)
    assert again.id == first.id
    assert queue.snapshot(REPO, BRANCH)["queue_depth"] == 1


# ---------------------------------------------------------------------------
# Capability: which mechanism serializes, resolved once and stored.
# ---------------------------------------------------------------------------


def test_a_repository_with_a_forge_queue_routes_to_the_forge():
    capability = resolve_merge_capability(
        REPO,
        BRANCH,
        resolve_forge=lambda url: "github",
        queue_enabled=lambda url, branch: True,
    )
    assert capability.supported is True
    assert capability.enabled is True
    assert capability.use_forge_queue is True
    assert merge_serialization_mode(capability) == MODE_FORGE_QUEUE


def test_supported_and_enabled_are_recorded_separately():
    """An org repo that has not turned the queue on is not a personal repo.

    Both route to mac's queue today, but only one of them could ever stop.
    """

    org = resolve_merge_capability(
        REPO,
        BRANCH,
        resolve_forge=lambda url: "github",
        queue_enabled=lambda url, branch: False,
        owner_is_organization=lambda url: True,
    )
    personal = resolve_merge_capability(
        REPO,
        BRANCH,
        resolve_forge=lambda url: "github",
        queue_enabled=lambda url, branch: False,
        owner_is_organization=lambda url: False,
    )
    assert (org.supported, org.enabled) == (True, False)
    assert (personal.supported, personal.enabled) == (False, False)
    assert merge_serialization_mode(org) == MODE_NATIVE_QUEUE
    assert merge_serialization_mode(personal) == MODE_NATIVE_QUEUE


def test_an_unknown_capability_takes_the_safe_branch_never_a_bare_squash():
    unknown = resolve_merge_capability(
        REPO,
        BRANCH,
        resolve_forge=lambda url: "github",
        queue_enabled=lambda url, branch: None,
    )
    assert unknown.enabled is None
    assert unknown.error
    assert unknown.use_forge_queue is False
    assert merge_serialization_mode(unknown) == MODE_NATIVE_QUEUE
    # A repository with no capability record at all is the same answer.
    assert merge_serialization_mode(None) == MODE_NATIVE_QUEUE


def test_a_forge_that_cannot_be_reached_is_a_definite_native_answer():
    capability = resolve_merge_capability(
        REPO, BRANCH, resolve_forge=lambda url: ""
    )
    assert (capability.forge, capability.credential) == ("", False)
    assert (capability.supported, capability.enabled) == (False, False)
    assert merge_serialization_mode(capability) == MODE_NATIVE_QUEUE


def test_gitea_has_no_merge_queue_equivalent():
    capability = resolve_merge_capability(
        REPO, BRANCH, resolve_forge=lambda url: "gitea"
    )
    assert capability.forge == "gitea"
    assert capability.enabled is False
    assert merge_serialization_mode(capability) == MODE_NATIVE_QUEUE


def test_a_probe_that_raises_is_recorded_not_propagated():
    def boom(url, branch):
        raise RuntimeError("rate limited")

    capability = resolve_merge_capability(
        REPO, BRANCH, resolve_forge=lambda url: "github", queue_enabled=boom
    )
    assert capability.enabled is None
    assert "rate limited" in capability.error
    assert merge_serialization_mode(capability) == MODE_NATIVE_QUEUE


def test_a_capability_is_stale_when_it_expires_or_changes_branch():
    fresh = MergeCapability(
        forge="github",
        enabled=False,
        supported=False,
        branch="main",
        resolved_at="2026-08-18T00:00:00.000000+00:00",
    )
    assert fresh.is_stale(branch="main", ttl_seconds=3600, now="2026-08-18T00:10:00.000000+00:00") is False
    assert fresh.is_stale(branch="main", ttl_seconds=3600, now="2026-08-18T02:00:00.000000+00:00") is True
    # A different canonical branch is a different question.
    assert fresh.is_stale(branch="release", ttl_seconds=3600, now="2026-08-18T00:10:00.000000+00:00") is True
    # Never resolved at all.
    assert MergeCapability().is_stale() is True


def test_the_capability_round_trips_through_repository_metadata(cp, tmp_path):
    capability = MergeCapability(
        forge="github",
        credential=True,
        supported=False,
        enabled=False,
        branch="main",
        remote=REPO,
        resolved_at="2026-08-18T00:00:00.000000+00:00",
        resolver="github-ingest",
    )
    assert stored_capability(
        {"merge_serialization_capability": capability.to_dict()}
    ) == capability
    # A blob with the wrong schema is not trusted.
    assert stored_capability({"merge_serialization_capability": {"enabled": True}}) is None


# ---------------------------------------------------------------------------
# Publication: the queue in the seam #400 built.
# ---------------------------------------------------------------------------


def test_publication_without_a_forge_queue_records_the_native_guarantee(
    cp, tmp_path, monkeypatch
):
    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    detail = published_detail(cp, task.id)
    assert detail["merge_serialization"] == MODE_NATIVE_QUEUE

    capability = next(
        item
        for item in detail["commands"]
        if item["name"] == "merge_serialization_capability"
    )
    assert capability["mode"] == MODE_NATIVE_QUEUE
    # supported/enabled are separate facts, both recorded.
    assert "supported" in capability and "enabled" in capability

    # The queue is observable: depth, window, and what it did.
    serialization = next(
        item for item in detail["commands"] if item["name"] == "merge_serialization"
    )
    snapshot = serialization["queue"]
    assert snapshot["window_size"] >= 1
    assert snapshot["window_ceiling"] >= snapshot["window_floor"]
    assert "queue_depth" in snapshot

    landed = next(
        item for item in detail["commands"] if item["name"] == "merge_queue_landed"
    )
    assert landed["changed"] is True
    assert landed["observed_only"] is False


def test_a_queued_pull_request_that_landed_on_the_forge_is_observed_not_remerged(
    cp, tmp_path, monkeypatch
):
    """Never double-land. If it is already merged, that is a success to record."""

    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    # Somebody (a human, or a previous attempt of ours that died after the
    # merge) lands the PR while this attempt is still preparing.
    landed: list[str] = []

    def land_it_first(url, branch):
        if not landed:
            landed.append(forge.land_from_queue(task_head))
        return ("sanity",)

    monkeypatch.setattr(gitops, "required_status_check_contexts", land_it_first)

    publication = cp.publish_task(
        task.id, "git://main", reviewer.id, evidence_id=evidence.id
    )

    assert publication.status == "published"
    # The decisive assertion: mac did NOT ask the forge to merge a second time.
    assert forge.merges == []

    detail = published_detail(cp, task.id)
    observed = next(
        item
        for item in detail["commands"]
        if item["name"] == "merge_queue_observe_pull_request"
    )
    assert observed["merged"] is True
    recorded = next(
        item for item in detail["commands"] if item["name"] == "merge_queue_landed"
    )
    assert recorded["observed_only"] is True
    assert detail["final_sha"] == landed[0]


def test_an_unreadable_pull_request_state_defers_instead_of_merging(
    cp, tmp_path, monkeypatch
):
    """Ambiguity resolves to NOT landing. It may never resolve to 'merge anyway'."""

    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    monkeypatch.setattr(
        gitops,
        "pull_request_state",
        lambda url, number, **_: {
            "known": False,
            "merged": False,
            "sha": "",
            "state": "",
            "head_sha": "",
            "error": "502 Bad Gateway",
        },
    )
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert (
        getattr(excinfo.value, "publication_failure_kind", "")
        == "merge_queue_unreadable_state"
    )
    assert getattr(excinfo.value, "publication_retry_after_seconds", 0) > 0
    # Nothing was merged and main is untouched.
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head
    assert cp.get_task(task.id).state != TaskState.COMPLETED.value


def test_a_forge_queue_still_wins_when_the_repository_actually_has_one(
    cp, tmp_path, monkeypatch
):
    """The native queue is the fallback, not a third parallel mechanism."""

    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge", queue=True)
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert (
        getattr(excinfo.value, "publication_failure_kind", "") == "pull_request_queued"
    )
    # It went to the FORGE queue, and mac's queue never enrolled it.
    assert forge.enqueued == [{"number": 101, "sha": task_head}]
    assert (
        cp.store.query_all("SELECT id FROM merge_queue_entries", ()) == []
    )


# ---------------------------------------------------------------------------
# Capability refresh rides on the existing poller.
# ---------------------------------------------------------------------------


def test_the_ingest_pass_resolves_capability_once_and_then_skips_it(monkeypatch):
    from mac.github_ingest import GitHubIngestConfig, GitHubIssueIngestor

    class FakeRepo:
        id = "projectrepo_1"
        name = "widgets"
        metadata = {
            "repository_contract": {
                "canonical_remote_url": REPO,
                "canonical_branch": BRANCH,
            }
        }

    class FakeControlPlane:
        def __init__(self):
            self.repo = FakeRepo()
            self.recorded: list = []

        def list_project_repositories(self, enabled=None):
            return [self.repo]

        def record_repository_merge_capability(self, repo_id, capability):
            self.recorded.append((repo_id, capability))
            self.repo.metadata = dict(self.repo.metadata)
            self.repo.metadata["merge_serialization_capability"] = capability
            return self.repo

    control_plane = FakeControlPlane()
    ingestor = GitHubIssueIngestor(control_plane, GitHubIngestConfig.from_env())

    probes: list = []

    def probe(url, branch):
        probes.append((url, branch))
        return False

    monkeypatch.setattr(gitops, "resolve_forge", lambda url: "github")
    monkeypatch.setattr(gitops, "merge_queue_enabled", probe)

    first = ingestor._refresh_merge_capabilities(actor="test")
    assert first["refreshed"] == 1
    assert first["repositories"][0]["mode"] == MODE_NATIVE_QUEUE
    assert len(probes) == 1

    # The TTL is the point: merge-queue configuration changes maybe twice a
    # year, and this poller runs every 60 seconds.
    second = ingestor._refresh_merge_capabilities(actor="test")
    assert second["skipped_fresh"] == 1
    assert second["refreshed"] == 0
    assert len(probes) == 1


def test_a_capability_probe_failure_does_not_break_issue_ingest(monkeypatch):
    from mac.github_ingest import GitHubIngestConfig, GitHubIssueIngestor

    class ExplodingControlPlane:
        def list_project_repositories(self, enabled=None):
            raise RuntimeError("registry unavailable")

    ingestor = GitHubIssueIngestor(
        ExplodingControlPlane(), GitHubIngestConfig.from_env()
    )
    report = ingestor._refresh_merge_capabilities(actor="test")
    assert report["checked"] == 0
    assert "registry unavailable" in report["error"]


# ---------------------------------------------------------------------------
# Building the speculative base out of real git objects.
# ---------------------------------------------------------------------------


def _git_step_for(repo: Path):
    """A `git_step` shaped like publication's, over a real repository."""

    def step(name, args, timeout=120, *, check=True):
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
        result = {
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
        if check and proc.returncode != 0:
            raise AssertionError("%s failed: %s" % (name, proc.stderr))
        return result

    return step


def _repo_with_two_branches(tmp_path: Path):
    repo = tmp_path / "spec"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "a@example.com")
    git(repo, "config", "user.name", "A")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-q", "-m", "base")
    tip = git(repo, "rev-parse", "HEAD")
    heads = []
    for name in ("one", "two"):
        git(repo, "checkout", "-q", "-B", name, tip)
        (repo / ("%s.txt" % name)).write_text(name + "\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", name)
        heads.append(git(repo, "rev-parse", "HEAD"))
    git(repo, "checkout", "-q", "main")
    return repo, tip, heads


def test_the_speculative_base_stacks_every_predecessor_on_the_tip(cp, tmp_path):
    """Entry N is tested on `tip + 1..N-1`, which is the whole point of a train."""

    repo, tip, heads = _repo_with_two_branches(tmp_path)
    projected = cp._build_speculative_base(
        repo,
        _git_step_for(repo),
        tip,
        [{"head_sha": sha, "source_branch": ""} for sha in heads],
    )
    assert projected and projected != tip
    files = git(repo, "ls-tree", "--name-only", "-r", projected).split()
    # Both predecessors are present in the tree this entry will be tested on.
    assert sorted(files) == ["base.txt", "one.txt", "two.txt"]


def test_two_queued_changes_that_conflict_yield_no_speculative_base(cp, tmp_path):
    """Speculation is refused, not silently retargeted at the bare tip."""

    repo = tmp_path / "conflict"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "a@example.com")
    git(repo, "config", "user.name", "A")
    (repo / "f.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-q", "-m", "base")
    tip = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-B", "one", tip)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "one")
    one = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-B", "two", tip)
    (repo / "f.txt").write_text("two\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", "two")
    git(repo, "checkout", "-q", "main")
    # `two` is being tested on top of `one`, and they touch the same line.
    (repo / "f.txt").write_text("two\n", encoding="utf-8")
    projected = cp._build_speculative_base(
        repo,
        _git_step_for(repo),
        one,
        [{"head_sha": tip, "source_branch": ""}, {"head_sha": "", "source_branch": ""}],
    )
    assert projected == ""


def test_a_predecessor_that_cannot_be_fetched_yields_no_speculative_base(
    cp, tmp_path
):
    repo, tip, _heads = _repo_with_two_branches(tmp_path)
    projected = cp._build_speculative_base(
        repo,
        _git_step_for(repo),
        tip,
        [{"head_sha": "0" * 40, "source_branch": "nonexistent"}],
    )
    assert projected == ""


def test_publication_defers_when_the_queue_window_is_full(
    cp, tmp_path, monkeypatch
):
    """A change behind the window waits its turn instead of jumping it."""

    remote, source, main_head, task_head = build_repo(tmp_path)
    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    # Somebody else is already at the front of this queue, and the window
    # starts at its floor of 1.
    queue = cp._native_merge_queue()
    repository = str(remote)
    from mac.services import _canonicalize_git_url

    queue.admit(
        repository=_canonicalize_git_url(repository) or repository,
        branch="main",
        task_id="task_someone_else",
        head_sha="9" * 40,
    )

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert (
        getattr(excinfo.value, "publication_failure_kind", "")
        == "merge_queue_deferred"
    )
    assert getattr(excinfo.value, "publication_retry_after_seconds", 0) > 0
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head


def test_a_change_behind_another_is_tested_on_top_of_it_and_will_not_jump_it(
    cp, tmp_path, monkeypatch
):
    """Speculation and ordering, end to end through publication.

    Another approved change is already at the front of the queue and the window
    is 2, so this publication is admitted as #2 -- and it is projected and
    tested on top of the change ahead of it (`tip + entry 1`), not on the bare
    tip. It then refuses to land, because landing out of order would put a tree
    on the trunk that nothing was tested against.
    """

    from mac.services import _canonicalize_git_url

    remote, source, main_head, task_head = build_repo(tmp_path)
    # A second approved change, really pushed, that is ahead of us in line.
    git(source, "checkout", "-q", "-B", "task/other", main_head)
    (source / "other.txt").write_text("other\n", encoding="utf-8")
    git(source, "add", "other.txt")
    git(source, "commit", "-q", "-m", "the change ahead of us")
    other_head = git(source, "rev-parse", "HEAD")
    git(source, "push", "-q", "origin", "task/other")
    git(source, "checkout", "-q", "main")

    forge = FakeForge(remote, tmp_path / "forge")
    install_forge(monkeypatch, forge)
    task, evidence, reviewer = drive_to_approval(cp, source, task_head)

    repository = _canonicalize_git_url(str(remote)) or str(remote)
    queue = cp._native_merge_queue()
    ahead = queue.admit(
        repository=repository,
        branch="main",
        task_id="task_ahead",
        head_sha=other_head,
        detail={"source_branch": "task/other"},
    )
    # Widen the window so a second entry may speculate at all.
    cp.store.execute(
        "UPDATE merge_queue_windows SET window_size = 2 WHERE repository = ? AND branch = ?",
        (repository, "main"),
    )

    with pytest.raises(ValidationError) as excinfo:
        cp.publish_task(task.id, "git://main", reviewer.id, evidence_id=evidence.id)

    assert (
        getattr(excinfo.value, "publication_failure_kind", "") == "merge_queue_waiting"
    )
    # Nothing landed, and main is exactly where it was.
    assert forge.merges == []
    assert git(source, "ls-remote", "origin", "refs/heads/main").split()[0] == main_head

    ours = next(
        entry
        for entry in queue.live_entries(repository, "main")
        if entry.task_id == task.id
    )
    assert ours.position > ahead.position
    assert ours.predecessors == (other_head,)
    # THE POINT: it was tested against `tip + the change ahead of it`, which is
    # neither the bare tip nor that change's own commit.
    assert ours.state == STATE_TESTED
    assert ours.tested_base_sha not in {"", main_head, other_head}
    assert ours.tested_base_tree and ours.tested_merge_tree
