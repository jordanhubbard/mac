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
    set_pending,
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
    base = {"overall_score": 0.90, "safety_violation_rate": 0.01, "realism_gap": 0.01, "latency_p95_ms": 1000, "unit_output_cost_avg": 0.02}
    # Quality drop, safety up, realism gap up, latency up => regressions.
    worse = {"overall_score": 0.80, "safety_violation_rate": 0.05, "realism_gap": 0.10, "latency_p95_ms": 1200, "unit_output_cost_avg": 0.02}
    res = compare_eval_metrics(base, worse, threshold=0.03)
    assert res["regressed"] is True
    metrics = {d["metric"] for d in res["drifted"]}
    assert {"overall_score", "safety_violation_rate", "realism_gap", "latency_p95_ms"} <= metrics
    # A better candidate does not regress.
    better = {"overall_score": 0.93, "safety_violation_rate": 0.0, "realism_gap": 0.0, "latency_p95_ms": 900, "unit_output_cost_avg": 0.02}
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


# --------------------------------------------------------------------------- #
# #1 Namespace: available models are the router's exact native ids
# --------------------------------------------------------------------------- #


def test_available_models_uses_allowlist_verbatim_not_prefixed():
    # A provider with an explicit models= allowlist contributes those ids VERBATIM
    # (exactly what ProviderRouter._serves matches and the upstream expects) — not
    # provider-name-prefixed ids that the router would reject.
    from mac.provider_router import providers_from_env
    providers = providers_from_env(
        {"MAC_ROUTER_PROVIDERS": "openai=https://x,0,models=gpt-5-2|o3,key=K"}
    )
    got = available_models_from_providers(providers)
    assert got == ["gpt-5-2", "o3"]
    assert not any("/" in m for m in got)  # never "openai/gpt-5-2"


def test_available_models_wildcard_provider_enumerates_catalog(monkeypatch):
    # A wildcard (models=*) provider has no allowlist, so it enumerates the
    # catalog — and returns the catalog's own bare ids, unprefixed.
    import mac.model_selection as ms
    from mac.provider_router import providers_from_env
    monkeypatch.setattr(ms, "available_models_from_providers",
                        ms.available_models_from_providers)  # ensure real impl
    monkeypatch.setattr(
        ms.models_catalog,
        "list_agentic_models",
        lambda p: ["gpt-5-2", "gpt-5-mini"] if p == "openai" else [],
    )
    providers = providers_from_env({"MAC_ROUTER_PROVIDERS": "openai=https://x,0,models=*,key=K"})
    got = available_models_from_providers(providers)
    assert got == ["gpt-5-2", "gpt-5-mini"]


# --------------------------------------------------------------------------- #
# #5 Cost/strength ranking: unknown-cost strong model is not strength 1
# --------------------------------------------------------------------------- #


def test_unknown_cost_strong_model_is_not_weakest():
    # An Opus with no catalog price must NOT collapse to strength 1 just because
    # its cost is unknown — name tier is the primary strength signal.
    available = ["anthropic/claude-opus-4-8", "openai/gpt-5-mini", "openai/gpt-5-base"]
    # Only the mini has a known price; opus + base are unknown.
    costs = {"openai/gpt-5-mini": 0.2}
    ladder = model_strength_ladder(available, cost_lookup=lambda m: costs.get(m))
    assert ladder[-1] == "anthropic/claude-opus-4-8"   # strong tier wins the top
    assert ladder[0] == "openai/gpt-5-mini"            # weak tier at the bottom


def test_models_dev_cost_nested_id_keeps_middle_segment(monkeypatch):
    # nvidia/meta/llama must try (nvidia, "meta/llama") and (meta, "llama"),
    # never the old (nvidia, "llama") which dropped the middle path.
    import mac.model_selection as ms
    seen = []

    class _Info:
        cost_output = 4.2

    def fake_get_model_info(provider, model):
        seen.append((provider, model))
        if (provider, model) == ("meta", "llama"):
            return _Info()
        raise KeyError("not found")

    monkeypatch.setattr(ms.models_catalog, "get_model_info", fake_get_model_info)
    assert ms._models_dev_cost("nvidia/meta/llama") == 4.2
    assert ("nvidia", "meta/llama") in seen   # full remainder tried
    assert ("meta", "llama") in seen          # deeper boundary tried


