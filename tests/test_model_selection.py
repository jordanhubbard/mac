"""Tests for dynamic powerhouse-model selection (mac.model_selection)."""

from __future__ import annotations

from mac.model_selection import (
    ModelSelection,
    available_models_from_providers,
    discover_via_web,
    extract_leading_families,
    moderate_by_availability,
    select_powerhouse_models,
)


def _results(*texts):
    return [{"title": t, "description": ""} for t in texts]


def test_extract_ranks_by_mention_frequency():
    results = _results(
        "Claude Opus 4.8 leads coding benchmarks",
        "GPT-5.2 vs Claude Opus 4.8 head to head",
        "Gemini 2.5 Pro review",
        "Claude Opus 4 still the best for agents",
    )
    families = extract_leading_families(results)
    assert families[0] == "claude-opus"  # mentioned 3x
    assert set(families) >= {"claude-opus", "gpt-5", "gemini-pro"}


def test_moderate_drops_unavailable_families():
    leading = ["claude-opus", "gpt-5", "gemini-pro"]
    available = [
        "azure/anthropic/claude-opus-4-8",
        "openai/gpt-5-2",
        # no gemini available
    ]
    chosen = moderate_by_availability(leading, available)
    assert chosen == ["azure/anthropic/claude-opus-4-8", "openai/gpt-5-2"]


def test_moderate_picks_newest_available_of_family():
    leading = ["claude-opus"]
    available = [
        "azure/anthropic/claude-opus-4-1",
        "azure/anthropic/claude-opus-4-8",
        "azure/anthropic/claude-opus-4-2",
    ]
    assert moderate_by_availability(leading, available) == [
        "azure/anthropic/claude-opus-4-8"
    ]


def test_select_end_to_end_dynamic():
    results = _results(
        "Claude Opus 4.8 tops the leaderboard",
        "GPT-5.2 close second",
        "Gemini 2.5 Pro third",
        "Grok 4 rising",
    )
    available = [
        "azure/anthropic/claude-opus-4-8",
        "openai/gpt-5-2",
        "google/gemini-2.5-pro",
        "xai/grok-4",
    ]
    sel = select_powerhouse_models(results, available, n=3, fallback="fb-model", now="T")
    assert sel.source == "dynamic"
    assert len(sel.models) == 3
    assert sel.models[0] == "azure/anthropic/claude-opus-4-8"
    assert "fb-model" not in sel.models
    assert sel.available_count == 4


def test_select_falls_back_when_nothing_available():
    results = _results("Gemini 2.5 Pro is great")
    available = ["azure/anthropic/claude-opus-4-8"]  # no gemini available
    sel = select_powerhouse_models(results, available, fallback="fallback-strong", now="T")
    assert sel.source == "fallback"
    assert sel.models == ["fallback-strong"]


def test_select_falls_back_when_no_web_signal():
    sel = select_powerhouse_models([], ["openai/gpt-5-2"], fallback="fb", now="T")
    assert sel.source == "fallback" and sel.models == ["fb"]


def test_discover_via_web_aggregates_and_isolates_failures():
    calls = []

    def searcher(q, limit):
        calls.append(q)
        if "leaderboard" in q:
            raise RuntimeError("boom")  # one query fails
        return [{"title": "Claude Opus 4.8", "description": ""}]

    out = discover_via_web(searcher, queries=("a best models", "b leaderboard", "c ranking"), limit=5)
    assert len(calls) == 3          # all queries attempted despite one failing
    assert any("Opus" in r["title"] for r in out)


def test_available_models_from_providers_empty_safe():
    # Unknown provider -> empty, never raises.
    assert available_models_from_providers(["definitely-not-a-provider"]) == []


def test_selection_serializes():
    sel = ModelSelection(models=["m1", "m2"], leading_families=["claude-opus"],
                         available_count=5, source="dynamic", at="T", ladder=["w", "s"])
    d = sel.to_dict()
    assert d["schema"] == "mac.model_selection.v1"
    assert d["models"] == ["m1", "m2"] and d["source"] == "dynamic"
    assert d["ladder"] == ["w", "s"]


# --------------------------------------------------------------------------- #
# Strength scale (1..10)
# --------------------------------------------------------------------------- #

from mac.model_selection import (  # noqa: E402
    ModelSelection as _MS,
    ModelSelectionConfig,
    ModelSelectionService,
    model_strength_ladder,
    read_selection,
    resolve_strength,
    resolve_strength_from_selection,
    selected_models,
    write_selection,
)


