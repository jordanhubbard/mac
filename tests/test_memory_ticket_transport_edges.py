from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from mac import memory_vetting, tickets_mirror


def test_qdrant_post_scroll_pagination_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []
    responses = iter(
        [
            {"result": {"points": [{"id": 1}], "next_page_offset": "next"}},
            {"result": {"points": [{"id": 2}], "next_page_offset": None}},
            {"result": {"status": "acknowledged"}},
        ]
    )

    class Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode()

    def urlopen(request: Any, *, timeout: float) -> Response:
        requests.append((request, timeout))
        return Response(next(responses))

    monkeypatch.setattr(memory_vetting.urllib.request, "urlopen", urlopen)
    client = memory_vetting.QdrantClient("http://qdrant/", timeout=3, page=1)
    assert list(client.scroll("memories")) == [{"id": 1}, {"id": 2}]
    assert client.delete("memories", [1])["result"]["status"] == "acknowledged"
    assert client.base == "http://qdrant"
    assert len(requests) == 3
    second_body = json.loads(requests[1][0].data)
    assert second_body["offset"] == "next"


def test_qdrant_scroll_stops_when_page_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = memory_vetting.QdrantClient("http://qdrant")
    monkeypatch.setattr(
        client,
        "_post",
        lambda *_a, **_k: {
            "result": {"points": [], "next_page_offset": "unexpected-next"}
        },
    )
    assert list(client.scroll("empty")) == []


def test_tickets_dir_git_cwd_and_error_fallbacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    root = tmp_path / "root"
    (root / ".tickets").mkdir(parents=True)
    monkeypatch.setattr(
        tickets_mirror.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{root}\n"),
    )
    monkeypatch.setattr(tickets_mirror.os, "getcwd", lambda: str(tmp_path))
    assert tickets_mirror.tickets_dir() == root / ".tickets"

    cwd = tmp_path / "cwd"
    (cwd / ".tickets").mkdir(parents=True)
    monkeypatch.setattr(
        tickets_mirror.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no git")),
    )
    monkeypatch.setattr(tickets_mirror.os, "getcwd", lambda: str(cwd))
    assert tickets_mirror.tickets_dir() == cwd / ".tickets"

    monkeypatch.setattr(
        tickets_mirror.os,
        "getcwd",
        lambda: (_ for _ in ()).throw(OSError("cwd removed")),
    )
    assert tickets_mirror.tickets_dir() is None


def test_ticket_emit_requires_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAC_NO_TICKET_MIRROR", raising=False)
    assert tickets_mirror.emit({"title": "missing id"}) is None
    monkeypatch.setattr(tickets_mirror, "tickets_dir", lambda: None)
    assert tickets_mirror.emit({"id": "task-1", "title": "no mirror"}) is None
