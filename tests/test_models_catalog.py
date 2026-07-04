"""Tests for native models.dev catalog access."""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import models_catalog as catalog


FIXTURE = Path(__file__).parent / "fixtures" / "models_dev_api.json"


def _reset_catalog(monkeypatch, tmp_path):
    cache_file = tmp_path / "models-dev-cache.json"
    cache_file.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("MAC_MODELS_DEV_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(catalog, "_catalog_cache", {})
    monkeypatch.setattr(catalog, "_catalog_cache_time", 0.0)

    def no_network(url):
        raise AssertionError("fixture-backed tests must not fetch %s" % url)

    monkeypatch.setattr(catalog, "_fetch_url_json", no_network)
    return cache_file


def test_fetch_models_dev_uses_fixture_disk_cache_without_network(tmp_path, monkeypatch):
    _reset_catalog(monkeypatch, tmp_path)
    data = catalog.fetch_models_dev()
    assert sorted(data) == ["google", "openai"]
    assert catalog.fetch_models_dev() is data


def test_force_refresh_falls_back_to_stale_fixture_cache(tmp_path, monkeypatch):
    _reset_catalog(monkeypatch, tmp_path)
    monkeypatch.setattr(catalog, "_disk_cache_age_seconds", lambda: 7200.0)
    data = catalog.fetch_models_dev(force_refresh=True)
    assert "openai" in data


def test_list_agentic_models_filters_noise_and_provider_aliases(tmp_path, monkeypatch):
    _reset_catalog(monkeypatch, tmp_path)
    assert catalog.list_agentic_models("openai") == ["gpt-5-2"]
    assert catalog.list_agentic_models("gemini") == ["gemini-3-pro"]
    assert catalog.list_agentic_models("definitely-missing") == []


def test_get_model_info_parses_costs_limits_and_modalities(tmp_path, monkeypatch):
    _reset_catalog(monkeypatch, tmp_path)
    info = catalog.get_model_info("OpenAI", "GPT-5-2")
    assert info is not None
    assert info.id == "gpt-5-2"
    assert info.provider_id == "openai"
    assert info.cost_output == pytest.approx(10.0)
    assert info.cost_cache_read == pytest.approx(0.125)
    assert info.context_window == 400000
    assert info.max_output == 8192
    assert info.structured_output is True
    assert info.supports_vision() is False

    gemini = catalog.get_model_info("gemini", "gemini-3-pro")
    assert gemini is not None
    assert gemini.provider_id == "google"
    assert gemini.supports_vision() is True
