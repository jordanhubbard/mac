"""The hub's event loop is measured, not inferred.

task_c429b062 narrowed a nine-minute hang by elimination -- not the threadpool
(54 of 65 threads idle), not the publication barrier (0 waiters), not Postgres
(1 active connection, longest query 0s) -- and the CLI's "Connection refused"
pointed at the ACCEPT path, which uvicorn runs on the event loop.

But `py-spy dump` without `--native` cannot see inside a C frame, so "the loop
is blocked" stayed a hypothesis. These tests cover the measurement that settles
it, and the property that matters most: the detector must not cause the fault
it measures.
"""

from __future__ import annotations

import asyncio

import pytest

from mac.loop_stall_detector import (
    DEFAULT_INTERVAL,
    DEFAULT_THRESHOLD,
    OBSERVABILITY_NAME,
    LoopStallDetector,
    _float_env,
)


class _Recorder:
    def __init__(self):
        self.calls = []

    def record_log(self, name, **kwargs):
        self.calls.append((name, kwargs))


class _Clock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_a_punctual_beat_reports_nothing():
    cp = _Recorder()
    clock = _Clock()
    det = LoopStallDetector(cp, interval=0.01, threshold=1.0, clock=clock)

    async def scenario():
        det.start()
        for _ in range(3):
            await asyncio.sleep(0.02)
            clock.now += 0.01  # exactly the interval: no gap
        det.stop()

    asyncio.run(scenario())
    assert det.stall_count == 0
    assert cp.calls == []


def test_a_gap_is_measured_and_recorded():
    cp = _Recorder()
    clock = _Clock()
    det = LoopStallDetector(cp, interval=0.01, threshold=0.5, clock=clock)

    async def scenario():
        det.start()
        await asyncio.sleep(0.03)
        clock.now += 47.0  # the loop did not run for 47 seconds
        await asyncio.sleep(0.05)
        det.stop()

    asyncio.run(scenario())
    assert det.stall_count >= 1
    assert det.worst_gap_seconds > 45
    assert cp.calls, "a stall must be recorded, not only logged"
    name, kwargs = cp.calls[0]
    assert name == OBSERVABILITY_NAME
    assert kwargs["level"] == "warning"
    assert kwargs["detail"]["gap_seconds"] > 45


def test_recording_happens_off_the_event_loop():
    """The property that makes this detector safe to run in production.

    `record_log` is synchronous database I/O. Doing it on the loop would block
    the loop -- the exact defect under investigation. A detector that caused
    the fault it measures would be worse than no detector, so the write must
    land on a worker thread.
    """
    seen_threads = []

    class _ThreadWatchingRecorder:
        def record_log(self, name, **kwargs):
            import threading

            seen_threads.append(threading.current_thread().name)

    clock = _Clock()
    det = LoopStallDetector(_ThreadWatchingRecorder(), interval=0.01, threshold=0.5, clock=clock)

    async def scenario():
        det.start()
        await asyncio.sleep(0.03)
        clock.now += 10.0
        await asyncio.sleep(0.05)
        det.stop()

    asyncio.run(scenario())
    assert seen_threads, "nothing was recorded"
    import threading

    assert all(t != threading.current_thread().name for t in seen_threads), (
        "record_log ran on the event loop thread: the detector would itself "
        "stall the loop it measures"
    )


def test_a_failing_recorder_never_breaks_the_hub():
    class _Broken:
        def record_log(self, *a, **k):
            raise RuntimeError("observability is down")

    clock = _Clock()
    det = LoopStallDetector(_Broken(), interval=0.01, threshold=0.5, clock=clock)

    async def scenario():
        det.start()
        await asyncio.sleep(0.03)
        clock.now += 10.0
        await asyncio.sleep(0.05)
        det.stop()

    asyncio.run(scenario())
    # The stall is still counted in-process even though persisting it failed.
    assert det.stall_count >= 1


def test_start_without_a_running_loop_is_a_no_op():
    """App construction and sync callers must not fail for a diagnostic."""
    det = LoopStallDetector(_Recorder())
    det.start()  # no running loop
    det.stop()
    assert det.stall_count == 0


def test_thresholds_come_from_the_environment_and_reject_nonsense():
    assert _float_env("X", 1.5, {}) == 1.5
    assert _float_env("X", 1.5, {"X": "4"}) == 4.0
    # Zero or negative would mean "report every beat" or never sleep.
    assert _float_env("X", 1.5, {"X": "0"}) == 1.5
    assert _float_env("X", 1.5, {"X": "-3"}) == 1.5
    assert _float_env("X", 1.5, {"X": "banana"}) == 1.5
    assert DEFAULT_THRESHOLD > DEFAULT_INTERVAL
