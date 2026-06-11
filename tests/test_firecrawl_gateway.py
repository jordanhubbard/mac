import re

from fastapi.testclient import TestClient

from mac import firecrawl_gateway


def test_firecrawl_gateway_health():
    client = TestClient(firecrawl_gateway.create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_firecrawl_search_returns_firecrawl_v2_shape(monkeypatch):
    html = """
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdoc">Example Doc</a>
    <a class="result__snippet">A concise search result.</a>
    """
    monkeypatch.setattr(firecrawl_gateway, "_fetch_text", lambda url, allow_private: html)
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


def test_firecrawl_scrape_blocks_private_targets_by_default():
    client = TestClient(firecrawl_gateway.create_app())

    response = client.post("/v2/scrape", json={"url": "http://127.0.0.1:8789", "formats": ["markdown"]})

    assert response.status_code == 400
    assert "private network" in response.json()["detail"]


def test_firecrawl_scrape_returns_document_shape(monkeypatch):
    page = "<html><head><title>Sample</title></head><body><h1>Hello</h1><p>World</p></body></html>"
    monkeypatch.setenv("MAC_FIRECRAWL_GATEWAY_ALLOW_PRIVATE_TARGETS", "1")
    monkeypatch.setattr(firecrawl_gateway, "_fetch_text", lambda url, allow_private: page)
    client = TestClient(firecrawl_gateway.create_app())

    response = client.post("/v2/scrape", json={"url": "http://127.0.0.1/page", "formats": ["markdown", "html"]})

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
