from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


DEFAULT_PUBLIC_PREFIX = "/artifacts/"
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024


def _normalize_prefix(raw: str) -> str:
    prefix = (raw or DEFAULT_PUBLIC_PREFIX).strip()
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


class WebDAVServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        root: Path,
        public_prefix: str,
        max_upload_bytes: int,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.root = root.resolve()
        self.public_prefix = _normalize_prefix(public_prefix)
        self.max_upload_bytes = max_upload_bytes


class WebDAVHandler(BaseHTTPRequestHandler):
    server: WebDAVServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            "%s %s - %s"
            % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), self.address_string(), fmt % args),
            flush=True,
        )

    def _send_json(self, status: HTTPStatus, body: dict[str, object]) -> None:
        payload = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_status(self, status: HTTPStatus, message: str = "") -> None:
        self.send_response(status.value)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        payload = (message or status.phrase).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _target_path(self) -> Optional[Path]:
        parsed = urllib.parse.urlsplit(self.path)
        request_path = urllib.parse.unquote(parsed.path)
        if request_path == "/health":
            return None
        prefix = self.server.public_prefix
        if not request_path.startswith(prefix):
            self._send_status(HTTPStatus.NOT_FOUND)
            return None
        relative = request_path[len(prefix) :]
        if not relative or relative.endswith("/"):
            self._send_status(HTTPStatus.FORBIDDEN, "directory listing is disabled")
            return None
        candidate = (self.server.root / relative).resolve()
        try:
            candidate.relative_to(self.server.root)
        except ValueError:
            self._send_status(HTTPStatus.FORBIDDEN, "path escapes artifact root")
            return None
        return candidate

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND")
        self.send_header("DAV", "1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._send_file(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        if urllib.parse.urlsplit(self.path).path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "schema": "mac.webdav.health.v1",
                    "public_prefix": self.server.public_prefix,
                },
            )
            return
        self._send_file(head_only=False)

    def _send_file(self, *, head_only: bool) -> None:
        target = self._target_path()
        if target is None:
            return
        if not target.is_file():
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        stat = target.stat()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        if not head_only:
            with target.open("rb") as fh:
                shutil.copyfileobj(fh, self.wfile)

    def do_PUT(self) -> None:  # noqa: N802
        self._send_status(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "HTTP writes are disabled; publish by writing to MAC_PUBLISH_DIR on the hub.",
        )

    def do_MKCOL(self) -> None:  # noqa: N802
        self._send_status(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "HTTP writes are disabled; publish by writing to MAC_PUBLISH_DIR on the hub.",
        )

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_status(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "HTTP deletes are disabled; delete publish records through MAC/AgentBus.",
        )

    def do_PROPFIND(self) -> None:  # noqa: N802
        target = self._target_path()
        if target is None:
            return
        exists = target.exists()
        href = urllib.parse.quote(urllib.parse.urlsplit(self.path).path)
        content_length = target.stat().st_size if exists and target.is_file() else 0
        status = "HTTP/1.1 200 OK" if exists else "HTTP/1.1 404 Not Found"
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:">'
            "<D:response>"
            f"<D:href>{href}</D:href>"
            "<D:propstat>"
            "<D:prop>"
            f"<D:getcontentlength>{content_length}</D:getcontentlength>"
            "</D:prop>"
            f"<D:status>{status}</D:status>"
            "</D:propstat>"
            "</D:response>"
            "</D:multistatus>"
        ).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Serve public-read MAC artifacts from a hub publish directory.")
    parser.add_argument("--host", default=os.environ.get("MAC_WEBDAV_BIND_ADDR", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MAC_WEBDAV_PORT", "80")))
    parser.add_argument("--root", default=os.environ.get("MAC_WEBDAV_ROOT", str(Path.home() / ".mac" / "public-artifacts")))
    parser.add_argument("--public-prefix", default=os.environ.get("MAC_WEBDAV_PUBLIC_PATH", DEFAULT_PUBLIC_PREFIX))
    parser.add_argument(
        "--max-upload-bytes",
        type=int,
        default=int(os.environ.get("MAC_WEBDAV_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
    )
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    server = WebDAVServer(
        (args.host, args.port),
        WebDAVHandler,
        root=root,
        public_prefix=args.public_prefix,
        max_upload_bytes=args.max_upload_bytes,
    )
    print(
        "mac-webdav-server listening on %s:%d root=%s prefix=%s"
        % (args.host, args.port, root, _normalize_prefix(args.public_prefix)),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
