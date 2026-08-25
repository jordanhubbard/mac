"""The fleet-wide ordered coding-route ladder (ADR 0029).

Why this exists
---------------
During development the fleet owner repeatedly moved work across Codex, Claude,
Cursor, OpenCode and Pi as monthly account credits ran out. Each move was a
change to *per-worker environment* — a pinned ``MAC_CODING_AGENT``, an exported
key, a hand-edited unit file — so the fleet's real route search path lived
nowhere, was known to no one, and was re-derived by every worker from whatever
happened to be set on that host. When one account capped, every worker spent
its own turns rediscovering the cap.

This module makes that search path a **contract**: an owner-supplied ordered
list of route identities (``mac.coding_route_ladder.v1``), a closed set of
failure classes, and secret-free availability outcomes
(``mac.route_availability.v1``) that agents publish so the fleet converges
without each worker paying to learn the same thing.

The pieces, and the rules that make them honest
-----------------------------------------------
* **Route identity** (:class:`RouteKey`) is harness/CLI type + credential
  SOURCE NAME + provider + model + endpoint class. It never contains a token,
  an account identifier, a balance, or an authenticated URL — see
  :func:`assert_secret_free`, which is enforced at construction, not at
  publication, so an unsafe identity cannot exist to be leaked.

* **Order is the owner's** (:class:`RouteLadder.effective_order`). Rank 0 is
  cheapest/most preferred. Within an equal rank the harness/CLI type is the
  major key and the model is only a tie-breaker. Nothing here infers price from
  a model name; cost that was not measured is ``unknown``, never ``0``.

* **Failure class decides whether the ladder moves at all.** Only the
  route-availability classes in :data:`ROUTE_AVAILABILITY_FAILURES` suppress a
  route. A task whose tests failed is a ``semantic`` outcome: the route worked,
  and demoting it would be a lie that costs the fleet its cheapest option.

* **Suppression reuses the recovering breaker in** :mod:`mac.provider_router`,
  rather than inventing a second, disconnected health policy. That breaker
  already has the property this needs and that the retired standalone service
  lacked: OPEN → cooldown → HALF_OPEN probe → CLOSED. The ladder adds only what
  the breaker could not know — a per-failure-class dwell time, so an account cap
  is not re-proven every thirty seconds while a 429 still is.

* **Switching happens at a turn boundary, never mid-turn.**
  :meth:`RouteLadder.begin_turn` pins the route for the duration of a turn;
  :meth:`RouteLadder.pending_switch` reports the cheaper route that a peer's
  success announcement has made eligible. An in-flight executor keeps the route
  it started on even when a cheaper one comes back, because changing harness
  under a running turn loses the turn.

* **The hub is the authority; AgentBus only accelerates.** :meth:`apply_outcome`
  folds a peer's report into local state, but a route is never *selected*
  because a peer could use it: :meth:`select` still requires the route to be
  locally capable (:class:`RouteCapability`). Fleet learning removes options; it
  never grants one the worker cannot actually run.

Deliberately stdlib-only and free of network, clock, and hub dependencies, so
the hub, the worker, the executor, the reviewer, and the tests all agree on one
contract — the same discipline :mod:`mac.agentbus_outcomes` and
:mod:`mac.coding_agent` follow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from mac.provider_router import BreakerState, Provider, ProviderRouter

__all__ = [
    "LADDER_SCHEMA",
    "AVAILABILITY_SCHEMA",
    "TELEMETRY_SCHEMA",
    "FAILURE_QUOTA_EXHAUSTED",
    "FAILURE_RATE_LIMITED",
    "FAILURE_AUTH",
    "FAILURE_PROVIDER_OUTAGE",
    "FAILURE_MODEL_UNAVAILABLE",
    "FAILURE_TRANSPORT",
    "FAILURE_SEMANTIC",
    "FAILURE_CLASSES",
    "ROUTE_AVAILABILITY_FAILURES",
    "FLEET_SUPPRESSING_FAILURES",
    "ANY_CAPABILITY",
    "ENDPOINT_SUBSCRIPTION",
    "ENDPOINT_DIRECT_API",
    "ENDPOINT_SELF_HOSTED",
    "ENDPOINT_CLASSES",
    "UNKNOWN_COST",
    "OUTCOME_SUCCESS",
    "OUTCOME_FAILURE",
    "LADDER_ENV",
    "LADDER_FILE_ENV",
    "SecretLeakError",
    "LadderConfigError",
    "RouteKey",
    "LadderRoute",
    "LadderPolicy",
    "RouteCapability",
    "RouteCost",
    "RouteLadder",
    "assert_secret_free",
    "classify_route_failure",
    "cooldown_for_failure",
    "parse_ladder_document",
    "load_ladder",
    "ladder_harness_order",
    "route_availability_record",
    "parse_route_availability",
]

LADDER_SCHEMA = "mac.coding_route_ladder.v1"
AVAILABILITY_SCHEMA = "mac.route_availability.v1"
TELEMETRY_SCHEMA = "mac.route_ladder.telemetry.v1"

#: Owner-authored ladder: either an inline JSON document or a path to one.
LADDER_ENV = "MAC_CODING_ROUTE_LADDER"
#: Explicit path to the ladder document; wins over an inline value.
LADDER_FILE_ENV = "MAC_CODING_ROUTE_LADDER_FILE"


# --------------------------------------------------------------------------- #
# Failure classes
# --------------------------------------------------------------------------- #
# Closed set. A consumer switches on these; it never parses provider prose to
# decide whether the ladder should move.
FAILURE_QUOTA_EXHAUSTED = "quota_exhausted"  # monthly/account credits are gone
FAILURE_RATE_LIMITED = "rate_limited"  # transient throttle; retry soon
FAILURE_AUTH = "auth"  # credential missing/expired/denied
FAILURE_PROVIDER_OUTAGE = "provider_outage"  # upstream 5xx / declared incident
FAILURE_MODEL_UNAVAILABLE = "model_unavailable"  # route is fine, this model is not
FAILURE_TRANSPORT = "transport"  # harness/CLI/network broke
FAILURE_SEMANTIC = "semantic"  # the MODEL answered; the work failed

FAILURE_CLASSES = frozenset(
    {
        FAILURE_QUOTA_EXHAUSTED,
        FAILURE_RATE_LIMITED,
        FAILURE_AUTH,
        FAILURE_PROVIDER_OUTAGE,
        FAILURE_MODEL_UNAVAILABLE,
        FAILURE_TRANSPORT,
        FAILURE_SEMANTIC,
    }
)

#: The classes that say something about the ROUTE. Everything else says
#: something about the work, and must not cost a route its rank.
ROUTE_AVAILABILITY_FAILURES = frozenset(FAILURE_CLASSES - {FAILURE_SEMANTIC})

#: The subset that is a fact about the ACCOUNT and therefore travels between
#: workers. A capped or rejected credential is capped or rejected everywhere it
#: is used; a transport break, a throttle window and a provider's regional
#: outage are facts about the reporting host and must stay there. Broadcasting
#: those would let one sick worker suppress the fleet's cheapest route.
FLEET_SUPPRESSING_FAILURES = frozenset({FAILURE_QUOTA_EXHAUSTED, FAILURE_AUTH})

OUTCOME_SUCCESS = "success"
OUTCOME_FAILURE = "failure"

#: Bound on the free-text evidence carried in a published outcome. Enough to
#: recognise a failure, far too little to relay a provider's response body.
EVIDENCE_MAX_CHARS = 240


class SecretLeakError(ValueError):
    """A route identity or outcome carried something that must never be published."""


class LadderConfigError(ValueError):
    """The owner's ladder document is not usable as written."""


