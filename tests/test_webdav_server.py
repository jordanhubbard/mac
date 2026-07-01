from __future__ import annotations

import threading
import urllib.error
import urllib.request
from http import HTTPStatus

import pytest

from mac.webdav_server import WebDAVHandler, WebDAVServer


def test_webdav_server_serves_public_reads_without_http_writes(tmp_path):
    root = tmp_path / "published"
    root.mkdir()
    (root / "hello.txt").write_text("hello mac\n", encoding="utf-8")
    server = WebDAVServer(
        ("127.0.0.1", 0),
        WebDAVHandler,
        root=root,
        public_prefix="/artifacts/",
        max_upload_bytes=1024,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with urllib.request.urlopen(base + "/health", timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            assert b"mac.webdav.health.v1" in resp.read()
        req = urllib.request.Request(base + "/health", method="HEAD")
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            assert resp.read() == b""
        with urllib.request.urlopen(base + "/artifacts/hello.txt", timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            assert resp.read() == b"hello mac\n"

        req = urllib.request.Request(
            base + "/artifacts/new.txt",
            data=b"no ingress",
            method="PUT",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(req, timeout=5)  # noqa: S310
        assert excinfo.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_webdav_server_covers_read_only_dav_protocol(tmp_path):
    root = tmp_path / "published"
    root.mkdir()
    (root / "hello.txt").write_text("hello mac\n", encoding="utf-8")
    server = WebDAVServer(
        ("127.0.0.1", 0),
        WebDAVHandler,
        root=root,
        public_prefix="artifacts",
        max_upload_bytes=1024,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    def request(path, method):
        return urllib.request.urlopen(  # noqa: S310
            urllib.request.Request(base + path, method=method), timeout=5
        )

    try:
        with request("/artifacts/hello.txt", "HEAD") as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/plain")
            assert response.read() == b""
        with request("/artifacts/hello.txt", "OPTIONS") as response:
            assert response.status == HTTPStatus.NO_CONTENT
            assert response.headers["DAV"] == "1"
        with request("/artifacts/hello.txt", "PROPFIND") as response:
            assert response.status == 207
            assert b"getcontentlength" in response.read()
        with request("/artifacts/missing.txt", "PROPFIND") as response:
            assert b"404 Not Found" in response.read()

        for path, expected in (
            ("/wrong/file.txt", 404),
            ("/artifacts/", 403),
            ("/artifacts/%2e%2e/secret.txt", 403),
            ("/artifacts/missing.txt", 404),
        ):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                request(path, "GET")
            assert excinfo.value.code == expected

        for method in ("PUT", "MKCOL", "DELETE"):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                request("/artifacts/hello.txt", method)
            assert excinfo.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_webdav_main_closes_server_on_normal_and_interrupted_exit(tmp_path, monkeypatch, capsys):
    from mac import webdav_server as module

    instances = []

    class FakeServer:
        interrupt = False

        def __init__(self, address, handler, **kwargs):
            self.address = address
            self.handler = handler
            self.kwargs = kwargs
            self.closed = False
            instances.append(self)

        def serve_forever(self):
            if self.interrupt:
                raise KeyboardInterrupt

        def server_close(self):
            self.closed = True

    monkeypatch.setattr(module, "WebDAVServer", FakeServer)
    root = tmp_path / "root"
    assert module.main(
        ["--host", "127.0.0.1", "--port", "8080", "--root", str(root), "--public-prefix", "files"]
    ) == 0
    assert root.is_dir()
    assert instances[-1].address == ("127.0.0.1", 8080)
    assert instances[-1].closed
    assert "prefix=/files/" in capsys.readouterr().out

    FakeServer.interrupt = True
    assert module.main(["--port", "8081", "--root", str(root)]) == 130
    assert instances[-1].closed
