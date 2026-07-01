"""Failure, parsing, and executable coverage for the Firecrawl-compatible gateway."""

from __future__ import annotations

import io
import socket
from urllib.error import HTTPError, URLError

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mac import firecrawl_gateway as gateway


def test_gateway_routes_validate_and_persist_crawl_jobs(monkeypatch):
    monkeypatch.setattr(gateway, "search_web", lambda query, limit: [{"url": f"https://{query}/{limit}"}])
    monkeypatch.setattr(
        gateway,
        "scrape_url",
        lambda url, formats: {"metadata": {"url": url}, "formats": sorted(formats)},
    )
    monkeypatch.setattr(
        gateway,
        "crawl_url",
        lambda url, limit, formats: [{"metadata": {"url": url}, "limit": limit, "formats": sorted(formats)}],
    )
    client = TestClient(gateway.create_app())
    assert client.get("/health").json()["status"] == "ok"
    assert client.post("/v2/search", json={}).status_code == 400
    assert client.post("/v2/search", json={"query": "mac", "limit": 999}).json()["data"]["web"][0]["url"].endswith("/25")
    assert client.post("/v2/scrape", json={}).status_code == 400
    assert client.post("/v2/scrape", json={"url": "https://example.com", "formats": "html"}).json()["data"]["formats"] == ["html"]
    assert client.post("/v2/crawl", json={}).status_code == 400
    created = client.post(
        "/v2/crawl",
        json={"url": "https://example.com", "limit": "2", "scrapeOptions": {"formats": ["links"]}},
    ).json()
    status = client.get("/v2/crawl/%s" % created["id"])
    assert status.status_code == 200
    assert status.json()["completed"] == 1
    assert client.get("/v2/crawl/missing").status_code == 404


def test_html_parsers_search_scrape_and_link_normalization(monkeypatch):
    search_html = """
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa"> Example A </a>
    <div class="result__snippet"> A useful result </div>
    <a class="result__a" href="https://example.com/a"> Duplicate </a>
    <a class="result__a" href="https://example.com/b"> Example B </a>
    """
    monkeypatch.setattr(gateway, "_fetch_text", lambda *_args, **_kwargs: search_html)
    results = gateway.search_web("query", 2)
    assert [item["url"] for item in results] == ["https://example.com/a", "https://example.com/b"]
    assert results[0]["description"] == "A useful result"

    page = """
    <html><head><title> Example &amp; Page </title><style>hidden</style></head>
    <body><main><h1>Hello</h1><script>secret</script><p>World</p>
    <a href="/relative#fragment">relative</a><a href="mailto:test@example.com">mail</a></main></body></html>
    """
    monkeypatch.setattr(gateway, "_validate_public_http_url", lambda _url: None)
    monkeypatch.setattr(gateway, "_fetch_text", lambda *_args, **_kwargs: page)
    document = gateway.scrape_url("https://example.com/base", {"markdown", "html", "links"})
    assert document["metadata"]["title"] == "Example & Page"
    assert "Hello" in document["markdown"] and "secret" not in document["markdown"]
    assert document["html"] == page
    assert document["links"] == ["https://example.com/relative"]
    assert "markdown" in gateway.scrape_url("https://example.com/base", {"unsupported"})


def test_crawl_follows_same_host_and_ignores_failed_pages(monkeypatch):
    calls = []

    def scrape(url, formats):
        calls.append((url, formats))
        if url.endswith("bad"):
            raise HTTPException(status_code=502, detail="bad")
        if len(calls) == 1:
            return {
                "metadata": {"url": url},
                "links": [
                    "https://other.example/outside",
                    "https://example.com/bad",
                    "https://example.com/good",
                ],
            }
        return {"metadata": {"url": url}}

    monkeypatch.setattr(gateway, "scrape_url", scrape)
    documents = gateway.crawl_url("https://example.com/start", 3, {"markdown"})
    assert [item["metadata"]["url"] for item in documents] == [
        "https://example.com/start",
        "https://example.com/good",
    ]


class _Headers:
    def __init__(self, charset=None):
        self.charset = charset

    def get_content_charset(self):
        return self.charset


class _Response:
    def __init__(self, payload: bytes, charset=None):
        self.payload = payload
        self.headers = _Headers(charset)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size):
        return self.payload[:size]


def test_fetch_text_success_truncation_and_transport_errors(monkeypatch):
    monkeypatch.setattr(gateway, "_validate_public_http_url", lambda _url: None)
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 4)
    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response("ééé".encode("latin-1"), "latin-1"),
    )
    assert gateway._fetch_text("https://example.com", allow_private=False) == "ééé"[:4]

    def http_error(*_args, **_kwargs):
        raise HTTPError("url", 503, "down", {}, io.BytesIO(b"down"))

    monkeypatch.setattr(gateway.urllib.request, "urlopen", http_error)
    with pytest.raises(HTTPException) as excinfo:
        gateway._fetch_text("https://example.com", allow_private=True)
    assert excinfo.value.status_code == 502

    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(HTTPException, match="offline"):
        gateway._fetch_text("https://example.com", allow_private=True)

    monkeypatch.setattr(
        gateway.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    with pytest.raises(HTTPException) as excinfo:
        gateway._fetch_text("https://example.com", allow_private=True)
    assert excinfo.value.status_code == 504


def test_public_url_validation_and_helpers(monkeypatch):
    with pytest.raises(HTTPException):
        gateway._validate_public_http_url("file:///tmp/private")

    monkeypatch.delenv("MAC_FIRECRAWL_GATEWAY_ALLOW_PRIVATE_TARGETS", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: [(None, None, None, None, ("93.184.216.34", 0))])
    gateway._validate_public_http_url("https://example.com")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: [(None, None, None, None, ("127.0.0.1", 0))])
    with pytest.raises(HTTPException, match="private"):
        gateway._validate_public_http_url("http://localhost")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args: (_ for _ in ()).throw(socket.gaierror("unknown")),
    )
    with pytest.raises(HTTPException, match="cannot resolve"):
        gateway._validate_public_http_url("https://missing.invalid")
    monkeypatch.setenv("MAC_FIRECRAWL_GATEWAY_ALLOW_PRIVATE_TARGETS", "YES")
    gateway._validate_public_http_url("http://127.0.0.1")

    assert gateway._formats("html") == {"html"}
    assert gateway._formats(["html", ""]) == {"html"}
    assert gateway._formats(None) == {"markdown"}
    assert gateway._bounded_int("bad", default=3, minimum=1, maximum=5) == 3
    assert gateway._bounded_int(99, default=3, minimum=1, maximum=5) == 5
    assert gateway._decode_duckduckgo_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com") == "https://example.com"
    assert gateway._absolute_links(
        "https://example.com/base",
        ["/a#fragment", "/a#other", "mailto:user@example.com", "https://other.example/b"],
    ) == ["https://example.com/a", "https://other.example/b"]
    assert gateway._clean_text(" A &amp;   B ") == "A & B"


def test_firecrawl_main_runs_uvicorn(monkeypatch):
    import uvicorn

    seen = []
    monkeypatch.setattr("sys.argv", ["mac-firecrawl-gateway", "--host", "0.0.0.0", "--port", "3456"])
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.append((app, kwargs)))
    gateway.main()
    assert seen[-1][1] == {"host": "0.0.0.0", "port": 3456, "log_level": "info"}