# --------------------------------------------------------------------------- #
# Secret-free enforcement
# --------------------------------------------------------------------------- #
# A route key names a credential SOURCE ("codex-oauth-file", "OPENCODE_API_KEY")
# — never its value. These patterns catch the ways a value gets in by accident:
# an authenticated URL, a query string, a bearer header, or a long opaque token
# pasted where a source name belongs.
_URL_USERINFO = re.compile(r"://[^/\s@]*@")
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")
_BEARER = re.compile(r"\bbearer\s+\S+", re.IGNORECASE)
_TOKEN_PREFIX = re.compile(
    r"\b(sk|pk|rk|ghp|gho|ghu|ghs|ghr|github_pat|xox[abposr]|AKIA|ASIA|"
    r"mac_worker|nvapi|AIza)[-_][A-Za-z0-9_\-]{8,}",
    re.IGNORECASE,
)
# A long unbroken high-entropy run. Real identity fields are words, dots,
# dashes and slashes; a 32-character alphanumeric blob is a credential.
_OPAQUE_BLOB = re.compile(r"[A-Za-z0-9+/_\-]{32,}")


def assert_secret_free(value: str, *, field_name: str) -> str:
    """Return ``value`` when it is safe to publish; raise otherwise.

    Enforced where identities are *constructed* rather than where they are
    published: a route whose fingerprint embeds a token is already a leak
    waiting for the first telemetry call, and the fingerprint is exactly what
    every consumer copies around.
    """
    text = str(value or "")
    if not text:
        return text
    if _URL_USERINFO.search(text):
        raise SecretLeakError("%s carries URL userinfo: refusing to publish" % field_name)
    if _QUERY_OR_FRAGMENT.search(text):
        raise SecretLeakError(
            "%s carries a query/fragment, which is where keys hide: refusing to publish"
            % field_name
        )
    if _BEARER.search(text):
        raise SecretLeakError("%s carries a bearer credential: refusing to publish" % field_name)
    if _TOKEN_PREFIX.search(text):
        raise SecretLeakError("%s looks like an API token: refusing to publish" % field_name)
    if _OPAQUE_BLOB.search(text):
        raise SecretLeakError(
            "%s contains a %d-character opaque run; route identities name a "
            "credential source, not its value" % (field_name, len(text))
        )
    return text


def _bounded_evidence(text: str) -> str:
    """Scrub and bound free-text evidence so it can ride the bus.

    Raw provider output is never republished. What survives is a short,
    redacted phrase an operator can recognise.
    """
    raw = " ".join(str(text or "").split())
    raw = _URL_USERINFO.sub("://[redacted]@", raw)
    raw = _BEARER.sub("bearer [redacted]", raw)
    raw = _TOKEN_PREFIX.sub("[redacted]", raw)
    raw = _OPAQUE_BLOB.sub("[redacted]", raw)
    if len(raw) > EVIDENCE_MAX_CHARS:
        raw = raw[: EVIDENCE_MAX_CHARS - 1].rstrip() + "…"
    return raw


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #
_QUOTA_TEXT = re.compile(
    r"(quota (?:exceeded|exhausted)|out of (?:credits?|quota)|"
    r"insufficient (?:credits?|quota|balance|funds)|credit balance|"
    r"monthly (?:limit|cap|allowance)|usage limit reached|"
    r"resource[_ ]exhausted|billing (?:hard )?limit|plan limit reached|"
    r"you(?:'ve| have) (?:reached|hit) your .{0,40}limit)",
    re.IGNORECASE,
)
_RATE_TEXT = re.compile(
    r"(rate[_ ]limit|too many requests|slow down|retry[- ]after|\b429\b|"
    r"overloaded|capacity constraints)",
    re.IGNORECASE,
)
_AUTH_TEXT = re.compile(
    r"(unauthorized|unauthenticated|forbidden|invalid[_ ]api[_ ]key|"
    r"authentication (?:failed|error)|not logged in|login required|"
    r"credential(?:s)? (?:expired|invalid|missing)|session expired|"
    r"\b401\b|\b403\b)",
    re.IGNORECASE,
)
_OUTAGE_TEXT = re.compile(
    r"(service unavailable|internal server error|bad gateway|gateway timeout|"
    r"upstream (?:error|unavailable)|provider (?:outage|incident)|"
    r"\b50[0234]\b)",
    re.IGNORECASE,
)
_MODEL_TEXT = re.compile(
    r"(model (?:not found|not available|unavailable|does not exist|is retired|"
    r"decommissioned)|unknown model|unsupported model|no such model)",
    re.IGNORECASE,
)
_TRANSPORT_TEXT = re.compile(
    r"(connection (?:refused|reset|closed|error)|network (?:error|unreachable)|"
    r"dns|timed? ?out|command not found|no such file or directory|"
    r"broken pipe|tls|certificate|proxy error|exec format error)",
    re.IGNORECASE,
)

