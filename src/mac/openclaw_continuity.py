"""Selective, provenance-rich OpenClaw continuity recall.

The ``GET /v1/agents/{agent_id}/continuity`` endpoint is the narrow runtime
bridge an OpenClaw agent uses to remember "what was I doing / who was I talking
to".  Historically it had two blind spots that made a real prior conversation
irrecoverable:

* It merged medium- and long-tier Qdrant recall with ``min_score`` unset, so a
  handful of near-zero task-import confirmations (scores ~0.037-0.054) were
  injected purely to fill the requested ``limit`` — burying, or displacing, a
  genuinely relevant memory.
* It never looked at the AgentBus at all, so a prior peer conversation living
  in an authenticated bus stream simply could not be recovered no matter how
  the query was phrased.

This module implements the fix as a self-contained, testable unit:

* :func:`recall_continuity` fuses vector-memory candidates with bounded,
  agent-scoped AgentBus history, applies a calibrated minimum relevance
  threshold, and returns a strict, token-budgeted, source-labelled result set.
* Every returned item carries ``source`` (``"memory"`` or ``"bus"``) and a
  numeric ``score`` so OpenClaw can label memory vs. bus history and
  observability can measure a useful-hit rate.

Security invariants enforced here (acceptance criterion 4):

* Only streams the requesting agent is authorized for are scanned (the
  bound-agent authorization rule).
* Control streams, LLM-generated Slack/fleet mirror payloads, and
  secret-bearing payloads/topics are excluded — the source record is the real
  peer message, never its mirror, and raw sensitive tool output is never copied
  into the recalled text.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from mac.agentbus_control import (
    PEER_MESSAGE_SCHEMA,
    PEER_REPLY_SCHEMA,
    is_control_stream,
)

JsonDict = Dict[str, Any]

# ---------------------------------------------------------------------------
# Calibrated defaults (acceptance criteria 1 and 3).
#
# The 0.037-0.054 task-import band observed in the field is filler: it must not
# be injected merely to reach ``limit``.  The floor sits comfortably above that
# band while staying below the ~0.6+ scores a genuine conversational match
# earns.  Every knob is overridable from the environment so operators can
# recalibrate without a code change.
# ---------------------------------------------------------------------------

DEFAULT_MIN_SCORE = 0.15
DEFAULT_MAX_ITEMS = 8
DEFAULT_TOKEN_BUDGET = 1200
DEFAULT_BUS_STREAM_SCAN = 40
DEFAULT_BUS_CHUNK_SCAN = 20
# A strong conversational match should not be crowded out by a pile of
# marginal task-import confirmations.  We cap how many memory items of the same
# low-value shape may occupy the final set.
DEFAULT_MAX_LOW_VALUE_MEMORIES = 2

# Payload schemas that are NOT the source record.  A fleet/Slack mirror is an
# LLM-generated restatement of a real bus exchange; recalling it would surface a
# lossy paraphrase instead of the authenticated original.
MIRROR_SCHEMAS = frozenset(
    {
        "mac.fleet_conversation_mirror.v1",
    }
)

# Topics whose very name signals credential/secret material.  Streams on these
# topics are skipped wholesale.
_SECRET_TOPIC_MARKERS = (
    "secret",
    "credential",
    "token",
    "password",
    "apikey",
    "api_key",
    "private_key",
)

# Payload keys that look like credentials; any chunk carrying one is dropped so
# raw sensitive material never enters recalled text.
_SECRET_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "bearer",
    "private_key",
)

# High-signal value patterns (bearer tokens, PEM blocks, long hex/base64 keys)
# used to reject payloads whose values look secret even under an innocuous key.
_SECRET_VALUE_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----)"
    r"|(?:\b(?:sk|xox[abpr]|ghp|gho|github_pat|AKIA)[-_A-Za-z0-9]{8,})"
    r"|(?:\bBearer\s+[A-Za-z0-9._\-]{12,})",
)

# Task-import confirmations are the canonical filler: short, templated summaries
# announcing that a task was imported/queued.  Recognising them lets us keep a
# strong conversational match from being crowded out (criterion 3) without
# discarding legitimately relevant memories.
_LOW_VALUE_MEMORY_RE = re.compile(
    r"\b(task[- ]?import(?:ed|s)?|imported task|queued task|task queued|"
    r"import confirmation|nap (?:summary|consolidation))\b",
    re.IGNORECASE,
)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


@dataclass
class ContinuityConfig:
    """Tunable, calibrated budgets for selective continuity recall."""

    min_score: float = DEFAULT_MIN_SCORE
    max_items: int = DEFAULT_MAX_ITEMS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    bus_stream_scan: int = DEFAULT_BUS_STREAM_SCAN
    bus_chunk_scan: int = DEFAULT_BUS_CHUNK_SCAN
    max_low_value_memories: int = DEFAULT_MAX_LOW_VALUE_MEMORIES

    @classmethod
    def from_env(cls) -> "ContinuityConfig":
        return cls(
            min_score=_env_float("MAC_CONTINUITY_MIN_SCORE", DEFAULT_MIN_SCORE),
            max_items=_env_int("MAC_CONTINUITY_MAX_ITEMS", DEFAULT_MAX_ITEMS),
            token_budget=_env_int(
                "MAC_CONTINUITY_TOKEN_BUDGET", DEFAULT_TOKEN_BUDGET
            ),
            bus_stream_scan=_env_int(
                "MAC_CONTINUITY_BUS_STREAM_SCAN", DEFAULT_BUS_STREAM_SCAN
            ),
            bus_chunk_scan=_env_int(
                "MAC_CONTINUITY_BUS_CHUNK_SCAN", DEFAULT_BUS_CHUNK_SCAN
            ),
            max_low_value_memories=_env_int(
                "MAC_CONTINUITY_MAX_LOW_VALUE_MEMORIES",
                DEFAULT_MAX_LOW_VALUE_MEMORIES,
            ),
        )


@dataclass
class ContinuityMetrics:
    """Counters describing a single recall, logged without query contents."""

    candidates_considered: int = 0
    threshold_drops: int = 0
    secret_drops: int = 0
    mirror_drops: int = 0
    source_memory: int = 0
    source_bus: int = 0
    selected: int = 0
    low_value_capped: int = 0

    def to_dict(self) -> JsonDict:
        return {
            "candidates_considered": self.candidates_considered,
            "threshold_drops": self.threshold_drops,
            "secret_drops": self.secret_drops,
            "mirror_drops": self.mirror_drops,
            "source_memory": self.source_memory,
            "source_bus": self.source_bus,
            "selected": self.selected,
            "low_value_capped": self.low_value_capped,
        }


@dataclass
class _Candidate:
    source: str  # "memory" | "bus"
    score: float
    text: str
    item: JsonDict
    low_value: bool = False


def _estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~4 chars/token, min 1)."""

    return max(1, len(str(text or "")) // 4)


def _looks_secret(value: Any) -> bool:
    if isinstance(value, str):
        return bool(_SECRET_VALUE_RE.search(value))
    if isinstance(value, dict):
        for key, sub in value.items():
            key_text = str(key).lower()
            if any(marker in key_text for marker in _SECRET_KEY_MARKERS):
                return True
            if _looks_secret(sub):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_looks_secret(item) for item in value)
    return False


