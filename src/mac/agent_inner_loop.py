"""Bounded local continuation policy for persistent MAC workers.

The worker owns *when to try again*; the hub still owns *whether it may work*.
Every wake therefore returns to the normal heartbeat, hold, claim, and lease
checks instead of carrying authority across iterations.
"""

from __future__ import annotations

from dataclasses import dataclass

PROGRESS_STATUSES = frozenset(
    {
        "blocked",
        "completed",
        "decomposed",
        "failed",
        "needs_review",
        "review_nudge_invalid",
        "review_verdict_failed",
        "stale_result",
        "submitted_for_review",
    }
)
WAITING_STATUSES = frozenset({"held"})
TERMINAL_STATUSES = frozenset({"self_update_restart"})


@dataclass(frozen=True)
class InnerLoopDecision:
    """One local scheduling decision, with no task or lease authority."""

    mode: str
    delay_seconds: float
    streak: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "mac.agent_inner_loop.v1",
            "mode": self.mode,
            "delay_seconds": self.delay_seconds,
            "streak": self.streak,
            "reason": self.reason,
        }


class PersistentAgentLoop:
    """Schedule immediate continuation or a bounded self-wake.

    Productive iterations continue immediately. Empty and failed iterations
    back off exponentially to avoid hot polling. A durable hub hold is treated
    as explicit waiting and stays at the cap until the hub releases it.
    """

    def __init__(
        self,
        *,
        base_delay_seconds: float,
        max_delay_seconds: float,
    ) -> None:
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(self.base_delay_seconds, float(max_delay_seconds))
        self._empty_streak = 0
        self._error_streak = 0

    def observe(self, status: str) -> InnerLoopDecision:
        normalized = str(status or "error").strip().lower()
        if normalized in TERMINAL_STATUSES:
            self._reset()
            return InnerLoopDecision("stopped", 0.0, 0, normalized)
        if normalized in WAITING_STATUSES:
            self._empty_streak = 0
            self._error_streak = 0
            return InnerLoopDecision(
                "waiting",
                self.max_delay_seconds,
                0,
                normalized,
            )
        if normalized == "no_task":
            self._error_streak = 0
            self._empty_streak += 1
            return InnerLoopDecision(
                "idle",
                self._bounded_delay(self._empty_streak),
                self._empty_streak,
                normalized,
            )
        if normalized == "error":
            self._empty_streak = 0
            self._error_streak += 1
            return InnerLoopDecision(
                "recovering",
                self._bounded_delay(self._error_streak),
                self._error_streak,
                normalized,
            )

        self._reset()
        return InnerLoopDecision(
            "continuing",
            0.0,
            0,
            normalized if normalized in PROGRESS_STATUSES else "work_observed",
        )

    def _bounded_delay(self, streak: int) -> float:
        if self.base_delay_seconds == 0:
            return 0.0
        exponent = min(max(0, streak - 1), 30)
        return min(self.max_delay_seconds, self.base_delay_seconds * (2**exponent))

    def _reset(self) -> None:
        self._empty_streak = 0
        self._error_streak = 0