def test_ladder_orders_by_cost_then_name_tier():
    available = ["p/opus-big", "p/mini-cheap", "p/base-mid"]
    costs = {"p/opus-big": 15.0, "p/base-mid": 3.0, "p/mini-cheap": 0.2}
    ladder = model_strength_ladder(available, cost_lookup=lambda m: costs.get(m))
    assert ladder == ["p/mini-cheap", "p/base-mid", "p/opus-big"]  # weakest->strongest


def test_ladder_name_tier_fallback_when_no_cost():
    available = ["p/foo-opus", "p/foo-mini", "p/foo-base"]
    ladder = model_strength_ladder(available, cost_lookup=lambda m: None)
    assert ladder[0] == "p/foo-mini"     # weak tier
    assert ladder[-1] == "p/foo-opus"    # strong tier


def test_resolve_strength_endpoints_and_clamp():
    ladder = ["w", "a", "b", "c", "strong"]
    assert resolve_strength(1, ladder) == "w"
    assert resolve_strength(10, ladder) == "strong"
    assert resolve_strength(0, ladder) == "w"      # clamp low
    assert resolve_strength(99, ladder) == "strong"  # clamp high
    assert resolve_strength(5, ladder) in ladder
    assert resolve_strength(5, []) == ""            # empty ladder


def test_persist_and_resolve_strength_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "sel.json"))
    sel = _MS(models=["p/opus"], source="dynamic", at="T",
              ladder=["p/mini", "p/base", "p/opus"])
    write_selection(sel)
    assert selected_models() == ["p/opus"]
    assert resolve_strength_from_selection(1) == "p/mini"
    assert resolve_strength_from_selection(10) == "p/opus"
    assert read_selection()["ladder"] == ["p/mini", "p/base", "p/opus"]


def test_resolve_strength_no_selection_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "absent.json"))
    assert resolve_strength_from_selection(5) == ""   # caller falls back to fleet default


# --------------------------------------------------------------------------- #
# Service: refresh persists a dynamic selection (with injected searcher)
# --------------------------------------------------------------------------- #


class _FakeCP:
    def record_log(self, *a, **k):
        pass


def test_service_run_once_persists_dynamic_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "sel.json"))
    monkeypatch.setenv("MAC_ROUTER_PROVIDERS", "openai=https://x,0,key=secret:o")

    def searcher(q, limit):
        return [{"title": "Claude Opus 4.8 leads", "description": ""}]

    # Inject availability by monkeypatching the catalog adapter.
    import mac.model_selection as ms
    monkeypatch.setattr(ms, "available_models_from_providers",
                        lambda providers: ["azure/anthropic/claude-opus-4-8",
                                           "openai/gpt-5-mini"])
    svc = ModelSelectionService(_FakeCP(), ModelSelectionConfig(enabled=True, fallback_model="fb"),
                                searcher=searcher, environ=dict(os.environ))
    report = svc.run_once()
    assert report["status"] == "ok"
    assert report["selection"]["source"] == "dynamic"
    # Bootstrap: nothing to regress from, so the first dynamic pick is adopted.
    assert report["outcome"] == "adopted_bootstrap"
    # The active file drives both the powerhouse pick and the strength ladder.
    assert "azure/anthropic/claude-opus-4-8" in selected_models(dict(os.environ))
    assert resolve_strength_from_selection(10, dict(os.environ)) == "azure/anthropic/claude-opus-4-8"


# --------------------------------------------------------------------------- #
# Swap gate: a swap never silently changes routing
# --------------------------------------------------------------------------- #

from mac.model_selection import (  # noqa: E402
    compare_eval_metrics,
    read_active,
    read_pending,
    set_active,
)


def _svc_env(tmp_path, monkeypatch, providers="openai=https://x,0,key=secret:o", avail=None):
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "sel.json"))
    monkeypatch.setenv("MAC_ROUTER_PROVIDERS", providers)
    if avail is not None:
        import mac.model_selection as ms
        monkeypatch.setattr(ms, "available_models_from_providers", lambda p: avail)
    return dict(os.environ)