def _topic_is_secret(topic: str) -> bool:
    topic_text = str(topic or "").lower()
    return any(marker in topic_text for marker in _SECRET_TOPIC_MARKERS)


def _declared_schema(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("schema") or "")
    return ""


def _conversational_text(payload: Any) -> str:
    """Extract the human-readable body from a peer message/reply payload."""

    if isinstance(payload, dict):
        for key in ("message", "reply", "text", "body", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(payload, str):
        return payload.strip()
    return ""


def _keyword_overlap_score(query: str, text: str) -> float:
    """Lightweight lexical relevance in ``[0, 1]`` for bus text.

    Bus chunks are not embedded, so we cannot get a vector score for them.  A
    bounded keyword-overlap ratio gives a comparable, monotonic signal: the
    fraction of distinct query terms that appear in the candidate text.  It is
    deliberately conservative so a real conversational match (which shares the
    query's salient nouns) outranks the filler band, while unrelated chatter
    scores low enough to be dropped by the threshold.
    """

    query_terms = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2}
    if not query_terms:
        return 0.0
    text_terms = set(re.findall(r"[a-z0-9]+", str(text or "").lower()))
    if not text_terms:
        return 0.0
    hits = sum(1 for term in query_terms if term in text_terms)
    return hits / float(len(query_terms))


