from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from fastapi import FastAPI, HTTPException


USER_AGENT = os.environ.get(
    "MAC_FIRECRAWL_GATEWAY_USER_AGENT",
    "mac-firecrawl-gateway/0.1 (+https://github.com/jordanh/mac)",
)
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("MAC_FIRECRAWL_GATEWAY_TIMEOUT", "15"))
MAX_RESPONSE_BYTES = int(os.environ.get("MAC_FIRECRAWL_GATEWAY_MAX_BYTES", str(2 * 1024 * 1024)))
MAX_SEARCH_LIMIT = int(os.environ.get("MAC_FIRECRAWL_GATEWAY_MAX_SEARCH_LIMIT", "25"))
_CRAWL_JOBS: dict[str, dict[str, Any]] = {}


class DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._active_link: dict[str, str] | None = None
        self._active_snippet = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        classes = set(attr.get("class", "").split())
        if tag == "a" and "result__a" in classes:
            self._active_link = {"url": _decode_duckduckgo_url(attr.get("href", "")), "title": ""}
            self._text = []
        elif "result__snippet" in classes:
            self._active_snippet = True
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._active_link is not None or self._active_snippet:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active_link is not None:
            title = _clean_text(" ".join(self._text))
            if title and self._active_link.get("url"):
                self._active_link["title"] = title
                self._active_link["description"] = ""
                self.results.append(self._active_link)
            self._active_link = None
            self._text = []
        elif self._active_snippet and tag in {"a", "div", "td"}:
            snippet = _clean_text(" ".join(self._text))
            if snippet and self.results and not self.results[-1].get("description"):
                self.results[-1]["description"] = snippet
            self._active_snippet = False
            self._text = []


class PageTextParser(HTMLParser):
    _block_tags = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    _skip_tags = {"script", "style", "noscript", "svg", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            self._title_parts = []
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in self._block_tags:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        text = _clean_text(data)
        if text:
            self._parts.append(text)
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self.title = _clean_text(" ".join(self._title_parts))
        if tag in self._block_tags:
            self._parts.append("\n")

    @property
    def markdown(self) -> str:
        text = html.unescape("".join(self._parts))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
        return text.strip()


def create_app() -> FastAPI:
    app = FastAPI(title="mac Firecrawl-compatible Web Gateway", version="0.1.0")

    @app.get("/")
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "mac-firecrawl-gateway"}

    @app.post("/v2/search")
    def search(payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        limit = _bounded_int(payload.get("limit"), default=5, minimum=1, maximum=MAX_SEARCH_LIMIT)
        return {"success": True, "data": {"web": search_web(query, limit)}}

    @app.post("/v2/scrape")
    def scrape(payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        formats = _formats(payload.get("formats") or payload.get("formats[]") or ["markdown"])
        return {"success": True, "data": scrape_url(url, formats)}

    @app.post("/v2/crawl")
    def crawl(payload: dict[str, Any]) -> dict[str, Any]:
        url = str(payload.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="url is required")
        limit = _bounded_int(payload.get("limit"), default=1, minimum=1, maximum=10)
        formats = _formats((payload.get("scrapeOptions") or {}).get("formats") or ["markdown"])
        job_id = hashlib.sha256(f"{time.time()}:{url}".encode("utf-8")).hexdigest()[:24]
        documents = crawl_url(url, limit, formats)
        _CRAWL_JOBS[job_id] = {
            "success": True,
            "status": "completed",
            "completed": len(documents),
            "total": len(documents),
            "creditsUsed": 0,
            "expiresAt": None,
            "next": None,
            "data": documents,
        }
        return {"success": True, "id": job_id, "url": url}

    @app.get("/v2/crawl/{job_id}")
    def crawl_status(job_id: str) -> dict[str, Any]:
        job = _CRAWL_JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="crawl job not found")
        return job

    return app


def search_web(query: str, limit: int) -> list[dict[str, str]]:
    params = urllib.parse.urlencode({"q": query})
    body = _fetch_text(f"https://html.duckduckgo.com/html/?{params}", allow_private=False)
    parser = DuckDuckGoHTMLParser()
    parser.feed(body)
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for result in parser.results:
        url = result.get("url", "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "url": url,
                "title": result.get("title") or url,
                "description": result.get("description") or "",
            }
        )
        if len(results) >= limit:
            break
    return results


def scrape_url(url: str, formats: set[str]) -> dict[str, Any]:
    _validate_public_http_url(url)
    body = _fetch_text(url, allow_private=_allow_private_targets())
    parser = PageTextParser()
    parser.feed(body)
    document: dict[str, Any] = {
        "metadata": {
            "title": parser.title,
            "sourceURL": url,
            "url": url,
        }
    }
    if "markdown" in formats:
        document["markdown"] = parser.markdown
    if "html" in formats:
        document["html"] = body
    if "links" in formats:
        document["links"] = _absolute_links(url, parser.links)
    if not formats.intersection({"markdown", "html", "links"}):
        document["markdown"] = parser.markdown
    return document


def crawl_url(seed_url: str, limit: int, formats: set[str]) -> list[dict[str, Any]]:
    seed = scrape_url(seed_url, formats | {"links"})
    documents = [seed]
    base_host = urllib.parse.urlsplit(seed_url).hostname
    for link in seed.get("links", []):
        if len(documents) >= limit:
            break
        parsed = urllib.parse.urlsplit(link)
        if parsed.hostname != base_host:
            continue
        try:
            documents.append(scrape_url(link, formats))
        except HTTPException:
            continue
    return documents[:limit]


def _fetch_text(url: str, *, allow_private: bool) -> str:
    if not allow_private:
        _validate_public_http_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raw = raw[:MAX_RESPONSE_BYTES]
            content_type = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="upstream fetch timed out") from exc
    return raw.decode(content_type, errors="replace")


def _validate_public_http_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="url must be http or https")
    if _allow_private_targets():
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise HTTPException(status_code=400, detail=f"cannot resolve host: {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HTTPException(status_code=400, detail="private network targets are disabled")


def _allow_private_targets() -> bool:
    return os.environ.get("MAC_FIRECRAWL_GATEWAY_ALLOW_PRIVATE_TARGETS", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _formats(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    return {"markdown"}


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _decode_duckduckgo_url(raw: str) -> str:
    if raw.startswith("//"):
        raw = "https:" + raw
    parsed = urllib.parse.urlsplit(raw)
    query = urllib.parse.parse_qs(parsed.query)
    target = query.get("uddg", [""])[0]
    return urllib.parse.unquote(target or raw)


def _absolute_links(base_url: str, links: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for link in links:
        absolute = urllib.parse.urljoin(base_url, link)
        parsed = urllib.parse.urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        normalized = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        if normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return output


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the mac Firecrawl-compatible web gateway.")
    parser.add_argument("--host", default=os.environ.get("FIRECRAWL_BIND_ADDR", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FIRECRAWL_PORT", "3002")))
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
