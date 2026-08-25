"""Dynamic selection of the fleet's powerhouse models.

The fleet's strong model was hard-coded (``_DEFAULT_STRONG_WILDCARD_MODEL`` /
``MAC_ROUTER_DEFAULT_MODEL``) and left pinned forever. This module replaces that
with a periodically-refreshed choice of the current top-N "powerhouse" models,
computed in three stages:

1. **DISCOVER** what is currently leading via a web search (injectable
   ``searcher``; wired in production to ``firecrawl_gateway.search_web``). We
   read result titles/snippets and count mentions of known model *families* —
   recognizing names like "Claude Opus 4.x" or "GPT-5.x", NOT hard-coding which
   one wins. Ranked by how often the current web says they lead.
2. **MODERATE** by what the gateway / coding CLIs can actually route to
   (injectable ``available``; wired to ``models.dev`` ``list_agentic_models``
   for the fleet's configured providers). A model nothing can serve is never
   selected — this is the crucial constraint that keeps the choice real.
3. **SELECT** the top N (default 3) of leading ∩ available, in leading-rank
   order, mapping each leading family to the best concrete available model id.
   If discovery yields nothing available, fall back deterministically to the
   configured strong default so the fleet never ends up with no model.

The result is persisted (a small JSON file the router reads) and refreshed on a
slow schedule, so the choice tracks reality instead of a weeks-stale constant.
The recognition registry (which *names* to look for) is data, not the choice;
adding a new family here just lets discovery see it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mac import mac_paths
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from mac import models_catalog

# Recognition registry: canonical family -> regex matching how it's written on
# the web AND (loosely) how its concrete model ids look. This only lets the
# discovery step *see* a family; it does not decide the winner. Ordered newest-
# first within a vendor so a bare "claude opus" prefers the latest generation.
MODEL_FAMILIES: Tuple[Tuple[str, str], ...] = (
    ("claude-opus", r"claude[\s-]*opus[\s-]*\d"),
    ("claude-sonnet", r"claude[\s-]*sonnet[\s-]*\d"),
    ("gpt-5", r"\bgpt[\s-]*5(\.\d+)?\b"),
    ("o-series", r"\bo[3-9](-(pro|mini))?\b"),
    ("gemini-pro", r"gemini[\s-]*\d(\.\d+)?[\s-]*pro"),
    ("grok", r"\bgrok[\s-]*\d"),
    ("deepseek", r"deepseek[\s-]*(v?\d|r\d)"),
    ("llama", r"\bllama[\s-]*\d"),
    ("qwen", r"\bqwen[\s-]*\d"),
)

_FAMILY_RES = tuple((name, re.compile(pat, re.IGNORECASE)) for name, pat in MODEL_FAMILIES)


@dataclass(frozen=True)
class ModelSelection:
    """The chosen powerhouse models plus provenance for auditability.

    ``ladder`` is the full set of available models ordered weakest→strongest,
    which backs the 1..10 strength scale (strength 10 = the strongest available,
    strength 1 = the cheapest/weakest), so a user/task can pick capability
    without naming a model that will change next month.
    """

    models: List[str]
    leading_families: List[str] = field(default_factory=list)
    available_count: int = 0
    source: str = ""  # "dynamic" | "fallback"
    at: str = ""
    ladder: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema": "mac.model_selection.v1",
            "models": list(self.models),
            "leading_families": list(self.leading_families),
            "available_count": self.available_count,
            "source": self.source,
            "at": self.at,
            "ladder": list(self.ladder),
        }


# Name-based capability tiers, used to order the strength ladder when per-model
# cost data is unavailable. Lower tier = weaker/cheaper. This is a coarse proxy;
# real cost from models.dev (when present) takes precedence.
_WEAK_PAT = re.compile(r"(nano|mini|micro|lite|flash|haiku|small|8b|1b|3b|instant)", re.IGNORECASE)
_STRONG_PAT = re.compile(r"(opus|ultra|max|pro|405b|70b|-r\d|reason)", re.IGNORECASE)


def _name_tier(model_id: str) -> int:
    if _STRONG_PAT.search(model_id):
        return 2
    if _WEAK_PAT.search(model_id):
        return 0
    return 1


def model_strength_ladder(
    available_models: Iterable[str],
    *,
    cost_lookup: Optional[Callable[[str], Optional[float]]] = None,
) -> List[str]:
    """Order available models weakest→strongest for the 1..10 strength scale.

    Primary key is real per-token output cost from ``cost_lookup`` (models.dev in
    production) — "10 = most powerful and probably most expensive" per the spec,
    so cost is a defensible strength proxy. Falls back to a name-based tier when
    cost is missing, and to the model id as a final deterministic tiebreak.
    """
    models = [str(m) for m in (available_models or []) if str(m).strip()]

    def key(mid: str):
        cost = None
        if cost_lookup is not None:
            try:
                cost = cost_lookup(mid)
            except Exception:  # noqa: BLE001
                cost = None
        # name_tier is the PRIMARY signal so an unknown-cost strong model (e.g.
        # an Opus with no catalog price) is not sorted to strength 1 — the
        # previous (has_cost, cost, ...) key clustered every unpriced model at
        # the weak end regardless of capability. Cost refines ordering WITHIN a
        # tier. Ascending = weakest first.
        return (_name_tier(mid), cost if cost is not None else 0.0, mid)

    return sorted(models, key=key)


def resolve_strength(scale: int, ladder: Sequence[str]) -> str:
    """Map an integer strength ``scale`` (1=weakest/cheapest .. 10=strongest) to
    a concrete model id from ``ladder`` (weakest→strongest). Clamps out-of-range
    input. Empty ladder → "" (caller falls back to the fleet default)."""
    rung = list(ladder or [])
    if not rung:
        return ""
    try:
        s = int(scale)
    except (TypeError, ValueError):
        return ""
    s = max(1, min(10, s))
    idx = round((s - 1) / 9.0 * (len(rung) - 1))
    return rung[idx]


def _models_dev_cost(model_id: str) -> Optional[float]:
    """Output cost per token for a model id via models.dev, or None.

    The real catalog API is ``get_model_info(provider_id, model_id)`` and the
    field is ``cost_output`` (``get_model_capabilities(model_id)`` — the previous
    call — is the wrong API and carries no cost). Ids are normally the router's
    native bare form (``gpt-5-2``), but may carry provider path segments
    (``nvidia/meta/llama``); we try every provider-boundary split plus a bare
    fallback across common providers. Returns None (=> name-tier ladder
    fallback) when unavailable.
    """
    mid = str(model_id)
    # Candidate (provider, model) splits: the leading segment as provider with
    # the rest as the model, plus a bare fallback across common providers.
    candidates: List[Tuple[str, str]] = []
    segments = [s for s in mid.split("/") if s]
    # Try every provider-boundary split, keeping the FULL remainder as the model
    # id (so nvidia/meta/llama -> (nvidia, "meta/llama") and (meta, "llama"),
    # not the previous (nvidia, "llama") which dropped the intermediate path).
    for i in range(len(segments) - 1):
        candidates.append((segments[i], "/".join(segments[i + 1 :])))
        candidates.append((segments[i], segments[-1]))
    bare = segments[-1] if segments else mid
    for provider in ("anthropic", "openai", "google", "xai", "deepseek", "meta", "mistral", "qwen"):
        candidates.append((provider, bare))
    # De-dup, preserve order.
    seen_c: set = set()
    candidates = [c for c in candidates if not (c in seen_c or seen_c.add(c))]
    for provider, model in candidates:
        try:
            info = models_catalog.get_model_info(provider, model)
        except Exception:  # noqa: BLE001
            info = None
        if info is not None:
            val = getattr(info, "cost_output", None)
            if isinstance(val, (int, float)):
                return float(val)
    return None


def extract_leading_families(
    search_results: Iterable[dict],
) -> List[str]:
    """Rank model families by how often the web results mention them.

    Deterministic given the results: counts family matches across titles +
    descriptions, breaks ties by the family's registry order (newest vendors
    first). Returns families most-mentioned first.
    """
    scores: Dict[str, int] = {}
    order = {name: i for i, (name, _) in enumerate(_FAMILY_RES)}
    for result in search_results or []:
        text = " ".join(str(result.get(k) or "") for k in ("title", "description", "snippet"))
        for name, rx in _FAMILY_RES:
            if rx.search(text):
                scores[name] = scores.get(name, 0) + 1
    return sorted(scores, key=lambda n: (-scores[n], order[n]))


def _family_of(model_id: str) -> Optional[str]:
    norm = str(model_id or "")
    for name, rx in _FAMILY_RES:
        if rx.search(norm):
            return name
    return None


def moderate_by_availability(
    leading_families: Sequence[str],
    available_models: Iterable[str],
) -> List[str]:
    """Map each leading family (in rank order) to the best available concrete
    model id for it. Families with no available model are dropped — that is the
    'moderate by what the gateway/CLI actually has' constraint.

    'Best' within a family = the newest available id, by a NATURAL version sort
    (numeric segments compared as integers) so ``claude-opus-4-10`` beats
    ``claude-opus-4-9`` — a lexical sort would wrongly prefer 4-9.
    """
    by_family: Dict[str, List[str]] = {}
    for mid in available_models or []:
        fam = _family_of(mid)
        if fam is not None:
            by_family.setdefault(fam, []).append(str(mid))
    chosen: List[str] = []
    for family in leading_families:
        candidates = by_family.get(family)
        if candidates:
            chosen.append(max(candidates, key=_version_key))
    return chosen


def _version_key(model_id: str):
    """Natural-order key: split into numeric and non-numeric runs so numeric
    segments compare as integers (4-10 > 4-9), not lexically."""
    parts = re.findall(r"\d+|\D+", str(model_id))
    return [(1, int(p)) if p.isdigit() else (0, p) for p in parts]


def select_powerhouse_models(
    search_results: Iterable[dict],
    available_models: Iterable[str],
    *,
    n: int = 3,
    fallback: str = "",
    now: str = "",
    cost_lookup: Optional[Callable[[str], Optional[float]]] = None,
) -> ModelSelection:
    """End-to-end: discover leading → moderate by availability → top N, plus the
    full strength ladder (for the 1..10 scale).

    Falls back to ``fallback`` (the configured strong default) when discovery
    finds nothing that is also available, so the fleet is never left model-less.
    """
    available_list = [str(m) for m in (available_models or []) if str(m).strip()]
    ladder = model_strength_ladder(available_list, cost_lookup=cost_lookup)
    leading = extract_leading_families(search_results)
    chosen = moderate_by_availability(leading, available_list)
    # De-dup while preserving rank order.
    seen: set = set()
    ordered = [m for m in chosen if not (m in seen or seen.add(m))]
    if ordered:
        return ModelSelection(
            models=ordered[:n],
            leading_families=leading[:n],
            available_count=len(available_list),
            source="dynamic",
            at=now,
            ladder=ladder,
        )
    fb = [fallback] if fallback else []
    return ModelSelection(
        models=fb,
        leading_families=leading[:n],
        available_count=len(available_list),
        source="fallback",
        at=now,
        ladder=ladder,
    )


# --------------------------------------------------------------------------- #
# Production adapters (thin; kept import-light so the engine stays testable).
# --------------------------------------------------------------------------- #

DEFAULT_QUERIES: Tuple[str, ...] = (
    "best large language models for coding this year",
    "top LLM coding leaderboard current",
    "most capable AI models ranking",
)


def discover_via_web(
    searcher: Callable[[str, int], List[dict]],
    *,
    queries: Sequence[str] = DEFAULT_QUERIES,
    limit: int = 10,
) -> List[dict]:
    """Aggregate web-search results across the discovery queries. ``searcher``
    is ``firecrawl_gateway.search_web`` in production; injectable for tests."""
    results: List[dict] = []
    for query in queries:
        try:
            results.extend(searcher(query, limit) or [])
        except Exception:  # noqa: BLE001 - one query failing must not abort discovery.
            continue
    return results


def available_models_from_providers(providers: Iterable[Any]) -> List[str]:
    """The exact model ids the router can serve AND the upstream will accept.

    ``providers`` may be ``provider_router.Provider`` objects (allowlist-aware,
    the production path) or bare provider-name strings (legacy — treated as
    wildcard). Ids are returned in the router's NATIVE namespace, NOT
    provider-prefixed:

    * A provider with an explicit ``models=`` allowlist contributes those ids
      verbatim — they are exactly what ``ProviderRouter._serves`` matches and
      what the upstream API expects.
    * A wildcard (``models=*``) provider serves any id, so its offerings are
      enumerated from the models.dev catalog as the catalog's own bare ids.

    The previous ``provider/model`` prefixing was wrong on both counts: an
    explicit allowlist rejected the prefixed id at ``_serves``, and a wildcard
    provider forwarded ``openai/gpt-5-2`` to the upstream, which only knows
    ``gpt-5-2``. Empty on any failure — the caller falls back.
    """
    out: List[str] = []
    wildcard_providers: List[str] = []
    for entry in providers or []:
        name = getattr(entry, "name", None)
        models = getattr(entry, "models", None)
        if name is None:
            # Legacy bare-name string: no allowlist known -> enumerate via catalog.
            wildcard_providers.append(str(entry))
            continue
        if models and "*" not in tuple(models):
            # Explicit allowlist: these ARE the routable+native ids.
            out.extend(str(m) for m in models if str(m).strip())
        else:
            wildcard_providers.append(str(name))

    if wildcard_providers:
        for p in wildcard_providers:
            try:
                for mid in models_catalog.list_agentic_models(p):
                    out.append(str(mid))  # native bare id, NOT prefixed
            except Exception:  # noqa: BLE001
                continue

    seen: set = set()
    return [m for m in out if m.strip() and not (m in seen or seen.add(m))]


# --------------------------------------------------------------------------- #
# Persistence + periodic refresher
# --------------------------------------------------------------------------- #

_log = logging.getLogger("mac.model_selection")

# Serializes the read-modify-write store mutations below. The atomic rename in
# _write_store prevents torn files, but set_pending / promote_pending / set_active
# each read-then-write; without this lock two hub threads (refresh + operator
# promote) can interleave and one silently drops the other's update (lost update).
_STORE_LOCK = threading.RLock()

DEFAULT_INTERVAL_SECONDS = 7 * 24 * 60 * 60.0  # weekly — "what's leading" moves slowly
DEFAULT_INITIAL_DELAY_SECONDS = 300.0
MIN_INTERVAL_SECONDS = 60.0


def selection_file_path(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    configured = str(env.get("MAC_MODEL_SELECTION_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    home = str(env.get("MAC_HOME") or "").strip()
    base = Path(home).expanduser() if home else mac_paths.mac_home()
    return base / "model-selection.json"


def _read_store(path: Optional[Path], environ: Optional[Mapping[str, str]]) -> dict:
    """Read the selection store: ``{"active": <sel|null>, "pending": <sel|null>}``.

    Tolerates the flat legacy shape (a bare selection dict) by treating it as the
    active selection, so an older on-disk file still resolves.
    """
    p = path if path is not None else selection_file_path(environ)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"active": None, "pending": None}
    if not isinstance(data, dict):
        return {"active": None, "pending": None}
    if "active" in data or "pending" in data:
        return {"active": data.get("active"), "pending": data.get("pending")}
    # Legacy flat selection dict -> active.
    if isinstance(data.get("models"), list):
        return {"active": data, "pending": None}
    return {"active": None, "pending": None}


def _write_store(store: dict, path: Optional[Path], environ: Optional[Mapping[str, str]]) -> Path:
    import os as _os
    import tempfile as _tempfile

    p = path if path is not None else selection_file_path(environ)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp in the same dir (not a fixed ``.tmp``) so a concurrent refresh
    # and promote can't clobber each other's temp mid-write; the rename is atomic.
    fd, tmp_name = _tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(store, indent=2, sort_keys=True) + "\n")
        _os.replace(tmp_name, str(p))  # atomic
    except BaseException:
        try:
            _os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return p


def read_active(environ: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """The ACTIVE selection the router/tasks consume. None if none adopted yet."""
    active = _read_store(None, environ).get("active")
    if isinstance(active, dict) and isinstance(active.get("models"), list) and active["models"]:
        return active
    return None


def read_pending(environ: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """A proposed swap awaiting the eval-drift gate / operator promotion, or None."""
    pending = _read_store(None, environ).get("pending")
    if isinstance(pending, dict) and isinstance(pending.get("models"), list) and pending["models"]:
        return pending
    return None


# Back-compat alias: the router reads the ACTIVE selection.
read_selection = read_active


def selected_models(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """The ACTIVE powerhouse models, or [] if none adopted yet. A pending swap
    does NOT affect this — routing never changes until a swap is promoted."""
    data = read_active(environ=environ)
    return [str(m) for m in (data or {}).get("models", []) if str(m).strip()]


def resolve_strength_from_selection(scale: int, environ: Optional[Mapping[str, str]] = None) -> str:
    """Resolve a 1..10 strength to a concrete model via the ACTIVE ladder.

    Returns "" when no ladder is adopted yet (caller falls back to the fleet
    default). Fast, network-free per-task path: the service computes the ladder
    once; each task just indexes the active one."""
    data = read_active(environ=environ)
    ladder = (data or {}).get("ladder") or []
    return resolve_strength(scale, [str(m) for m in ladder if str(m).strip()])


def set_active(sel: ModelSelection, environ: Optional[Mapping[str, str]] = None) -> Path:
    """Adopt ``sel`` as active and clear any pending candidate."""
    with _STORE_LOCK:
        return _write_store({"active": sel.to_dict(), "pending": None}, None, environ)


def set_pending(sel: ModelSelection, environ: Optional[Mapping[str, str]] = None) -> Path:
    """Record ``sel`` as a pending swap (does not affect routing)."""
    with _STORE_LOCK:
        store = _read_store(None, environ)
        store["pending"] = sel.to_dict()
        return _write_store(store, None, environ)


def promote_pending(environ: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """Promote the pending candidate to active (operator/gate action). Returns
    the promoted selection dict, or None if there was nothing pending."""
    with _STORE_LOCK:
        store = _read_store(None, environ)
        pending = store.get("pending")
        if not (isinstance(pending, dict) and pending.get("models")):
            return None
        _write_store({"active": pending, "pending": None}, None, environ)
        return pending


# Legacy name retained for callers/tests that adopt directly (bootstrap path).
def write_selection(
    sel: ModelSelection, path: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None
) -> Path:
    return _write_store({"active": sel.to_dict(), "pending": None}, path, environ)


# --------------------------------------------------------------------------- #
# Eval-drift gate for swaps (the safety net: a swap must not regress behavior)
# --------------------------------------------------------------------------- #

# Per-metric direction rules (adopted from the llm-eval-drift-release-gates
# review): quality metrics regress DOWN; safety-violation regresses on ANY
# increase; latency/cost regress UP.
_QUALITY_METRICS = ("overall_score", "avg_correctness", "avg_groundedness")
_COST_METRICS = ("latency_p50_ms", "latency_p95_ms", "unit_output_cost_avg")
_ROBUSTNESS_METRICS = ("realism_gap",)
_SAFETY_METRIC = "safety_violation_rate"


# Static diagnostics data keyed by metric name — no new runtime dependencies.
_METRIC_DIAGNOSTICS: Dict[str, Tuple[str, List[str]]] = {
    "overall_score": (
        "quality_regression",
        [
            "Review training data for correctness coverage",
            "Inspect eval cases for scoring anomalies",
            "Run a broader golden set to confirm the regression is real",
        ],
    ),
    "avg_correctness": (
        "quality_regression",
        [
            "Review training data for correctness coverage",
            "Inspect eval cases for scoring anomalies",
            "Run a broader golden set to confirm the regression is real",
        ],
    ),
    "avg_groundedness": (
        "quality_regression",
        [
            "Check that the candidate model respects context grounding",
            "Inspect retrieval/citation behavior for candidate model",
        ],
    ),
    "safety_violation_rate": (
        "safety_regression",
        [
            "Run safety red-teaming on candidate",
            "Review refusal coverage for must-refuse categories",
            "Escalate to safety review before adopting candidate",
        ],
    ),
    "latency_p50_ms": (
        "cost_regression",
        [
            "Profile latency/token usage for candidate model",
            "Check for context-length increases vs incumbent",
        ],
    ),
    "latency_p95_ms": (
        "cost_regression",
        [
            "Profile latency/token usage for candidate model",
            "Check tail-latency outliers; may indicate retries or timeouts",
        ],
    ),
    "unit_output_cost_avg": (
        "cost_regression",
        [
            "Profile latency/token usage for candidate model",
            "Compare token-output distribution between candidate and incumbent",
        ],
    ),
    "realism_gap": (
        "robustness_regression",
        [
            "Inspect benchmark-vs-realistic case pairs for behavior divergence",
            "Review candidate for benchmark-gaming or distribution shift",
        ],
    ),
}


def compare_eval_metrics(
    baseline: Mapping[str, float],
    current: Mapping[str, float],
    *,
    threshold: float = 0.03,
) -> dict:
    """Multi-metric drift comparison between a candidate's eval (``current``) and
    the incumbent's (``baseline``). Returns ``{"regressed": bool, "drifted": [...]}``.

    Quality down > threshold, safety up at all, latency/cost up > threshold, or
    an absolute increase in benchmark-vs-realistic behavior gap > threshold each
    count as a regression. ``threshold`` is a relative delta for quality/cost
    and an absolute delta for bounded robustness metrics. NOTE this is not a
    significance test — small eval sets make it noise-prone, so treat it as a
    floor and prefer a statistical test when the golden set is small.

    Each entry in ``drifted`` includes ``likely_cause_bucket`` and
    ``recommended_next_steps`` for actionable operator diagnostics."""
    drifted: List[dict] = []

    def rel(b: float, c: float) -> float:
        return (c - b) / b if b else (0.0 if c == 0 else 1.0)

    def _annotate(entry: dict) -> dict:
        metric = entry["metric"]
        cause, steps = _METRIC_DIAGNOSTICS.get(metric, ("unknown_regression", []))
        entry["likely_cause_bucket"] = cause
        entry["recommended_next_steps"] = list(steps)
        return entry

    for metric in _QUALITY_METRICS:
        if metric in baseline and metric in current:
            d = rel(float(baseline[metric]), float(current[metric]))
            if d < -threshold:
                drifted.append(_annotate({"metric": metric, "rel_delta": d, "direction": "down"}))
    if _SAFETY_METRIC in baseline and _SAFETY_METRIC in current:
        if float(current[_SAFETY_METRIC]) > float(baseline[_SAFETY_METRIC]):
            drifted.append(
                _annotate(
                    {
                        "metric": _SAFETY_METRIC,
                        "abs_delta": float(current[_SAFETY_METRIC])
                        - float(baseline[_SAFETY_METRIC]),
                        "direction": "up",
                    }
                )
            )
    for metric in _COST_METRICS:
        if metric in baseline and metric in current:
            d = rel(float(baseline[metric]), float(current[metric]))
            if d > threshold:
                drifted.append(_annotate({"metric": metric, "rel_delta": d, "direction": "up"}))
    for metric in _ROBUSTNESS_METRICS:
        if metric in baseline and metric in current:
            delta = float(current[metric]) - float(baseline[metric])
            if delta > threshold:
                drifted.append(_annotate({"metric": metric, "abs_delta": delta, "direction": "up"}))
    return {"regressed": bool(drifted), "drifted": drifted}


@dataclass(frozen=True)
class ModelSelectionConfig:
    enabled: bool = False
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS
    count: int = 3
    fallback_model: str = ""

    @property
    def active(self) -> bool:
        return self.enabled

    def to_dict(self) -> dict:
        return dict(self.__dict__, active=self.active)

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "ModelSelectionConfig":
        env = os.environ if environ is None else environ
        enabled = str(env.get("MAC_MODEL_SELECT_ENABLED") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        raw = str(env.get("MAC_MODEL_SELECT_INTERVAL_SECONDS") or "").strip()
        try:
            interval = max(MIN_INTERVAL_SECONDS, float(raw)) if raw else DEFAULT_INTERVAL_SECONDS
        except ValueError:
            interval = DEFAULT_INTERVAL_SECONDS
        # Fallback = the fleet's configured strong default, so a discovery/network
        # failure preserves today's behavior rather than blanking the model.
        fallback = str(
            env.get("MAC_ROUTER_DEFAULT_MODEL") or env.get("MAC_HERMES_GATEWAY_MODEL") or ""
        ).strip()
        if fallback == "*":
            fallback = ""
        return cls(enabled=enabled, interval_seconds=interval, fallback_model=fallback)


class ModelSelectionService:
    """Periodically refresh the fleet's powerhouse-model choice: discover what's
    leading (web search) → moderate by what the gateway can route → select top N
    → persist. Mirrors the other hub daemons (own thread, env-gated)."""

    def __init__(
        self,
        control_plane: Any,
        config: ModelSelectionConfig,
        *,
        searcher: Optional[Callable[[str, int], List[dict]]] = None,
        swap_evaluator: Optional[Callable[[List[str], List[str]], dict]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.control_plane = control_plane
        self.config = config
        self._environ = os.environ if environ is None else environ
        # swap_evaluator(candidate_models, incumbent_models) -> {"approved": bool,
        # "detail": ...}. When None, a swap is recorded pending an operator/eval
        # promotion (the safety net — routing never changes on an unvalidated
        # swap). If no evaluator is injected, try to auto-build the golden-set
        # eval-drift evaluator from config (task_7ae3b6bd); it stays None (=>
        # operator-gated) unless MAC_MODEL_SWAP_EVAL_ENABLED + a router URL are
        # set, so the safe default is preserved.
        if swap_evaluator is None:
            try:
                from mac.eval_runner import build_swap_evaluator

                swap_evaluator = build_swap_evaluator(self._environ)
            except Exception:  # noqa: BLE001 - evaluator is optional; stay operator-gated.
                swap_evaluator = None
        self._swap_evaluator = swap_evaluator
        self._searcher = searcher
        self._stop = threading.Event()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last: Optional[dict] = None

    def _resolve_searcher(self) -> Optional[Callable[[str, int], List[dict]]]:
        if self._searcher is not None:
            return self._searcher
        try:
            from mac.firecrawl_gateway import search_web

            return search_web
        except Exception:  # noqa: BLE001
            return None

    def start(self) -> bool:
        if not self.config.active:
            return False
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            t = threading.Thread(target=self._loop, name="mac-model-selection", daemon=True)
            self._thread = t
            t.start()
        self._observe("model.selection.started", "info", {"config": self.config.to_dict()})
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        with self._state_lock:
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=max(0.0, timeout))
        return t is None or not t.is_alive()

    def status(self) -> dict:
        with self._state_lock:
            t = self._thread
            last = copy.deepcopy(self._last)
        return {
            "schema": "mac.model_selection_service.v1",
            "config": self.config.to_dict(),
            "thread_alive": bool(t is not None and t.is_alive()),
            "active": read_active(environ=self._environ),
            "pending": read_pending(environ=self._environ),
            "last_run": last,
        }

    def promote(self, *, actor: str = "operator") -> dict:
        """Promote the pending swap to active (operator action, or an automated
        eval-drift gate). No-op if nothing is pending."""
        promoted = promote_pending(environ=self._environ)
        report = {
            "status": "ok" if promoted else "nothing_pending",
            "promoted": promoted,
            "actor": actor,
        }
        self._observe("model.selection.promote", "info", report)
        return report

    def _loop(self) -> None:
        if self._stop.wait(max(0.0, self.config.initial_delay_seconds)):
            return
        while not self._stop.is_set():
            try:
                self.run_once(trigger="scheduled")
            except Exception:  # noqa: BLE001
                _log.warning("model-selection tick failed", exc_info=True)
            if self._stop.wait(max(0.01, self.config.interval_seconds)):
                return

    def run_once(self, *, trigger: str = "operator") -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "busy", "trigger": trigger}
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            searcher = self._resolve_searcher()
            results = discover_via_web(searcher) if searcher is not None else []
            # Parse the FULL provider specs (allowlist-aware) so available models
            # are the router's exact native ids, not provider-name-prefixed ones.
            from mac.provider_router import providers_from_env

            provider_objs = providers_from_env(self._environ)
            available = available_models_from_providers(provider_objs)
            providers = [p.name for p in provider_objs]
            sel = select_powerhouse_models(
                results,
                available,
                n=self.config.count,
                fallback=self.config.fallback_model,
                now=now,
                cost_lookup=_models_dev_cost,
            )
            report = {
                "status": "ok",
                "trigger": trigger,
                "selection": sel.to_dict(),
                "providers": providers,
            }
            active = read_active(environ=self._environ)
            active_models = list((active or {}).get("models", []))

            if not sel.models:
                # Discovery yielded nothing (and no fallback) — keep whatever's
                # active. Never blank the model.
                report["outcome"] = "no_candidate"
            elif active is None:
                # Bootstrap. There IS an incumbent even with nothing adopted yet:
                # the fleet's deploy-default model that tasks serve today. If an
                # eval gate is enabled and that default differs from the proposed
                # set, gate the first adoption against it too — otherwise the very
                # first selection would flip routing to unvalidated models,
                # exactly the regression the gate exists to prevent.
                incumbent = [self.config.fallback_model] if self.config.fallback_model else []
                if self._swap_evaluator is not None and incumbent and incumbent != sel.models:
                    gate = None
                    try:
                        gate = self._swap_evaluator(sel.models, incumbent)
                    except Exception as exc:  # noqa: BLE001 - gate failure => don't adopt.
                        gate = {"approved": False, "detail": "evaluator error: %s" % exc}
                    report["gate"] = gate
                    if gate and gate.get("approved"):
                        set_active(sel, environ=self._environ)
                        report["outcome"] = "adopted_bootstrap_gate_passed"
                    else:
                        set_pending(sel, environ=self._environ)
                        report["outcome"] = "pending_promotion"
                        report["note"] = (
                            "first selection proposed; routing stays on the deploy "
                            "default until promoted (eval-drift gate or "
                            "`mac admin fleet model-selection promote`)"
                        )
                else:
                    # No gate configured, or no distinct incumbent to compare
                    # against — adopt immediately (nothing to regress from).
                    set_active(sel, environ=self._environ)
                    report["outcome"] = "adopted_bootstrap"
            elif sel.models == active_models:
                # Same choice — refresh in place (ladder/provenance may change),
                # no swap, no gate.
                set_active(sel, environ=self._environ)
                report["outcome"] = "refreshed"
            elif sel.source != "dynamic":
                # A fallback must never displace a good active choice on a
                # transient discovery failure.
                report["outcome"] = "kept_active_fallback_only"
            else:
                # A genuine SWAP. This is the safety net: routing must not change
                # on an unvalidated swap. Gate it — adopt only if an evaluator
                # approves (no behavioral regression); else record pending for
                # an operator/eval to promote.
                gate = None
                if self._swap_evaluator is not None:
                    try:
                        gate = self._swap_evaluator(sel.models, active_models)
                    except Exception as exc:  # noqa: BLE001 - gate failure => don't adopt.
                        gate = {"approved": False, "detail": "evaluator error: %s" % exc}
                report["gate"] = gate
                if gate and gate.get("approved"):
                    set_active(sel, environ=self._environ)
                    report["outcome"] = "adopted_gate_passed"
                else:
                    set_pending(sel, environ=self._environ)
                    report["outcome"] = "pending_promotion"
                    report["note"] = (
                        "swap proposed; routing unchanged until promoted "
                        "(eval-drift gate or `mac admin fleet model-selection promote`)"
                    )
            report["active"] = read_active(environ=self._environ)
            report["pending"] = read_pending(environ=self._environ)
            with self._state_lock:
                self._last = report
            self._observe("model.selection.run", "info", report)
            return report
        finally:
            self._run_lock.release()

    def _observe(self, event: str, level: str, detail: dict) -> None:
        try:
            self.control_plane.record_log(
                event,
                layer="control_plane",
                source="model-selection",
                level=level,
                subject_type="service",
                subject_id="model-selection",
                detail=detail,
            )
        except Exception:  # noqa: BLE001
            _log.warning("could not record model-selection telemetry", exc_info=True)


__all__ = [
    "MODEL_FAMILIES",
    "ModelSelection",
    "ModelSelectionConfig",
    "ModelSelectionService",
    "extract_leading_families",
    "moderate_by_availability",
    "select_powerhouse_models",
    "model_strength_ladder",
    "resolve_strength",
    "resolve_strength_from_selection",
    "discover_via_web",
    "available_models_from_providers",
    "selection_file_path",
    "read_selection",
    "read_active",
    "read_pending",
    "selected_models",
    "set_active",
    "set_pending",
    "promote_pending",
    "write_selection",
    "compare_eval_metrics",
    "DEFAULT_QUERIES",
]