# Order matters: a 429 body that also says "quota exceeded" is a cap, not a
# throttle, and treating it as a throttle is precisely the "spend turns proving
# the same cap" waste this ladder exists to stop.
_CLASSIFIERS: Tuple[Tuple[Any, str], ...] = (
    (_QUOTA_TEXT, FAILURE_QUOTA_EXHAUSTED),
    (_MODEL_TEXT, FAILURE_MODEL_UNAVAILABLE),
    (_AUTH_TEXT, FAILURE_AUTH),
    (_RATE_TEXT, FAILURE_RATE_LIMITED),
    (_OUTAGE_TEXT, FAILURE_PROVIDER_OUTAGE),
    (_TRANSPORT_TEXT, FAILURE_TRANSPORT),
)

_STATUS_CLASSES: Dict[int, str] = {
    401: FAILURE_AUTH,
    403: FAILURE_AUTH,
    404: FAILURE_MODEL_UNAVAILABLE,
    429: FAILURE_RATE_LIMITED,
    500: FAILURE_PROVIDER_OUTAGE,
    502: FAILURE_PROVIDER_OUTAGE,
    503: FAILURE_PROVIDER_OUTAGE,
    504: FAILURE_PROVIDER_OUTAGE,
}


def classify_route_failure(
    text: str = "",
    *,
    status_code: Optional[int] = None,
    default: str = FAILURE_SEMANTIC,
) -> str:
    """Classify a failure into exactly one :data:`FAILURE_CLASSES` member.

    The default is deliberately :data:`FAILURE_SEMANTIC`: an error nobody
    recognised is not evidence that the route is unavailable, and guessing
    "unavailable" would demote the fleet's cheapest route on the strength of an
    unparsed string. Callers that *know* they are reporting a transport-level
    failure pass ``default=FAILURE_TRANSPORT``.

    Text wins over ``status_code`` because providers routinely return a monthly
    cap as HTTP 429 — the status says "throttled", the body says "you are out of
    credits", and only the body is true.
    """
    blob = str(text or "")
    for pattern, failure_class in _CLASSIFIERS:
        if pattern.search(blob):
            return failure_class
    if status_code is not None:
        mapped = _STATUS_CLASSES.get(int(status_code))
        if mapped:
            return mapped
    if default not in FAILURE_CLASSES:
        raise ValueError("unknown failure class default: %r" % default)
    return default


# --------------------------------------------------------------------------- #
# Route identity
# --------------------------------------------------------------------------- #
#: Endpoint classes. A *class*, not a URL: "which kind of endpoint does this
#: route talk to", which is what an operator needs and what is safe to publish.
ENDPOINT_SUBSCRIPTION = "subscription"  # CLI authenticated by a seat/plan
ENDPOINT_DIRECT_API = "direct_api"  # metered provider API
ENDPOINT_SELF_HOSTED = "self_hosted"  # fleet-operated inference
ENDPOINT_CLASSES = frozenset({ENDPOINT_SUBSCRIPTION, ENDPOINT_DIRECT_API, ENDPOINT_SELF_HOSTED})