def collect_bus_candidates(
    *,
    agent_id: str,
    query: str,
    agentbus: Any,
    config: ContinuityConfig,
    metrics: ContinuityMetrics,
) -> List[_Candidate]:
    """Return authorized, non-secret, non-mirror bus conversation candidates.

    Only streams the agent may read are scanned (``list_streams`` already scopes
    to sender/recipient/participant), the scan is bounded by ``bus_stream_scan``
    and ``bus_chunk_scan``, and each chunk is authorization-checked again via
    ``read_chunks`` so the bound-agent rule is enforced end to end.
    """

    candidates: List[_Candidate] = []
    try:
        streams = agentbus.list_streams(
            agent_id=agent_id, limit=config.bus_stream_scan
        )
    except Exception:
        return candidates

    for stream in streams:
        topic = getattr(stream, "topic", "") or ""
        content_type = getattr(stream, "content_type", "") or ""
        if is_control_stream(topic, content_type):
            continue
        if _topic_is_secret(topic):
            metrics.secret_drops += 1
            continue
        try:
            chunks = agentbus.read_chunks(
                agent_id,
                stream.id,
                after_sequence=0,
                limit=config.bus_chunk_scan,
                # This is a bounded historical search, not consumption of new
                # messages.  It intentionally starts at sequence zero and must
                # not advance the delivery cursor or emit one durable read event
                # per stream on every prompt.
                record_observation=False,
            )
        except Exception:
            # Not authorized / vanished stream: skip, never leak.
            continue

        for chunk in chunks:
            payload = getattr(chunk, "payload", None)
            schema = _declared_schema(payload)
            if schema in MIRROR_SCHEMAS:
                metrics.mirror_drops += 1
                continue
            # Only true conversational payloads are eligible; keep the source
            # record narrow to peer message/reply (plus untyped text chunks).
            if schema and schema not in (PEER_MESSAGE_SCHEMA, PEER_REPLY_SCHEMA):
                continue
            if _looks_secret(payload):
                metrics.secret_drops += 1
                continue
            text = _conversational_text(payload)
            if not text:
                continue
            metrics.candidates_considered += 1
            score = _keyword_overlap_score(query, text)
            headers = getattr(stream, "headers", {}) or {}
            correlation = ""
            if isinstance(payload, dict):
                correlation = str(payload.get("correlation_id") or "")
            if not correlation:
                correlation = str(headers.get("correlation_id") or "")
            item: JsonDict = {
                "source": "bus",
                "score": round(float(score), 6),
                "text": text,
                "stream_id": stream.id,
                "chunk_id": getattr(chunk, "id", None),
                "topic": topic,
                "sender_agent_id": getattr(chunk, "sender_agent_id", None),
                "recipient_agent_id": getattr(stream, "recipient_agent_id", None),
                "correlation_id": correlation or None,
                "timestamp": getattr(chunk, "created_at", None),
                "sequence": getattr(chunk, "sequence", None),
                "schema": schema or None,
            }
            candidates.append(
                _Candidate(source="bus", score=float(score), text=text, item=item)
            )

    return candidates


