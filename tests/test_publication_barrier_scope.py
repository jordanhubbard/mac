"""The publication barrier must not be held across the publication.

Thread dump taken on the hub while it was unresponsive, 2026-08-14:

    Thread A   publish_task -> validate_projected_merge_contract
               -> _hub_verify_run_contract_test -> subprocess wait
               ...holding _PUBLICATION_BARRIER_THREAD_LOCK

    Threads B..H  publish_task -> publication_serialization  (blocked)
                  one of them the hub TICK thread

One publication held the barrier for the whole contract gate -- a clone, an
upload, a dependency bootstrap and a test suite inside a sandbox, 45 to 90
minutes. Every other publication queued behind it, the waiters occupied the
request threadpool, /health went unanswered, and the supervisor restarted the
process after its probes failed. That killed the gate, which recorded nothing,
and the cycle repeated: 147 consecutive failed probes and 225 restarts were
logged before the dump was taken.

The lock's own docstring says the epoch row "remains the durable barrier after
this short creation critical section ends". Only the READ has to be atomic
against epoch creation; the publication that follows does not.
"""

from __future__ import annotations

import inspect
import re

from mac import services


def _serializer_source() -> str:
    return inspect.getsource(services._serialize_runtime_source_publication)


def test_the_publication_runs_outside_the_barrier():
    """The live failure: the gate ran inside the `with`, so the lock was held
    for its entire duration and every peer publication blocked on it."""
    source = _serializer_source()

    with_line = None
    call_line = None
    for index, line in enumerate(source.splitlines()):
        if "publication_serialization()" in line:
            with_line = index
        if re.search(r"return function\(self, task_id, target, evidence_id\)", line):
            call_line = index

    assert with_line is not None and call_line is not None
    body_indent = len(source.splitlines()[with_line]) - len(
        source.splitlines()[with_line].lstrip()
    )
    call_indent = len(source.splitlines()[call_line]) - len(
        source.splitlines()[call_line].lstrip()
    )

    assert call_indent <= body_indent, (
        "the publication is still inside the barrier's `with` block; holding it "
        "across a 45-90 minute contract gate starves the request threadpool and "
        "gets the hub restarted mid-publication"
    )


def test_the_barrier_is_still_consulted():
    """Releasing the lock early must not skip the check. A publication during
    an open release epoch is exactly what the barrier exists to defer."""
    source = _serializer_source()

    assert "publication_serialization()" in source
    assert "active_publication_barrier()" in source
    assert "PublicationDeferredError" in source


def test_the_deferral_still_raises_outside_the_lock():
    """The raise moved out of the `with`, so it must still happen -- a deferred
    publication that proceeded anyway would mutate the deployed checkout during
    a release."""
    source = _serializer_source()
    lines = source.splitlines()

    raise_idx = next(
        i for i, line in enumerate(lines) if "PublicationDeferredError" in line
    )
    guard_idx = next(
        i for i, line in enumerate(lines) if "if barrier is not None:" in line
    )

    assert guard_idx < raise_idx
