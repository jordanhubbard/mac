"""mac-nyx7: tests for the TokenHub wildcard-ladder refresh client.

stdlib-only; a fake opener stands in for the live TokenHub admin endpoint.
"""

from __future__ import annotations

import io
import json

from mac import tokenhub_wildcard as tw


def test_wildcard_url_from_env_explicit_and_derived():
    assert tw.wildcard_url_from_env({"MAC_TOKENHUB_WILDCARD_URL": "http://x/y"}) == "http://x/y"
    assert (
        tw.wildcard_url_from_env({"TOKENHUB_URL": "http://hub:8090/"})
        == "http://hub:8090/admin/v1/wildcard-models"
    )
    assert tw.wildcard_url_from_env({}) == ""


def test_extract_ladder_normalizes_shapes():
    # bare list of strings
    assert tw.extract_ladder(["a", "b"]) == [
        {"rank": 0, "model_id": "a"},
        {"rank": 1, "model_id": "b"},
    ]
    # dict wrapper + dict entries with mixed id keys
    payload = {
        "models": [
            {"model_id": "gpt", "provider_id": "openai", "rank": 2, "quality": 0.9},
            {"id": "claude", "cost_usd": 0.01},
        ]
    }
    ladder = tw.extract_ladder(payload)
    assert ladder[0]["model_id"] == "gpt"
    assert ladder[0]["provider_id"] == "openai"
    assert ladder[0]["rank"] == 2
    assert ladder[1]["model_id"] == "claude"  # normalized from "id"
    # unrecognized shape
    assert tw.extract_ladder(42) == []


def test_ladder_to_record_caps_and_shapes():
    payload = {"ladder": [f"m{i}" for i in range(80)]}
    record = tw.ladder_to_record(payload)
    assert record["name"] == "tokenhub.wildcard.refresh"
    assert record["layer"] == "tokenhub"
    assert record["detail"]["count"] == 80
    assert len(record["detail"]["ladder"]) == 50  # capped


class _FakeResp:
    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_wildcard_ladder_sends_auth_and_parses():
    captured = {}

    def fake_opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["method"] = req.get_method()
        return _FakeResp(json.dumps({"models": ["a", "b"]}))

    payload = tw.fetch_wildcard_ladder(
        "http://hub/admin/v1/wildcard-models", "admintok", _opener=fake_opener
    )
    assert payload == {"models": ["a", "b"]}
    assert captured["auth"] == "Bearer admintok"
    assert captured["method"] == "GET"


def test_refresh_skips_without_token():
    # URL present but no admin token → clean skip, no exception, no record.
    result = tw.refresh_wildcard_ladder(
        observability=None, env={"TOKENHUB_URL": "http://hub:8090"}
    )
    assert result["status"] == "skipped"
    assert result["have_url"] is True
    assert result["have_token"] is False


def test_refresh_records_when_configured():
    recorded = {}

    class _Obs:
        def record_observation(self, **kwargs):
            recorded.update(kwargs)

    def fake_opener(req, timeout=None):
        return _FakeResp(json.dumps({"models": [{"model_id": "gpt"}, {"model_id": "claude"}]}))

    result = tw.refresh_wildcard_ladder(
        observability=_Obs(),
        env={
            "TOKENHUB_URL": "http://hub:8090",
            "MAC_TOKENHUB_ADMIN_TOKEN": "admintok",
        },
        _opener=fake_opener,
    )
    assert result["status"] == "refreshed"
    assert result["count"] == 2
    assert recorded["name"] == "tokenhub.wildcard.refresh"
    assert recorded["detail"]["count"] == 2
