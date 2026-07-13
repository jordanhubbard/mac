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


def test_agentfs_mode_allows_token_authed_writes_reads_stay_open(tmp_path):
    """AgentFS v2 (shared fleet filesystem): with a write token set, callers
    presenting it can PUT/DELETE while reads stay open to anyone who can
    reach the (tailnet-bound) socket — the old SMB share's posture, minus the
    mount privileges sandboxes/pods can't have."""
    root = tmp_path / "agentfs"
    root.mkdir()
    token = "agentfs-secret-token"
    server = WebDAVServer(
        ("127.0.0.1", 0),
        WebDAVHandler,
        root=root,
        public_prefix="/agentfs/",
        max_upload_bytes=1024,
        write_token=token,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    auth = {"Authorization": "Bearer %s" % token}
    try:
        # Write with the token: created, nested path auto-made.
        put = urllib.request.Request(
            base + "/agentfs/demos/fluid_sim.py",
            data=b"print('sim')\n",
            method="PUT",
            headers=auth,
        )
        with urllib.request.urlopen(put, timeout=5) as resp:  # noqa: S310
            assert resp.status == 201
        assert (root / "demos" / "fluid_sim.py").read_bytes() == b"print('sim')\n"

        # Any reader (no token) gets it back — the shared-filesystem point.
        with urllib.request.urlopen(base + "/agentfs/demos/fluid_sim.py", timeout=5) as resp:  # noqa: S310
            assert resp.read() == b"print('sim')\n"

        # Write WITHOUT the token is refused (401), and a wrong token too.
        for header in ({}, {"Authorization": "Bearer wrong"}):
            bad = urllib.request.Request(
                base + "/agentfs/x.txt", data=b"nope", method="PUT", headers=header
            )
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(bad, timeout=5)  # noqa: S310
            assert excinfo.value.code == 401

        # Oversize upload is rejected even with the token.
        big = urllib.request.Request(
            base + "/agentfs/big.bin", data=b"x" * 2048, method="PUT", headers=auth
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(big, timeout=5)  # noqa: S310
        assert excinfo.value.code == 413

        # DELETE with the token removes it.
        rm = urllib.request.Request(
            base + "/agentfs/demos/fluid_sim.py", method="DELETE", headers=auth
        )
        with urllib.request.urlopen(rm, timeout=5) as resp:  # noqa: S310
            assert resp.status == 204
        assert not (root / "demos" / "fluid_sim.py").exists()
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
