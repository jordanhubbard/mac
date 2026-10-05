import re

from fastapi.testclient import TestClient

from mac import firecrawl_gateway


def test_firecrawl_gateway_health():
    client = TestClient(firecrawl_gateway.create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_firecrawl_search_returns_firecrawl_v2_shape(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    html = """
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc">Example Doc</a>
    <a class="result__snippet">A concise search result.</a>
    """
    monkeypatch.setattr(firecrawl_gateway, "_fetch_text", lambda *_args, **_kwargs: html)
    client = TestClient(firecrawl_gateway.create_app())

    response = client.post("/v2/search", json={"query": "example", "limit": 3})

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "data": {
            "web": [
                {
                    "url": "https://example.com/doc",
                    "title": "Example Doc",
                    "description": "A concise search result.",
                }
            ]
        },
    }


def test_firecrawl_search_prefers_brave_backend(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key")
    payload = {
        "web": {
            "results": [
                {
                    "url": "https://brave.example/first",
                    "title": "First",
                    "description": "Primary result",
                },
                {
                    "url": "https://brave.example/second",
                    "title": "Second",
                    "description": "Second result",
                },
            ]
        }
    }
    seen = {}

    def fake_fetch_json(request):
        seen["url"] = request.full_url
        seen["headers"] = {key.lower(): value for key, value in request.header_items()}
        return payload

    monkeypatch.setattr(firecrawl_gateway, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(
        firecrawl_gateway,
        "_search_duckduckgo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("DuckDuckGo must not run when Brave succeeds")
        ),
    )
    client = TestClient(firecrawl_gateway.create_app())

    response = client.post("/v2/search", json={"query": "example", "limit": 1})

    assert response.status_code == 200
    assert response.json()["data"]["web"] == [
        {"url": "https://brave.example/first", "title": "First", "description": "Primary result"}
    ]
    assert "api.search.brave.com" in seen["url"]
    assert "count=1" in seen["url"]
    assert seen["headers"]["x-subscription-token"] == "brave-test-key"


def test_firecrawl_search_falls_back_to_duckduckgo_with_browser_agent(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key")
    monkeypatch.setattr(
        firecrawl_gateway,
        "_fetch_json",
        lambda _request: (_ for _ in ()).throw(
            firecrawl_gateway.HTTPException(status_code=502, detail="brave down")
        ),
    )
    seen = {}
    html = '<a class="result__a" href="https://example.com/found">Found</a>'

    def fake_fetch_text(_url, *, allow_private, user_agent=firecrawl_gateway.USER_AGENT):
        seen["user_agent"] = user_agent
        assert allow_private is False
        return html

    monkeypatch.setattr(firecrawl_gateway, "_fetch_text", fake_fetch_text)

    results = firecrawl_gateway.search_web("example", 3)

    assert results == [{"url": "https://example.com/found", "title": "Found", "description": ""}]
    assert seen["user_agent"] == firecrawl_gateway.BROWSER_USER_AGENT
    assert "Mozilla" in seen["user_agent"]


def test_firecrawl_search_uses_duckduckgo_without_brave_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(
        firecrawl_gateway,
        "_fetch_json",
        lambda _request: (_ for _ in ()).throw(
            AssertionError("Brave must not be queried without a key")
        ),
    )
    monkeypatch.setattr(firecrawl_gateway, "_fetch_text", lambda *_args, **_kwargs: "")

    assert firecrawl_gateway.search_web("example", 3) == []


def test_firecrawl_search_falls_back_when_brave_has_no_results(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-test-key")
    monkeypatch.setattr(firecrawl_gateway, "_fetch_json", lambda _request: {"web": {"results": []}})
    monkeypatch.setattr(
        firecrawl_gateway,
        "_search_duckduckgo",
        lambda query, limit: [{"url": "https://ddg.example/1", "title": query, "description": ""}],
    )

    assert firecrawl_gateway._brave_api_key() == "brave-test-key"
    assert firecrawl_gateway.search_web("example", 3) == [
        {"url": "https://ddg.example/1", "title": "example", "description": ""}
    ]


def test_firecrawl_scrape_blocks_private_targets_by_default():
    client = TestClient(firecrawl_gateway.create_app())

    response = client.post(
        "/v2/scrape", json={"url": "http://127.0.0.1:8789", "formats": ["markdown"]}
    )

    assert response.status_code == 400
    assert "private network" in response.json()["detail"]


def test_firecrawl_scrape_returns_document_shape(monkeypatch):
    page = "<html><head><title>Sample</title></head><body><h1>Hello</h1><p>World</p></body></html>"
    monkeypatch.setenv("MAC_FIRECRAWL_GATEWAY_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setattr(firecrawl_gateway, "_fetch_text", lambda url, allow_private: page)
    client = TestClient(firecrawl_gateway.create_app())

    response = client.post(
        "/v2/scrape", json={"url": "http://127.0.0.1/page", "formats": ["markdown", "html"]}
    )

    data = response.json()["data"]
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert data["metadata"]["title"] == "Sample"
    assert re.search(r"Hello\s+World", data["markdown"])
    assert data["html"] == page


def test_firecrawl_crawl_start_and_status(monkeypatch):
    documents = [{"markdown": "Seed", "metadata": {"sourceURL": "https://example.com"}}]
    monkeypatch.setattr(firecrawl_gateway, "crawl_url", lambda url, limit, formats: documents)
    client = TestClient(firecrawl_gateway.create_app())

    start = client.post("/v2/crawl", json={"url": "https://example.com", "limit": 1})
    status = client.get(f"/v2/crawl/{start.json()['id']}")

    assert start.status_code == 200
    assert start.json()["success"] is True
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["data"] == documents
