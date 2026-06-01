"""th-merge-03: provider routing with a *recovering* circuit breaker.

This is the routing brain of the optional in-mac model router (ADR 0003). It
exists because of a concrete, observed failure: TokenHub had a single configured
provider whose circuit breaker tripped on a transient error and **never
half-opened to recover** — so even after the upstream was healthy again, every
completion was skipped/hung and the whole fleet went silent.

The fixes are requirements here, not afterthoughts:

* **Multiple providers** selected by priority (then name), health-aware.
* **A breaker that recovers**: CLOSED → (failures) → OPEN → (cooldown) →
  HALF_OPEN (allow a probe) → (success) CLOSED, or (probe fails) back to OPEN
  with the cooldown restarted. The half-open re-probe is the bit TokenHub was
  missing.
* **Fail-fast**: when every eligible provider is OPEN, ``select()`` returns
  ``None`` so the caller errors *immediately* instead of hanging on a dead or
  cooling-down upstream.

It is intentionally transport-agnostic: it decides *which* provider to use and
records success/failure. The HTTP front door (th-merge-02) calls ``select`` /
``record_*`` around each upstream request; the ``clock`` seam keeps the
time-based breaker logic deterministically testable.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["BreakerState", "Provider", "ProviderRouter", "AllProvidersDownError"]


class BreakerState(str, Enum):
    CLOSED = "closed"      # healthy; route freely
    OPEN = "open"          # tripped; skip until cooldown elapses
    HALF_OPEN = "half_open"  # cooldown elapsed; allow limited probes to recover


class AllProvidersDownError(RuntimeError):
    """Raised by callers that prefer an exception to a None select()."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    priority: int = 0                 # lower is preferred
    models: Tuple[str, ...] = ("*",)  # which model ids it serves; "*" = any
    enabled: bool = True


@dataclass
class _Status:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_inflight: int = 0
    total_successes: int = 0
    total_failures: int = 0


class ProviderRouter:
    """Health-aware provider selection with a recovering circuit breaker.

    Thread-safe: a hub serves many concurrent agents, so selection + breaker
    bookkeeping take a lock.
    """

    def __init__(
        self,
        providers: List[Provider],
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        half_open_max_probes: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("ProviderRouter needs at least one provider")
        self._providers = {p.name: p for p in providers}
        self._order = sorted(providers, key=lambda p: (p.priority, p.name))
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown = float(cooldown_seconds)
        self._half_open_max = max(1, int(half_open_max_probes))
        self._clock = clock
        self._status: Dict[str, _Status] = {p.name: _Status() for p in providers}
        self._lock = threading.RLock()

    # -- selection -----------------------------------------------------------

    @staticmethod
    def _serves(provider: Provider, model: str) -> bool:
        return model == "*" or "*" in provider.models or model in provider.models

    def _allow_locked(self, name: str) -> bool:
        st = self._status[name]
        if st.state is BreakerState.OPEN:
            if (self._clock() - st.opened_at) >= self._cooldown:
                st.state = BreakerState.HALF_OPEN  # cooldown elapsed → try to recover
                st.half_open_inflight = 0
            else:
                return False
        if st.state is BreakerState.HALF_OPEN:
            if st.half_open_inflight < self._half_open_max:
                st.half_open_inflight += 1
                return True
            return False
        return True  # CLOSED

    def select(self, model: str = "*") -> Optional[Provider]:
        """Return the preferred eligible provider for ``model``, or None when
        every eligible provider is open (fail-fast — never hang)."""
        with self._lock:
            for provider in self._order:
                if not provider.enabled:
                    continue
                if not self._serves(provider, model):
                    continue
                if self._allow_locked(provider.name):
                    return provider
            return None

    def select_or_raise(self, model: str = "*") -> Provider:
        chosen = self.select(model)
        if chosen is None:
            raise AllProvidersDownError("no eligible provider available for model=%s" % model)
        return chosen

    # -- outcome reporting ---------------------------------------------------

    def record_success(self, name: str) -> None:
        with self._lock:
            st = self._status.get(name)
            if st is None:
                return
            st.total_successes += 1
            st.consecutive_failures = 0
            st.half_open_inflight = 0
            st.state = BreakerState.CLOSED  # a success closes the breaker (recovery)

    def record_failure(self, name: str) -> None:
        with self._lock:
            st = self._status.get(name)
            if st is None:
                return
            st.total_failures += 1
            st.consecutive_failures += 1
            if st.state is BreakerState.HALF_OPEN:
                # the recovery probe failed → reopen and restart the cooldown
                st.state = BreakerState.OPEN
                st.opened_at = self._clock()
                st.half_open_inflight = 0
            elif st.consecutive_failures >= self._failure_threshold:
                st.state = BreakerState.OPEN
                st.opened_at = self._clock()

    # -- introspection (for `mac router status` / observability) -------------

    def status(self) -> Dict[str, Dict[str, object]]:
        with self._lock:
            # touch breaker state so OPEN providers past cooldown report half_open
            out: Dict[str, Dict[str, object]] = {}
            for name, st in self._status.items():
                if st.state is BreakerState.OPEN and (self._clock() - st.opened_at) >= self._cooldown:
                    state = BreakerState.HALF_OPEN.value
                else:
                    state = st.state.value
                p = self._providers[name]
                out[name] = {
                    "state": state,
                    "enabled": p.enabled,
                    "priority": p.priority,
                    "consecutive_failures": st.consecutive_failures,
                    "total_successes": st.total_successes,
                    "total_failures": st.total_failures,
                }
            return out

    def any_available(self, model: str = "*") -> bool:
        return self.select(model) is not None


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def providers_from_env(env: Optional[Dict[str, str]] = None) -> List[Provider]:
    """Parse ``MAC_ROUTER_PROVIDERS`` into Providers.

    Format (semicolon-separated providers, comma-separated fields):
      ``name=base_url[,priority][,models=a|b|*]``
    e.g. ``nvidia=https://inference-api.nvidia.com/v1,0,models=*;`` +
           ``openai=https://api.openai.com/v1,1,models=*``
    """
    env = env or os.environ
    raw = (env.get("MAC_ROUTER_PROVIDERS") or "").strip()
    providers: List[Provider] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, rest = chunk.partition("=")
        fields = [f.strip() for f in rest.split(",") if f.strip()]
        if not fields:
            continue
        base_url = fields[0]
        priority = 0
        models: Tuple[str, ...] = ("*",)
        for f in fields[1:]:
            if f.startswith("models="):
                models = tuple(m for m in f[len("models="):].split("|") if m) or ("*",)
            elif f.isdigit():
                priority = int(f)
        providers.append(Provider(name=name.strip(), base_url=base_url, priority=priority, models=models))
    return providers