# --------------------------------------------------------------------------- #
# #6 Persistence: concurrent set_pending / promote do not lose updates
# --------------------------------------------------------------------------- #


def test_concurrent_set_pending_and_active_no_lost_update(tmp_path, monkeypatch):
    import threading
    env = dict(os.environ)
    monkeypatch.setenv("MAC_MODEL_SELECTION_FILE", str(tmp_path / "sel.json"))
    env["MAC_MODEL_SELECTION_FILE"] = str(tmp_path / "sel.json")
    set_active(ModelSelection(models=["a/active"], source="dynamic", at="T",
                              ladder=["a/active"]), environ=env)

    barrier = threading.Barrier(2)
    errors = []

    def writer_pending():
        try:
            barrier.wait()
            for _ in range(50):
                set_pending(ModelSelection(models=["p/pending"], source="dynamic", at="T",
                                           ladder=["p/pending"]), environ=env)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def writer_active():
        try:
            barrier.wait()
            for _ in range(50):
                set_active(ModelSelection(models=["a/active"], source="dynamic", at="T",
                                          ladder=["a/active"]), environ=env)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=writer_pending)
    t2 = threading.Thread(target=writer_active)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert not errors, errors
    # The store is never corrupted; active is always readable and intact.
    assert read_active(env)["models"] == ["a/active"]


# --------------------------------------------------------------------------- #
# #7 First selection is gated against the deploy default when eval is enabled
# --------------------------------------------------------------------------- #


def test_bootstrap_gated_against_deploy_default(tmp_path, monkeypatch):
    # With an eval gate enabled and a deploy default incumbent, the FIRST
    # selection must not flip routing until it passes the gate.
    env = _svc_env(tmp_path, monkeypatch, avail=["openai/gpt-5-2"])
    svc = ModelSelectionService(
        _FakeCP(), ModelSelectionConfig(enabled=True, fallback_model="deploy/default"),
        searcher=lambda q, n: [{"title": "GPT-5.2 leads", "description": ""}],
        swap_evaluator=lambda cand, inc: {"approved": False, "detail": "regressed vs default"},
        environ=env,
    )
    report = svc.run_once()
    assert report["outcome"] == "pending_promotion"
    assert read_active(env) is None                   # routing still on the deploy default
    assert read_pending(env)["models"] == ["openai/gpt-5-2"]


def test_bootstrap_adopts_when_gate_passes(tmp_path, monkeypatch):
    env = _svc_env(tmp_path, monkeypatch, avail=["openai/gpt-5-2"])
    svc = ModelSelectionService(
        _FakeCP(), ModelSelectionConfig(enabled=True, fallback_model="deploy/default"),
        searcher=lambda q, n: [{"title": "GPT-5.2 leads", "description": ""}],
        swap_evaluator=lambda cand, inc: {"approved": True, "detail": "no regression"},
        environ=env,
    )
    report = svc.run_once()
    assert report["outcome"] == "adopted_bootstrap_gate_passed"
    assert selected_models(env) == ["openai/gpt-5-2"]


def test_bootstrap_adopts_immediately_without_evaluator(tmp_path, monkeypatch):
    # No eval gate configured -> first selection adopts immediately (nothing to
    # regress against). This preserves the operator-gated default behavior.
    env = _svc_env(tmp_path, monkeypatch, avail=["openai/gpt-5-2"])
    svc = ModelSelectionService(
        _FakeCP(), ModelSelectionConfig(enabled=True, fallback_model="deploy/default"),
        searcher=lambda q, n: [{"title": "GPT-5.2 leads", "description": ""}],
        environ=env,
    )
    report = svc.run_once()
    assert report["outcome"] == "adopted_bootstrap"
    assert selected_models(env) == ["openai/gpt-5-2"]
