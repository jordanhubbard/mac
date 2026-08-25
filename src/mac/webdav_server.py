"""Minimal WebDAV server for publishing task artifacts.

Implements the threading HTTP server and request handler that expose task
artifacts over WebDAV, including path normalization and an authenticated command
-line entry point.
"""

from __future__ import annotations

import argparse
import json
import hmac
import mimetypes
import os
import shutil
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mac import mac_paths
from mac.atomic_file import atomic_writer
from typing import Optional


DEFAULT_PUBLIC_PREFIX = "/artifacts/"
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024

#: Mode for uploaded artifacts. mkstemp creates 0600; the share is read by
#: other agents on the host, so restore the umask-default readability a plain
#: ``open(..., "wb")`` used to give these files.
_UPLOAD_MODE = 0o644


class _TruncatedUpload(Exception):
    """The client sent fewer bytes than Content-Length promised."""


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
        write_token: str = "",
    ) -> None:
        super().__init__(server_address, handler_class)
        self.root = root.resolve()
        self.public_prefix = _normalize_prefix(public_prefix)
        self.max_upload_bytes = max_upload_bytes
        # AgentFS v2 (shared fleet filesystem): when a write token is set,
        # PUT/MKCOL/DELETE are enabled for callers presenting it. Reads stay
        # open to whoever can reach the (tailnet-bound) socket, matching the
        # old SMB share's "open to the tailnet, closed to the internet"
        # posture. Empty token = legacy read-only public-artifact server.
        self.write_token = str(write_token or "")