@dataclass(frozen=True)
class RouteKey:
    """Secret-free identity of one executable coding route.

    Five fields, all of them names: the harness/CLI type, the credential
    *source* name, the provider, the model, and the endpoint class. Two workers
    that resolve the same five fields are on the same route, which is what makes
    one worker's quota-cap observation useful to every other worker.
    """

    harness: str
    credential_source: str
    provider: str
    model: str = ""
    endpoint_class: str = ENDPOINT_SUBSCRIPTION

    def __post_init__(self) -> None:
        for name in ("harness", "credential_source", "provider", "model", "endpoint_class"):
            value = str(getattr(self, name) or "").strip()
            assert_secret_free(value, field_name=name)
            object.__setattr__(self, name, value)
        if not self.harness:
            raise LadderConfigError("route harness is required")
        if not self.credential_source:
            raise LadderConfigError(
                "route credential_source is required: a route without a named "
                "credential source cannot be suppressed for the right account"
            )
        if self.endpoint_class and self.endpoint_class not in ENDPOINT_CLASSES:
            raise LadderConfigError(
                "unknown endpoint_class %r (expected one of %s)"
                % (self.endpoint_class, ", ".join(sorted(ENDPOINT_CLASSES)))
            )

    @property
    def identity(self) -> str:
        """Stable, human-legible identity string."""
        return "|".join(
            (
                self.harness,
                self.credential_source,
                self.provider,
                self.model or "*",
                self.endpoint_class,
            )
        )

    @property
    def credential_identity(self) -> str:
        """The harness+credential-source pair an account cap actually suppresses.

        A monthly cap belongs to the ACCOUNT, not to the model that happened to
        be requested when it was hit. Suppressing only the exact model would let
        the next worker re-prove the same cap with a different model on the same
        exhausted account.
        """
        return "%s|%s" % (self.harness, self.credential_source)

    def fingerprint(self) -> str:
        payload = {
            "harness": self.harness,
            "credential_source": self.credential_source,
            "provider": self.provider,
            "model": self.model,
            "endpoint_class": self.endpoint_class,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def observable(self) -> Dict[str, Any]:
        return {
            "harness": self.harness,
            "credential_source": self.credential_source,
            "provider": self.provider,
            "model": self.model or None,
            "endpoint_class": self.endpoint_class,
            "fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> "RouteKey":
        return cls(
            harness=str(doc.get("harness") or ""),
            credential_source=str(doc.get("credential_source") or ""),
            provider=str(doc.get("provider") or ""),
            model=str(doc.get("model") or ""),
            endpoint_class=str(doc.get("endpoint_class") or ENDPOINT_SUBSCRIPTION),
        )


@dataclass(frozen=True)
class LadderRoute:
    """One owner-ranked rung. ``rank`` 0 is cheapest/most preferred."""

    rank: int
    key: RouteKey
    enabled: bool = True
    note: str = ""

    @property
    def identity(self) -> str:
        return self.key.identity

    def observable(self) -> Dict[str, Any]:
        out = self.key.observable()
        out["rank"] = self.rank
        out["enabled"] = self.enabled
        if self.note:
            out["note"] = self.note
        return out


@dataclass(frozen=True)
class LadderPolicy:
    """Per-failure-class dwell times and breaker thresholds.

    Defaults encode the observed economics rather than a guess: a monthly cap
    is worth one hour of not asking again, a throttle is worth a minute, and a
    transport hiccup should still need a run of failures before it costs a
    route its place in the order.
    """

    quota_cooldown_seconds: float = 3600.0
    rate_limit_cooldown_seconds: float = 60.0
    auth_cooldown_seconds: float = 900.0
    outage_cooldown_seconds: float = 300.0
    model_unavailable_cooldown_seconds: float = 1800.0
    transport_cooldown_seconds: float = 120.0
    failure_threshold: int = 3
    half_open_max_probes: int = 1

    def observable(self) -> Dict[str, Any]:
        return {
            "quota_cooldown_seconds": self.quota_cooldown_seconds,
            "rate_limit_cooldown_seconds": self.rate_limit_cooldown_seconds,
            "auth_cooldown_seconds": self.auth_cooldown_seconds,
            "outage_cooldown_seconds": self.outage_cooldown_seconds,
            "model_unavailable_cooldown_seconds": self.model_unavailable_cooldown_seconds,
            "transport_cooldown_seconds": self.transport_cooldown_seconds,
            "failure_threshold": self.failure_threshold,
            "half_open_max_probes": self.half_open_max_probes,
        }

    @classmethod
    def from_dict(cls, doc: Optional[Mapping[str, Any]]) -> "LadderPolicy":
        doc = doc or {}
        defaults = cls()
        values: Dict[str, Any] = {}
        for name in defaults.observable():
            if name not in doc:
                continue
            raw = doc.get(name)
            try:
                values[name] = int(raw) if name.endswith(("threshold", "probes")) else float(raw)
            except (TypeError, ValueError) as exc:
                raise LadderConfigError("policy.%s is not a number: %r" % (name, raw)) from exc
            if values[name] < 0:
                raise LadderConfigError("policy.%s must not be negative" % name)
        return cls(**values)


#: Failure class -> (cooldown attribute, opens on a single failure).
#: A cap, a denied credential, a retired model and a throttle are each proven
#: by ONE response; only outages and transport noise need corroboration.
_FAILURE_POLICY: Dict[str, Tuple[str, bool]] = {
    FAILURE_QUOTA_EXHAUSTED: ("quota_cooldown_seconds", True),
    FAILURE_RATE_LIMITED: ("rate_limit_cooldown_seconds", True),
    FAILURE_AUTH: ("auth_cooldown_seconds", True),
    FAILURE_MODEL_UNAVAILABLE: ("model_unavailable_cooldown_seconds", True),
    FAILURE_PROVIDER_OUTAGE: ("outage_cooldown_seconds", False),
    FAILURE_TRANSPORT: ("transport_cooldown_seconds", False),
}


def cooldown_for_failure(failure_class: str, policy: LadderPolicy) -> Tuple[float, bool]:
    """Return ``(cooldown_seconds, opens_immediately)`` for a failure class.

    Raises for :data:`FAILURE_SEMANTIC`: a semantic failure has no cooldown
    because it never suppresses anything, and asking for one means a caller has
    confused "the work failed" with "the route failed".
    """
    if failure_class not in _FAILURE_POLICY:
        raise ValueError(
            "%r does not suppress a route; only %s do"
            % (failure_class, ", ".join(sorted(ROUTE_AVAILABILITY_FAILURES)))
        )
    attr, immediate = _FAILURE_POLICY[failure_class]
    return float(getattr(policy, attr)), immediate


# --------------------------------------------------------------------------- #
# Cost (advisory; unknown is not zero)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteCost:
    """A measured cost observation, or an explicit *unknown*.

    ``usd_per_million_tokens is None`` means nobody measured it. That is not
    ``0.0``, and the distinction is load-bearing: a subscription route with no
    Relay measurement would sort as free and win every tie-break it should have
    lost.
    """

    usd_per_million_tokens: Optional[float] = None
    observed_at: Optional[float] = None
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return self.usd_per_million_tokens is not None

    def observable(self) -> Dict[str, Any]:
        return {
            "known": self.known,
            "usd_per_million_tokens": (self.usd_per_million_tokens if self.known else None),
            "observed_at": self.observed_at,
            "source": self.source,
        }


UNKNOWN_COST = RouteCost()


# --------------------------------------------------------------------------- #
# Local capability
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RouteCapability:
    """What this worker can actually run, independent of what the fleet knows.

    ``harnesses`` are the CLI types verified present and authenticated here, and
    ``credential_sources`` the credential source names this worker actually
    holds. ``None`` means "unrestricted on this axis" — an explicitly empty set
    means "this worker can run nothing", and the two must not collapse into one
    value, because a worker that has lost every CLI credential looks exactly
    like an unconfigured one otherwise.

    Fleet learning may take a route away; it may never hand one over.
    """

    harnesses: Optional[frozenset] = None
    credential_sources: Optional[frozenset] = None

    def permits(self, route: LadderRoute) -> bool:
        if self.harnesses is not None and route.key.harness not in self.harnesses:
            return False
        if self.credential_sources is None:
            return True
        return route.key.credential_source in self.credential_sources

    @classmethod
    def from_iterables(
        cls,
        harnesses: Optional[Iterable[str]] = None,
        credential_sources: Optional[Iterable[str]] = None,
    ) -> "RouteCapability":
        def _frozen(values: Optional[Iterable[str]]) -> Optional[frozenset]:
            if values is None:
                return None
            return frozenset(str(v).strip() for v in values if str(v).strip())

        return cls(harnesses=_frozen(harnesses), credential_sources=_frozen(credential_sources))


#: The unrestricted capability: used by the hub and by tests, where "what this
#: host has installed" is not the question being asked.
ANY_CAPABILITY = RouteCapability()


# --------------------------------------------------------------------------- #
# Ladder document
# --------------------------------------------------------------------------- #
def parse_ladder_document(doc: Mapping[str, Any]) -> Tuple[List[LadderRoute], LadderPolicy]:
    """Validate a ``mac.coding_route_ladder.v1`` document.

    Fails closed on anything ambiguous. A ladder that half-parses is worse than
    no ladder: the fleet would run on a route order nobody wrote.
    """
    if not isinstance(doc, Mapping):
        raise LadderConfigError("ladder document must be a mapping")
    schema = str(doc.get("schema") or "").strip()
    if schema != LADDER_SCHEMA:
        raise LadderConfigError(
            "ladder document schema is %r; expected %r" % (schema, LADDER_SCHEMA)
        )
    raw_routes = doc.get("routes")
    if not isinstance(raw_routes, Sequence) or isinstance(raw_routes, (str, bytes)):
        raise LadderConfigError("ladder document needs a 'routes' list")
    if not raw_routes:
        raise LadderConfigError("ladder document has no routes")

    policy = LadderPolicy.from_dict(doc.get("policy"))
    routes: List[LadderRoute] = []
    seen: Dict[str, int] = {}
    for index, entry in enumerate(raw_routes):
        if not isinstance(entry, Mapping):
            raise LadderConfigError("routes[%d] is not a mapping" % index)
        if "rank" in entry:
            try:
                rank = int(entry["rank"])
            except (TypeError, ValueError) as exc:
                raise LadderConfigError(
                    "routes[%d].rank is not an integer: %r" % (index, entry["rank"])
                ) from exc
        else:
            # Absent rank means "the position I wrote it in", which is what an
            # owner means when they hand over an ordered list.
            rank = index
        if rank < 0:
            raise LadderConfigError("routes[%d].rank must not be negative" % index)
        key = RouteKey.from_dict(entry)
        route = LadderRoute(
            rank=rank,
            key=key,
            enabled=bool(entry.get("enabled", True)),
            note=_bounded_evidence(str(entry.get("note") or "")),
        )
        if route.identity in seen:
            raise LadderConfigError(
                "routes[%d] duplicates routes[%d] (%s); one identity cannot hold "
                "two ranks" % (index, seen[route.identity], route.identity)
            )
        seen[route.identity] = index
        routes.append(route)
    return routes, policy


def load_ladder(
    env: Optional[Mapping[str, str]] = None,
    *,
    read_text: Optional[Callable[[str], str]] = None,
) -> Optional[Tuple[List[LadderRoute], LadderPolicy]]:
    """Load the owner's ladder from the environment, or ``None`` when unset.

    ``MAC_CODING_ROUTE_LADDER_FILE`` names a document; ``MAC_CODING_ROUTE_LADDER``
    holds either an inline JSON document or a path to one. Unset means "no owner
    ladder configured" — callers keep their built-in order rather than inventing
    one, and say so in their rationale.
    """
    env = os.environ if env is None else env
    reader = read_text or (lambda path: Path(path).expanduser().read_text(encoding="utf-8"))

    path = str(env.get(LADDER_FILE_ENV) or "").strip()
    inline = str(env.get(LADDER_ENV) or "").strip()
    raw = ""
    origin = ""
    if path:
        origin = LADDER_FILE_ENV
        try:
            raw = reader(path)
        except OSError as exc:
            raise LadderConfigError("%s=%s is unreadable: %s" % (LADDER_FILE_ENV, path, exc))
    elif inline.startswith("{"):
        origin = LADDER_ENV
        raw = inline
    elif inline:
        origin = LADDER_ENV
        try:
            raw = reader(inline)
        except OSError as exc:
            raise LadderConfigError(
                "%s=%s is neither inline JSON nor a readable path: %s" % (LADDER_ENV, inline, exc)
            )
    else:
        return None

    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise LadderConfigError("%s does not contain valid JSON: %s" % (origin, exc))
    return parse_ladder_document(doc)


def ladder_harness_order(routes: Sequence[LadderRoute]) -> Tuple[str, ...]:
    """Harness/CLI types in ladder order, de-duplicated, first appearance wins.

    This is the projection a harness-granular selector consumes (see
    :func:`mac.coding_agent.resolve_coding_agent`): the owner's ordering of
    *routes* implies an ordering of *CLIs*, and both must come from the same
    document so a worker and the ladder can never disagree about what is
    cheapest.
    """
    order: List[str] = []
    for route in sorted(routes, key=_order_key):
        if not route.enabled:
            continue
        if route.key.harness not in order:
            order.append(route.key.harness)
    return tuple(order)


def _order_key(route: LadderRoute) -> Tuple[int, str, str]:
    """Sort key: owner rank, then harness, then model.

    Harness is the major tie-break and model is only the last one, per the
    owner's rule. Nothing here reads the model to *infer* cost — it is a
    deterministic tie-break so two workers reading one document pick the same
    route, not a price signal.
    """
    return (route.rank, route.key.harness, route.key.model)


# --------------------------------------------------------------------------- #
# Availability outcomes
# --------------------------------------------------------------------------- #
def route_availability_record(
    route: LadderRoute,
    *,
    outcome: str,
    agent_id: str,
    observed_at: float,
    failure_class: Optional[str] = None,
    cooldown_until: Optional[float] = None,
    evidence: str = "",
) -> Dict[str, Any]:
    """Build a publishable ``mac.route_availability.v1`` record.

    Secret-free by construction: the identity is a :class:`RouteKey` (already
    validated) and the evidence is scrubbed and bounded. Nothing else from the
    provider's response travels.
    """
    if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE):
        raise ValueError("outcome must be %r or %r" % (OUTCOME_SUCCESS, OUTCOME_FAILURE))
    if outcome == OUTCOME_FAILURE:
        if failure_class not in FAILURE_CLASSES:
            raise ValueError("failure outcome needs a known failure_class")
    elif failure_class is not None:
        raise ValueError("a success outcome must not carry a failure_class")
    record: Dict[str, Any] = {
        "schema": AVAILABILITY_SCHEMA,
        "route": route.key.observable(),
        "rank": route.rank,
        "agent_id": assert_secret_free(str(agent_id or ""), field_name="agent_id"),
        "outcome": outcome,
        "failure_class": failure_class,
        "affects_availability": bool(
            failure_class in ROUTE_AVAILABILITY_FAILURES if failure_class else True
        ),
        "observed_at": float(observed_at),
        "cooldown_until": (None if cooldown_until is None else float(cooldown_until)),
        "evidence": _bounded_evidence(evidence),
    }
    return record


def parse_route_availability(doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate a received ``mac.route_availability.v1`` record.

    A peer's report is untrusted input. It is validated to the same standard as
    a locally produced one — including reconstructing the :class:`RouteKey`,
    which re-runs the secret-free check on every field before any of it is
    stored or re-published.
    """
    if not isinstance(doc, Mapping):
        raise ValueError("route availability record must be a mapping")
    if str(doc.get("schema") or "") != AVAILABILITY_SCHEMA:
        raise ValueError("record schema is not %s" % AVAILABILITY_SCHEMA)
    route_doc = doc.get("route")
    if not isinstance(route_doc, Mapping):
        raise ValueError("record has no route identity")
    key = RouteKey.from_dict(route_doc)
    outcome = str(doc.get("outcome") or "")
    if outcome not in (OUTCOME_SUCCESS, OUTCOME_FAILURE):
        raise ValueError("record outcome must be success or failure")
    failure_class = doc.get("failure_class")
    if outcome == OUTCOME_FAILURE:
        if failure_class not in FAILURE_CLASSES:
            raise ValueError("failure record carries unknown failure_class %r" % failure_class)
    else:
        failure_class = None
    try:
        observed_at = float(doc.get("observed_at"))
    except (TypeError, ValueError):
        raise ValueError("record observed_at is not a timestamp") from None
    try:
        rank = int(doc.get("rank"))
    except (TypeError, ValueError):
        rank = 0
    return {
        "schema": AVAILABILITY_SCHEMA,
        "key": key,
        "rank": rank,
        "agent_id": str(doc.get("agent_id") or ""),
        "outcome": outcome,
        "failure_class": failure_class,
        "observed_at": observed_at,
        "cooldown_until": (
            None if doc.get("cooldown_until") is None else float(doc["cooldown_until"])
        ),
        "evidence": _bounded_evidence(str(doc.get("evidence") or "")),
    }


# --------------------------------------------------------------------------- #
# The ladder
# --------------------------------------------------------------------------- #
@dataclass
class _RouteState:
    suppressed_reason: str = ""
    suppressed_by: str = ""
    suppressed_at: Optional[float] = None
    cooldown_until: Optional[float] = None
    last_success_at: Optional[float] = None
    last_success_by: str = ""
    cost: RouteCost = UNKNOWN_COST


class RouteLadder:
    """Ordered route selection with fleet-shared, class-aware suppression.

    Selection is a pure function of three inputs: the owner's order, what this
    worker can locally run, and the breaker state that local and peer outcomes
    have produced. Selection never consults cost directly — Relay measurements
    break ties *within* an owner rank and nothing more (see
    :meth:`record_cost`), because a cost signal that can reorder the owner's
    ladder is a cost signal that silently overrides the owner.
    """

    def __init__(
        self,
        routes: Sequence[LadderRoute],
        *,
        policy: Optional[LadderPolicy] = None,
        capability: RouteCapability = ANY_CAPABILITY,
        agent_id: str = "",
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not routes:
            raise LadderConfigError("a route ladder needs at least one route")
        self._policy = policy or LadderPolicy()
        self._routes: Tuple[LadderRoute, ...] = tuple(sorted(routes, key=_order_key))
        self._by_identity = {r.identity: r for r in self._routes}
        self._capability = capability
        self._agent_id = agent_id
        self._clock = clock
        self._wall_clock = wall_clock
        self._state: Dict[str, _RouteState] = {r.identity: _RouteState() for r in self._routes}
        self._pinned: Optional[LadderRoute] = None
        # Local incapability is not a health signal, so it is expressed as a
        # DISABLED provider rather than an open breaker: a route this host
        # cannot run must never look like a route the fleet found unhealthy.
        self._breaker = ProviderRouter(
            [
                Provider(
                    name=route.identity,
                    base_url=route.key.endpoint_class,
                    priority=position,
                    enabled=route.enabled and capability.permits(route),
                )
                for position, route in enumerate(self._routes)
            ],
            failure_threshold=self._policy.failure_threshold,
            cooldown_seconds=self._policy.transport_cooldown_seconds,
            half_open_max_probes=self._policy.half_open_max_probes,
            clock=clock,
        )

    # -- introspection -------------------------------------------------------

    @property
    def policy(self) -> LadderPolicy:
        return self._policy

    def effective_order(self) -> Tuple[LadderRoute, ...]:
        """Every configured route, in the order the owner's document implies."""
        return self._routes

    def route_for(self, identity: str) -> Optional[LadderRoute]:
        return self._by_identity.get(identity)

    def harness_order(self) -> Tuple[str, ...]:
        return ladder_harness_order(self._routes)

    # -- selection -----------------------------------------------------------

    def eligible(self) -> Tuple[LadderRoute, ...]:
        """Routes this worker could use right now, cheapest first."""
        status = self._breaker.status()
        out: List[LadderRoute] = []
        for route in self._routes:
            if not route.enabled:
                continue
            if not self._capability.permits(route):
                continue
            if status[route.identity]["state"] == BreakerState.OPEN.value:
                continue
            out.append(route)
        return tuple(out)

    def select(self) -> Optional[LadderRoute]:
        """The cheapest eligible route, or ``None`` when every one is suppressed.

        ``None`` is a real answer and the caller must fail closed on it — the
        alternative is hanging on a route the fleet already proved is capped.
        """
        # Delegate to the breaker so a HALF_OPEN probe is *consumed* here rather
        # than being handed out to every caller at once: bounded half-open
        # probing is what stops a refreshed monthly quota from being rediscovered
        # by a stampede of workers all paying for the same discovery.
        provider = self._breaker.select()
        if provider is None:
            return None
        return self._by_identity.get(provider.name)

    # -- turn boundaries -----------------------------------------------------

    def begin_turn(self) -> Optional[LadderRoute]:
        """Pin the route for the turn that is starting, and return it.

        Every switch happens here. An executor that is mid-turn keeps the route
        it started on even when a cheaper one becomes eligible, because swapping
        harness under a running turn throws the turn away — which is a larger
        loss than one turn spent on a more expensive route.
        """
        self._pinned = self.select()
        return self._pinned

    def end_turn(self) -> None:
        self._pinned = None

    def current(self) -> Optional[LadderRoute]:
        """The route pinned for this turn (``None`` outside a turn)."""
        return self._pinned

    def pending_switch(self) -> Optional[LadderRoute]:
        """A cheaper eligible route that the NEXT :meth:`begin_turn` will take.

        Returns ``None`` while the pinned route is still the cheapest eligible
        one, so a caller can log "will switch at the next task boundary" without
        having to diff the ladder itself.
        """
        if self._pinned is None:
            return None
        for route in self.eligible():
            if route.identity == self._pinned.identity:
                return None
            if _order_key(route) < _order_key(self._pinned):
                return route
        return None

    # -- outcome reporting ---------------------------------------------------

    def record_success(
        self,
        route: LadderRoute,
        *,
        agent_id: str = "",
        cost: Optional[RouteCost] = None,
    ) -> Dict[str, Any]:
        """Record a local success and return the record to publish.

        A success supersedes an older failure — including a fleet-wide account
        cap — because a refreshed monthly quota or a raised organisation limit
        is exactly the event nobody gets told about in advance.
        """
        identity = self._require_identity(route)
        now = self._clock()
        self._breaker.record_success(identity)
        state = self._state[identity]
        state.suppressed_reason = ""
        state.suppressed_by = ""
        state.suppressed_at = None
        state.cooldown_until = None
        state.last_success_at = now
        state.last_success_by = agent_id or self._agent_id
        if cost is not None:
            state.cost = cost
        return route_availability_record(
            self._by_identity[identity],
            outcome=OUTCOME_SUCCESS,
            agent_id=agent_id or self._agent_id,
            observed_at=self._wall_clock(),
        )

    def record_failure(
        self,
        route: LadderRoute,
        failure_class: str,
        *,
        agent_id: str = "",
        evidence: str = "",
    ) -> Dict[str, Any]:
        """Record a local failure and return the record to publish.

        A :data:`FAILURE_SEMANTIC` outcome is still published — the fleet wants
        to know the turn failed — but it does not touch the breaker. The route
        answered; the work was wrong.
        """
        if failure_class not in FAILURE_CLASSES:
            raise ValueError("unknown failure class %r" % failure_class)
        identity = self._require_identity(route)
        cooldown_until: Optional[float] = None
        if failure_class in ROUTE_AVAILABILITY_FAILURES:
            cooldown_until = self._suppress(
                identity,
                failure_class,
                by=agent_id or self._agent_id,
                evidence=evidence,
            )
        return route_availability_record(
            self._by_identity[identity],
            outcome=OUTCOME_FAILURE,
            failure_class=failure_class,
            agent_id=agent_id or self._agent_id,
            observed_at=self._wall_clock(),
            cooldown_until=cooldown_until,
            evidence=evidence,
        )

    def record_cost(self, route: LadderRoute, cost: RouteCost) -> None:
        """Attach an advisory Relay cost observation to a route.

        Advisory means advisory: this never reorders the ladder. It is surfaced
        in :meth:`telemetry` so the owner can see what a rung actually costs and
        re-rank the document if they disagree with their own ordering.
        """
        self._state[self._require_identity(route)].cost = cost

    # -- fleet convergence ---------------------------------------------------

    def apply_outcome(self, record: Mapping[str, Any]) -> bool:
        """Fold a peer's ``mac.route_availability.v1`` record into local state.

        Returns True when local state changed. Two rules make this safe:

        * A peer's failure can only ever *remove* an option, and only for the
          exact credential-backed route it observed. A cap on one account never
          suppresses the same harness on a different account.
        * A peer's success clears suppression but does not select anything: this
          worker still has to be locally capable of the route. AgentBus
          accelerates convergence; it is not the authority, and it cannot hand a
          worker a credential it does not hold.
        """
        parsed = parse_route_availability(record)
        key: RouteKey = parsed["key"]
        agent_id = str(parsed["agent_id"] or "")
        if agent_id and self._agent_id and agent_id == self._agent_id:
            return False  # our own echo

        targets = [
            route
            for route in self._routes
            if route.key.credential_identity == key.credential_identity
            and (not key.model or not route.key.model or route.key.model == key.model)
        ]
        if not targets:
            return False

        changed = False
        if parsed["outcome"] == OUTCOME_SUCCESS:
            for route in targets:
                if self._breaker.status()[route.identity]["state"] != BreakerState.CLOSED.value:
                    changed = True
                self._breaker.record_success(route.identity)
                state = self._state[route.identity]
                if state.suppressed_reason:
                    changed = True
                state.suppressed_reason = ""
                state.suppressed_by = ""
                state.suppressed_at = None
                state.cooldown_until = None
                state.last_success_at = self._clock()
                state.last_success_by = agent_id
            return changed

        failure_class = str(parsed["failure_class"] or "")
        if failure_class not in FLEET_SUPPRESSING_FAILURES:
            # A peer's failed tests say nothing about our routes, and neither
            # does its transport break or its throttle window.
            return False
        for route in targets:
            already = self._state[route.identity].suppressed_reason
            self._suppress(
                route.identity,
                failure_class,
                by=agent_id,
                evidence=str(parsed["evidence"] or ""),
            )
            changed = changed or not already
        return changed

    # -- telemetry -----------------------------------------------------------

    def telemetry(self) -> Dict[str, Any]:
        """Secret-free ``mac.route_ladder.telemetry.v1`` for the CLI/UI.

        Answers, for every rung: what rank the owner gave it, whether it is the
        effective route, why it is suppressed and for how long, what Relay
        measured it to cost (or that nobody measured), and when it last actually
        worked.
        """
        status = self._breaker.status()
        rows: List[Dict[str, Any]] = []
        for route in self._routes:
            state = self._state[route.identity]
            breaker = status[route.identity]
            rows.append(
                {
                    "route": route.key.observable(),
                    "rank": route.rank,
                    "enabled": route.enabled,
                    "locally_capable": self._capability.permits(route),
                    "breaker_state": breaker["state"],
                    "suppressed": breaker["state"] == BreakerState.OPEN.value,
                    "suppression_reason": state.suppressed_reason or None,
                    "suppression_reported_by": state.suppressed_by or None,
                    "seconds_until_probe": breaker["seconds_until_probe"],
                    "cost": state.cost.observable(),
                    "last_success_at": state.last_success_at,
                    "last_success_by": state.last_success_by or None,
                    "consecutive_failures": breaker["consecutive_failures"],
                }
            )
        pending = self.pending_switch()
        return {
            "schema": TELEMETRY_SCHEMA,
            "agent_id": self._agent_id or None,
            "policy": self._policy.observable(),
            "effective_route": (self._pinned.key.observable() if self._pinned else None),
            "effective_rank": (self._pinned.rank if self._pinned else None),
            "pending_switch": (pending.key.observable() if pending else None),
            "pending_switch_rank": (pending.rank if pending else None),
            "routes": rows,
        }

    # -- internals -----------------------------------------------------------

    def _require_identity(self, route: LadderRoute) -> str:
        identity = route.identity
        if identity not in self._by_identity:
            raise KeyError("route %s is not on this ladder" % identity)
        return identity

    def _suppress(
        self,
        identity: str,
        failure_class: str,
        *,
        by: str,
        evidence: str,
    ) -> float:
        cooldown, immediate = cooldown_for_failure(failure_class, self._policy)
        self._breaker.record_failure(identity, cooldown_seconds=cooldown, immediate=immediate)
        state = self._state[identity]
        state.suppressed_reason = failure_class
        state.suppressed_by = by
        state.suppressed_at = self._clock()
        state.cooldown_until = self._clock() + cooldown
        if evidence:
            state.suppressed_reason = "%s: %s" % (failure_class, _bounded_evidence(evidence))
        return self._wall_clock() + cooldown
