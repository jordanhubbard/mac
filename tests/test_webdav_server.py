from __future__ import annotations

import threading
import urllib.error
import urllib.request

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