def test_swap_is_pending_not_adopted_without_evaluator(tmp_path, monkeypatch):
    # Active = opus. A refresh now finds gpt-5 leading+available -> a SWAP.
    env = _svc_env(tmp_path, monkeypatch, avail=["openai/gpt-5-2"])
    set_active(ModelSelection(models=["azure/anthropic/claude-opus-4-8"], source="dynamic",
                              at="T0", ladder=["azure/anthropic/claude-opus-4-8"]), environ=env)
    svc = ModelSelectionService(_FakeCP(), ModelSelectionConfig(enabled=True),
                                searcher=lambda q, n: [{"title": "GPT-5.2 leads", "description": ""}],
                                environ=env)
    report = svc.run_once()
    assert report["outcome"] == "pending_promotion"
    # Routing is UNCHANGED — active still the incumbent.
    assert selected_models(env) == ["azure/anthropic/claude-opus-4-8"]
    assert read_pending(env)["models"] == ["openai/gpt-5-2"]


def test_swap_adopted_when_evaluator_approves(tmp_path, monkeypatch):
    env = _svc_env(tmp_path, monkeypatch, avail=["openai/gpt-5-2"])
    set_active(ModelSelection(models=["azure/anthropic/claude-opus-4-8"], source="dynamic", at="T0",
                              ladder=["azure/anthropic/claude-opus-4-8"]), environ=env)
    svc = ModelSelectionService(_FakeCP(), ModelSelectionConfig(enabled=True),
                                searcher=lambda q, n: [{"title": "GPT-5.2 leads", "description": ""}],
                                swap_evaluator=lambda cand, inc: {"approved": True, "detail": "no regression"},
                                environ=env)
    report = svc.run_once()
    assert report["outcome"] == "adopted_gate_passed"
    assert selected_models(env) == ["openai/gpt-5-2"]
    assert read_pending(env) is None


def test_swap_stays_pending_when_evaluator_rejects(tmp_path, monkeypatch):
    env = _svc_env(tmp_path, monkeypatch, avail=["openai/gpt-5-2"])
    set_active(ModelSelection(models=["azure/anthropic/claude-opus-4-8"], source="dynamic", at="T0",
                              ladder=["azure/anthropic/claude-opus-4-8"]), environ=env)
    svc = ModelSelectionService(_FakeCP(), ModelSelectionConfig(enabled=True),
                                searcher=lambda q, n: [{"title": "GPT-5.2 leads", "description": ""}],
                                swap_evaluator=lambda cand, inc: {"approved": False, "detail": "correctness regressed"},
                                environ=env)
    report = svc.run_once()
    assert report["outcome"] == "pending_promotion"
    assert selected_models(env) == ["azure/anthropic/claude-opus-4-8"]  # incumbent kept
    # Operator promotion flips it.
    svc.promote()
    assert selected_models(env) == ["openai/gpt-5-2"]
    assert read_pending(env) is None


def test_compare_eval_metrics_direction_rules():
    base = {"overall_score": 0.90, "safety_violation_rate": 0.01, "latency_p95_ms": 1000, "cost_per_answer_avg": 0.02}
    # Quality drop >3%, safety up, latency up >3% => all regressions.
    worse = {"overall_score": 0.80, "safety_violation_rate": 0.05, "latency_p95_ms": 1200, "cost_per_answer_avg": 0.02}
    res = compare_eval_metrics(base, worse, threshold=0.03)
    assert res["regressed"] is True
    metrics = {d["metric"] for d in res["drifted"]}
    assert {"overall_score", "safety_violation_rate", "latency_p95_ms"} <= metrics
    # A better candidate does not regress.
    better = {"overall_score": 0.93, "safety_violation_rate": 0.0, "latency_p95_ms": 900, "cost_per_answer_avg": 0.02}
    assert compare_eval_metrics(base, better)["regressed"] is False


def test_safety_regresses_on_any_increase():
    base = {"overall_score": 0.9, "safety_violation_rate": 0.0}
    cur = {"overall_score": 0.9, "safety_violation_rate": 0.001}  # tiny safety increase
    assert compare_eval_metrics(base, cur)["regressed"] is True


import os  # noqa: E402


def test_moderate_natural_version_order():
    # 4-10 is newer than 4-9; a lexical sort would wrongly pick 4-9.
    from mac.model_selection import moderate_by_availability
    avail = ["anthropic/claude-opus-4-9", "anthropic/claude-opus-4-10", "anthropic/claude-opus-4-2"]
    assert moderate_by_availability(["claude-opus"], avail) == ["anthropic/claude-opus-4-10"]