def collect_memory_candidates(
    *,
    query: str,
    tiers: Sequence[str],
    limit: int,
    agent_id: str,
    recall: Callable[..., List[JsonDict]],
    config: ContinuityConfig,
    metrics: ContinuityMetrics,
) -> List[_Candidate]:
    """Recall vector memories across the requested tiers with a score floor.

    The ``min_score`` floor is pushed down into recall so the vector store never
    returns filler in the first place; the same floor is re-checked here in case
    a backend ignores it.
    """

    candidates: List[_Candidate] = []
    seen_ids: set = set()
    for tier in tiers:
        try:
            hits = recall(
                query,
                tier=tier,
                limit=limit,
                min_score=config.min_score,
                agent_id=agent_id,
            )
        except TypeError:
            # Backwards-compatible with recall() variants lacking min_score.
            hits = recall(query, tier=tier, limit=limit, agent_id=agent_id)
        for hit in hits or []:
            score = float(hit.get("score") or 0.0)
            text = str(
                hit.get("summary")
                or hit.get("content")
                or hit.get("text")
                or ""
            )
            # Dedup across tiers: identical records surfacing in both medium and
            # long tiers must not double-count toward budgets or the source mix.
            dedup_key = hit.get("memory_id") or hit.get("id") or ("text", text)
            if dedup_key in seen_ids:
                continue
            seen_ids.add(dedup_key)
            metrics.candidates_considered += 1
            item: JsonDict = {**hit, "source": "memory", "tier": tier}
            item.setdefault("score", round(score, 6))
            candidates.append(
                _Candidate(
                    source="memory",
                    score=score,
                    text=text,
                    item=item,
                    low_value=bool(_LOW_VALUE_MEMORY_RE.search(text)),
                )
            )
    return candidates


def _fuse(
    candidates: List[_Candidate],
    *,
    config: ContinuityConfig,
    limit: int,
    metrics: ContinuityMetrics,
) -> List[JsonDict]:
    """Rank and select candidates under threshold, token, and count budgets."""

    eligible: List[_Candidate] = []
    for cand in candidates:
        if cand.score < config.min_score:
            metrics.threshold_drops += 1
            continue
        eligible.append(cand)

    # Highest score first; on ties prefer bus provenance (a real exchange beats
    # a summarizing memory) then longer, more specific text.
    eligible.sort(
        key=lambda c: (c.score, c.source == "bus", len(c.text)),
        reverse=True,
    )

    max_items = min(limit, config.max_items)
    selected: List[JsonDict] = []
    tokens_used = 0
    low_value_used = 0
    for cand in eligible:
        if len(selected) >= max_items:
            break
        if cand.low_value and low_value_used >= config.max_low_value_memories:
            metrics.low_value_capped += 1
            continue
        cost = _estimate_tokens(cand.text)
        if selected and tokens_used + cost > config.token_budget:
            continue
        tokens_used += cost
        if cand.low_value:
            low_value_used += 1
        if cand.source == "bus":
            metrics.source_bus += 1
        else:
            metrics.source_memory += 1
        selected.append(cand.item)

    metrics.selected = len(selected)
    return selected


def recall_continuity(
    *,
    agent_id: str,
    query: str,
    limit: int,
    recall: Callable[..., List[JsonDict]],
    agentbus: Any = None,
    tiers: Sequence[str] = ("medium", "long"),
    config: Optional[ContinuityConfig] = None,
) -> Tuple[List[JsonDict], ContinuityMetrics]:
    """Return fused, provenance-rich continuity items plus recall metrics.

    ``recall`` is the vector-memory callable (``cp.recall_memory``); ``agentbus``
    is the ``AgentBusService`` (or ``None`` to skip bus recall). Bus recall
    failures degrade gracefully: memory recall still returns.
    """

    config = config or ContinuityConfig.from_env()
    metrics = ContinuityMetrics()

    query = str(query or "").strip()
    if not query or limit <= 0:
        return [], metrics

    candidates: List[_Candidate] = collect_memory_candidates(
        query=query,
        tiers=tiers,
        limit=limit,
        agent_id=agent_id,
        recall=recall,
        config=config,
        metrics=metrics,
    )

    if agentbus is not None:
        candidates.extend(
            collect_bus_candidates(
                agent_id=agent_id,
                query=query,
                agentbus=agentbus,
                config=config,
                metrics=metrics,
            )
        )

    selected = _fuse(candidates, config=config, limit=limit, metrics=metrics)
    return selected, metrics
