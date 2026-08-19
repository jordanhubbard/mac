"""Measure whether the hub's asyncio event loop stops running.

WHY THIS EXISTS

The hub wedges: probes fail for minutes and the supervisor eventually restarts
it. task_c429b062 narrowed the cause by elimination, and a thread dump taken
during a nine-minute hang ruled out everything obvious:

    65 threads, 54 idle          the request threadpool is NOT exhausted
    0 in publication_serialization   not the barrier fixed in #359
    Postgres: 12 conns, 1 active, longest query 0s   not the database

Meanwhile the CLI saw "Connection reset by peer" and "Connection refused" --
the ACCEPT path, not a slow handler. uvicorn accepts connections on the event
loop, so if anything runs blocking work there, accepts stall while worker
threads sit idle. That is exactly the observed shape.

But it was inference. `py-spy dump` without `--native` cannot show what the
loop thread is executing inside a C frame, so "the loop is blocked" remained a
hypothesis that no measurement confirmed. This module is the measurement the
task asked for: a heartbeat scheduled ON the loop, so a gap between beats is
time the loop was not running anything else.

WHAT A GAP MEANS, AND WHAT IT DOES NOT

A gap proves the loop did not get to us for that long. It does NOT say what
occupied it -- that still needs `py-spy dump --native`, or a narrowing of which
coroutine ran between two beats. What it does provide is the trigger: something
that knows a stall is happening WHILE it happens, rather than a supervisor
restart afterwards and a dump nobody caught in time.

THE RECORDING RUNS OFF THE LOOP, DELIBERATELY

Writing the observation to Postgres from the loop would be synchronous I/O on
the event loop -- the precise defect under investigation. A detector that
caused the fault it measures would be worse than none, so the beat happens on
the loop and the write happens in a thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

LOGGER = logging.getLogger("mac.loop_stall")

#: Seconds between heartbeats. One second is frequent enough to bound the
#: attribution window without being a meaningful load: the coroutine does a
#: clock read and a comparison.
INTERVAL_ENV = "MAC_LOOP_HEARTBEAT_SECONDS"
DEFAULT_INTERVAL = 1.0

#: A beat later than interval + this is reported. Ordinary scheduling noise on
#: a busy loop is milliseconds; the hangs under investigation are minutes. The
#: default is deliberately far above the noise and far below the fault, so a
#: report means something happened rather than that the box was busy.
THRESHOLD_ENV = "MAC_LOOP_STALL_THRESHOLD_SECONDS"
DEFAULT_THRESHOLD = 2.0

OBSERVABILITY_NAME = "hub.event_loop.stalled"


def _float_env(name: str, default: float, environ: Optional[dict] = None) -> float:
    raw = (environ if environ is not None else os.environ).get(name)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class LoopStallDetector:
    """Heartbeat on the event loop; report the gaps.

    Start/stop are shaped like the other lifespan services so it can join that
    list and be unwound with them.
    """

    def __init__(
        self,
        control_plane: Any,
        *,
        interval: Optional[float] = None,
        threshold: Optional[float] = None,
        environ: Optional[dict] = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._cp = control_plane
        self._interval = (
            interval
            if interval is not None
            else _float_env(INTERVAL_ENV, DEFAULT_INTERVAL, environ)
        )
        self._threshold = (
            threshold
            if threshold is not None
            else _float_env(THRESHOLD_ENV, DEFAULT_THRESHOLD, environ)
        )
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        #: Secret-free, in-process summary for tests and for /health-style
        #: callers that want the number without querying observability.
        self.worst_gap_seconds = 0.0
        self.stall_count = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a sync caller, or a test constructing the app). There is
            # nothing to measure, and refusing loudly here would break app
            # construction for a diagnostic.
            LOGGER.debug("no running event loop; loop stall detector not started")
            return
        self._task = loop.create_task(self._run(), name="mac-loop-stall-detector")

    def stop(self) -> None:
        self._stop = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    # -- the beat ----------------------------------------------------------

    async def _run(self) -> None:
        last = self._clock()
        while not self._stop:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            now = self._clock()
            elapsed = now - last
            last = now
            gap = elapsed - self._interval
            if gap <= self._threshold:
                continue
            self.stall_count += 1
            self.worst_gap_seconds = max(self.worst_gap_seconds, gap)
            LOGGER.warning(
                "event loop stalled for %.1fs (expected a beat every %.1fs)",
                gap,
                self._interval,
            )
            await self._record(gap, elapsed)

    async def _record(self, gap: float, elapsed: float) -> None:
        """Persist the observation WITHOUT touching the loop.

        `record_log` is synchronous database I/O. Calling it here would block
        the event loop -- the exact fault this detector exists to find -- so it
        goes to a thread. A detector that caused the defect it measures would
        be worse than no detector.
        """
        record = getattr(self._cp, "record_log", None)
        if record is None:
            return
        try:
            await asyncio.to_thread(
                record,
                OBSERVABILITY_NAME,
                layer="control_plane",
                source="hub",
                level="warning",
                detail={
                    "gap_seconds": round(gap, 3),
                    "elapsed_seconds": round(elapsed, 3),
                    "interval_seconds": self._interval,
                    "threshold_seconds": self._threshold,
                    "stall_count": self.stall_count,
                    # Names the hypothesis this measurement tests, so a reader
                    # of the row knows what to do with it.
                    "next_step": (
                        "py-spy dump --native on the hub pid during a gap: the loop "
                        "thread's C frames name what ran between two beats"
                    ),
                },
            )
        except Exception:  # noqa: BLE001 - a diagnostic must never break the hub
            LOGGER.warning("failed to record loop stall", exc_info=True)