class WebDAVHandler(BaseHTTPRequestHandler):
    server: WebDAVServer

    def log_message(self, fmt: str, *args: object) -> None:
        print(
            "%s %s - %s"
            % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                self.address_string(),
                fmt % args,
            ),
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
        # Advertise write verbs when a token is configured so Finder mounts
        # the share read-write rather than read-only.
        if self.server.write_token:
            self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND, PUT, DELETE, MKCOL")
        else:
            self.send_header("Allow", "OPTIONS, GET, HEAD, PROPFIND")
        self.send_header("DAV", "1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
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

    def _writes_authorized(self) -> bool:
        token = self.server.write_token
        if not token:
            self._send_status(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "HTTP writes are disabled on this server (no write token configured).",
            )
            return False
        header = self.headers.get("Authorization", "")
        presented = header[7:] if header.startswith("Bearer ") else ""
        if not hmac.compare_digest(presented, token):
            self._send_status(
                HTTPStatus.UNAUTHORIZED,
                "AgentFS writes require Authorization: Bearer <write token>.",
            )
            return False
        return True

    def do_PUT(self) -> None:  # noqa: N802
        if not self._writes_authorized():
            return
        target = self._target_path()
        if target is None:
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_status(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
            return
        if length < 0 or length > self.server.max_upload_bytes:
            self._send_status(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "upload exceeds %d bytes" % self.server.max_upload_bytes,
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a UNIQUELY named temp sibling then atomically rename, so a
        # concurrent reader never sees a half-written file AND two concurrent
        # PUTs to the same path cannot splice. This is a ThreadingHTTPServer in
        # front of a share many agents write: a fixed ".<name>.partial" gave
        # every in-flight PUT for one path the same temp file, so they truncated
        # and interleaved into it, one rename installed the mixture (answering
        # 201 with a byte count it had counted itself, not one it had written),
        # and the loser's rename raised FileNotFoundError -> 500.
        remaining = length
        try:
            with atomic_writer(target, binary=True, mode=_UPLOAD_MODE) as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    remaining -= len(chunk)
                if remaining != 0:
                    raise _TruncatedUpload()
        except _TruncatedUpload:
            self._send_status(HTTPStatus.BAD_REQUEST, "truncated upload")
            return
        except OSError as exc:
            self._send_status(HTTPStatus.INTERNAL_SERVER_ERROR, "write failed: %s" % exc)
            return
        self._send_status(HTTPStatus.CREATED, "stored %d bytes" % length)

    def do_MKCOL(self) -> None:  # noqa: N802
        if not self._writes_authorized():
            return
        parsed = urllib.parse.urlsplit(self.path)
        request_path = urllib.parse.unquote(parsed.path)
        prefix = self.server.public_prefix
        if not request_path.startswith(prefix):
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        relative = request_path[len(prefix) :].strip("/")
        candidate = (self.server.root / relative).resolve()
        try:
            candidate.relative_to(self.server.root)
        except ValueError:
            self._send_status(HTTPStatus.FORBIDDEN, "path escapes agentfs root")
            return
        candidate.mkdir(parents=True, exist_ok=True)
        self._send_status(HTTPStatus.CREATED, "collection created")

    def do_DELETE(self) -> None:  # noqa: N802
        if not self._writes_authorized():
            return
        target = self._target_path()
        if target is None:
            return
        # is_file() followed by unlink() is a TOCTOU: on a share written by many
        # agents the file can vanish (or be replaced by a PUT's rename) between
        # the two calls, turning a benign concurrent delete into an unhandled
        # FileNotFoundError and a 500. Attempt the unlink and interpret failure.
        if target.is_dir():
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        try:
            target.unlink()
        except FileNotFoundError:
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        except IsADirectoryError:
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        except OSError as exc:
            self._send_status(HTTPStatus.INTERNAL_SERVER_ERROR, "delete failed: %s" % exc)
            return
        self._send_status(HTTPStatus.NO_CONTENT)

    def _propfind_response(self, href: str, target: Path) -> str:
        is_dir = target.is_dir()
        resourcetype = "<D:collection/>" if is_dir else ""
        length = 0 if is_dir else target.stat().st_size
        modified = self.date_time_string(target.stat().st_mtime)
        return (
            "<D:response>"
            f"<D:href>{href}</D:href>"
            "<D:propstat>"
            "<D:prop>"
            f"<D:resourcetype>{resourcetype}</D:resourcetype>"
            f"<D:getcontentlength>{length}</D:getcontentlength>"
            f"<D:getlastmodified>{modified}</D:getlastmodified>"
            "</D:prop>"
            "<D:status>HTTP/1.1 200 OK</D:status>"
            "</D:propstat>"
            "</D:response>"
        )

    def do_PROPFIND(self) -> None:  # noqa: N802
        # Directory-aware PROPFIND so Finder (and any WebDAV client) can mount
        # and browse the share. Depth: 1 lists a collection's children.
        parsed = urllib.parse.urlsplit(self.path)
        request_path = urllib.parse.unquote(parsed.path)
        prefix = self.server.public_prefix
        if not request_path.startswith(prefix):
            self._send_status(HTTPStatus.NOT_FOUND)
            return
        relative = request_path[len(prefix) :].strip("/")
        target = (self.server.root / relative).resolve() if relative else self.server.root
        try:
            target.relative_to(self.server.root)
        except ValueError:
            self._send_status(HTTPStatus.FORBIDDEN, "path escapes agentfs root")
            return
        if not target.exists():
            body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<D:multistatus xmlns:D="DAV:"><D:response>'
                f"<D:href>{urllib.parse.quote(request_path)}</D:href>"
                "<D:status>HTTP/1.1 404 Not Found</D:status>"
                "</D:response></D:multistatus>"
            ).encode("utf-8")
            self.send_response(207)
            self.send_header("Content-Type", 'application/xml; charset="utf-8"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        base_href = urllib.parse.quote(request_path)
        depth = self.headers.get("Depth", "1")
        responses = [self._propfind_response(base_href, target)]
        if depth != "0" and target.is_dir():
            base = base_href if base_href.endswith("/") else base_href + "/"
            for child in sorted(target.iterdir()):
                if child.name.startswith("."):
                    continue  # hide .partial temp files and dotfiles
                child_href = base + urllib.parse.quote(child.name) + ("/" if child.is_dir() else "")
                responses.append(self._propfind_response(child_href, child))
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>"
        ).encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", 'application/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve public-read MAC artifacts from a hub publish directory."
    )
    parser.add_argument("--host", default=os.environ.get("MAC_WEBDAV_BIND_ADDR", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MAC_WEBDAV_PORT", "80")))
    parser.add_argument(
        "--root",
        default=os.environ.get("MAC_WEBDAV_ROOT", str(mac_paths.mac_home() / "public-artifacts")),
    )
    parser.add_argument(
        "--public-prefix", default=os.environ.get("MAC_WEBDAV_PUBLIC_PATH", DEFAULT_PUBLIC_PREFIX)
    )
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
        write_token=os.environ.get("MAC_WEBDAV_WRITE_TOKEN", ""),
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
