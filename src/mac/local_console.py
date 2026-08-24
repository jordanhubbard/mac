"""Kernel-authenticated, hub-local client enrollment over a Unix socket."""

from __future__ import annotations

import errno
import grp
import json
import logging
import os
import pwd
import socket
import stat
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from mac.client_principals import (
    ELEVATED_SCOPES,
    KNOWN_SCOPES,
    ClientPrincipalError,
    ClientPrincipalStore,
    enrollment_manifest,
)


DEFAULT_SOCKET_PATH = "~/.mac/local-console.sock"
DEFAULT_SCOPES = frozenset({"read", "write", "dispatch"})
MAX_FRAME_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
_REQUEST_KEYS = frozenset(
    {
        "action",
        "client_id",
        "display_name",
        "fleet",
        "profile",
        "scopes",
        "capabilities",
        "expires_in",
        "api_url",
        "allow_elevated",
        "rotate",
    }
)
_LOG = logging.getLogger("mac.local_console")


class LocalConsoleError(RuntimeError):
    """A secret-safe local-console transport or enrollment failure."""


@dataclass(frozen=True)
class PeerIdentity:
    uid: int
    gid: int
    pid: Optional[int] = None

    @property
    def username(self) -> str:
        try:
            value = pwd.getpwuid(self.uid).pw_name
        except KeyError:
            return "unknown"
        return "".join(ch for ch in value if ch.isalnum() or ch in "._-")[:64] or "unknown"


def configured_socket_path(value: Optional[str] = None) -> Path:
    raw = value or os.environ.get("MAC_LOCAL_CONSOLE_SOCKET") or DEFAULT_SOCKET_PATH
    return Path(raw).expanduser()


def default_api_url() -> str:
    for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value.rstrip("/")
    raw_port = (os.environ.get("MAC_PORT") or "8789").strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise LocalConsoleError("MAC_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise LocalConsoleError("MAC_PORT must be between 1 and 65535")
    return "http://127.0.0.1:%d" % port


def _json_without_duplicates(raw: bytes) -> Any:
    def pairs(items: Iterable[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _receive_frame(conn: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while True:
        chunk = conn.recv(min(4096, limit + 1 - len(data)))
        if not chunk:
            raise LocalConsoleError("connection closed before a complete request")
        data.extend(chunk)
        if len(data) > limit:
            raise LocalConsoleError("request exceeds the local-console size limit")
        newline = data.find(b"\n")
        if newline >= 0:
            if data[newline + 1 :].strip():
                raise LocalConsoleError("only one local-console request is allowed")
            return bytes(data[:newline])


def _peer_identity(conn: socket.socket) -> PeerIdentity:
    # BSD/macOS exposes getpeereid(); prefer it there. Some platforms also
    # define SO_PEERCRED with a non-Linux payload, so testing the constant first
    # and unpacking Linux's three integers can misidentify the peer.
    getter = getattr(conn, "getpeereid", None)
    if callable(getter):
        uid, gid = getter()
        return PeerIdentity(uid=int(uid), gid=int(gid))
    if hasattr(socket, "SO_PEERCRED"):
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return PeerIdentity(uid=uid, gid=gid, pid=pid)
    # CPython doesn't expose socket.getpeereid() on every macOS build even
    # though libc and the kernel provide it. Calling libc still retrieves the
    # credentials attached to this connected socket; no process-supplied data
    # participates.
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        if getpeereid(conn.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return PeerIdentity(uid=int(uid.value), gid=int(gid.value))
    except (AttributeError, OSError):
        pass
    raise LocalConsoleError("kernel peer credentials are unavailable on this platform")


def _group_gid(name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    try:
        return int(grp.getgrnam(name).gr_gid)
    except KeyError as exc:
        raise LocalConsoleError("configured local-console group does not exist") from exc


def _peer_in_group(peer: PeerIdentity, gid: int) -> bool:
    if peer.gid == gid:
        return True
    try:
        account = pwd.getpwuid(peer.uid)
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    return account.pw_gid == gid or account.pw_name in group.gr_mem


def _validate_api_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LocalConsoleError("api_url must be an http or https URL")
    if parsed.username or parsed.password:
        raise LocalConsoleError("api_url must not contain credentials")
    return url


def _string_list(value: Any, *, field: str, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise LocalConsoleError("%s must be a bounded JSON array" % field)
    result = []
    for item in value:
        text = str(item or "").strip()
        if not text or len(text) > 256 or any(ord(ch) < 32 for ch in text):
            raise LocalConsoleError("%s contains an invalid value" % field)
        result.append(text)
    return result


class LocalConsoleService:
    """Single-request JSON enrollment service authenticated by peer credentials."""

    def __init__(
        self,
        socket_path: Path,
        principal_store: ClientPrincipalStore,
        *,
        allowed_group: Optional[str] = None,
        service_uid: Optional[int] = None,
        request_timeout: float = 5.0,
    ) -> None:
        self.path = Path(socket_path)
        self.store = principal_store
        self.service_uid = os.geteuid() if service_uid is None else int(service_uid)
        self.allowed_group = allowed_group or None
        self.allowed_gid = _group_gid(self.allowed_group)
        self.request_timeout = max(0.1, float(request_timeout))
        self._listener: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _prepare_parent(self) -> None:
        parent = self.path.parent
        existed = parent.exists()
        if existed and (parent.is_symlink() or not parent.is_dir()):
            raise LocalConsoleError("local-console socket parent is not a directory")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = parent.stat()
        if current.st_uid not in {0, self.service_uid}:
            raise LocalConsoleError("local-console socket parent has an unsafe owner")
        if existed and current.st_mode & 0o007:
            # Never "repair" /tmp, /run, or another shared directory in place,
            # especially when the API runs as root. Require a dedicated private
            # parent instead of changing unrelated filesystem policy.
            raise LocalConsoleError(
                "local-console socket parent must not be accessible to other users"
            )
        mode = 0o750 if self.allowed_gid is not None else 0o700
        if self.allowed_gid is not None and current.st_gid != self.allowed_gid:
            try:
                os.chown(parent, -1, self.allowed_gid)
            except OSError as exc:
                raise LocalConsoleError(
                    "could not assign the configured local-console group to the socket directory"
                ) from exc
        if (current.st_mode & 0o777) != mode:
            try:
                parent.chmod(mode)
            except OSError as exc:
                raise LocalConsoleError(
                    "could not enforce local-console socket directory permissions"
                ) from exc

    def start(self) -> None:
        if self._listener is not None:
            return
        self._prepare_parent()
        if os.path.lexists(self.path):
            current = self.path.lstat()
            if not stat.S_ISSOCK(current.st_mode) or current.st_uid != self.service_uid:
                raise LocalConsoleError("refusing to replace an unsafe local-console socket path")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            try:
                probe.connect(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            except OSError as exc:
                raise LocalConsoleError(
                    "could not verify the existing local-console socket"
                ) from exc
            else:
                raise LocalConsoleError("another local-console service is already active")
            finally:
                probe.close()
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            if self.allowed_gid is not None:
                os.chown(self.path, -1, self.allowed_gid)
            self.path.chmod(0o660 if self.allowed_gid is not None else 0o600)
            listener.listen(8)
            listener.settimeout(0.2)
        except Exception:
            listener.close()
            self.path.unlink(missing_ok=True)
            raise
        self._stop.clear()
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="mac-local-console", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        try:
            if self.path.is_socket() and self.path.lstat().st_uid == self.service_uid:
                self.path.unlink()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with conn:
                conn.settimeout(self.request_timeout)
                self._handle_connection(conn)

    def _authorized(self, peer: PeerIdentity) -> bool:
        return peer.uid in {0, self.service_uid} or (
            self.allowed_gid is not None and _peer_in_group(peer, self.allowed_gid)
        )

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            peer = _peer_identity(conn)
            if not self._authorized(peer):
                raise PermissionError("local-console peer is not authorized")
            request = _json_without_duplicates(_receive_frame(conn, MAX_FRAME_BYTES))
            response = {"ok": True, "manifest": self._dispatch(peer, request)}
        except PermissionError:
            response = {"ok": False, "error": "access denied by local-console policy"}
        except (ClientPrincipalError, LocalConsoleError, ValueError, TypeError):
            _LOG.warning("local-console request rejected", exc_info=True)
            response = {"ok": False, "error": "local-console request was rejected"}
        except Exception:
            _LOG.error("local-console enrollment failed", exc_info=True)
            response = {"ok": False, "error": "local-console enrollment failed"}
        raw = (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_RESPONSE_BYTES:
            raw = b'{"ok":false,"error":"local-console enrollment failed"}\n'
        try:
            conn.sendall(raw)
        except OSError:
            pass

    def _dispatch(self, peer: PeerIdentity, request: Any) -> Mapping[str, Any]:
        if not isinstance(request, dict) or set(request) - _REQUEST_KEYS:
            raise LocalConsoleError("request is not a supported object")
        action = request.get("action")
        client_id = str(request.get("client_id") or "")
        actor = "local-console:uid=%d:user=%s" % (peer.uid, peer.username)
        owner = {"enrollment_channel": "local-console", "local_uid": peer.uid}
        if action == "revoke":
            self.store.revoke(client_id, required_metadata=owner, actor=actor)
            return {"revoked": True}
        if action == "renew":
            acknowledged = request.get("allow_elevated") is True
            elevated_authority = peer.uid in {0, self.service_uid}
            issued = self.store.renew(
                client_id,
                expires_in=int(request.get("expires_in") or 0),
                allowed_scopes=(
                    KNOWN_SCOPES if elevated_authority and acknowledged else DEFAULT_SCOPES
                ),
                required_metadata=owner,
                actor=actor,
            )
            return enrollment_manifest(issued)
        if action != "enroll":
            raise LocalConsoleError("unsupported local-console action")
        scopes = _string_list(request.get("scopes"), field="scopes")
        privileged = set(scopes) - DEFAULT_SCOPES
        acknowledged = request.get("allow_elevated") is True
        if privileged and (peer.uid not in {0, self.service_uid} or not acknowledged):
            raise PermissionError("elevated local-console scopes require the service owner or root")
        if set(scopes) & ELEVATED_SCOPES and not acknowledged:
            raise PermissionError("elevated local-console scopes require acknowledgement")
        capabilities = _string_list(request.get("capabilities") or [], field="capabilities")
        issued = self.store.enroll(
            client_id,
            display_name=str(request.get("display_name") or client_id),
            fleet=str(request.get("fleet") or ""),
            profile=str(request.get("profile") or client_id),
            scopes=scopes,
            capabilities=capabilities,
            expires_in=int(request.get("expires_in") or 0),
            api_url=_validate_api_url(request.get("api_url")),
            allow_elevated=acknowledged,
            rotate=request.get("rotate") is True,
            actor=actor,
            credential_metadata={
                **owner,
                "local_username": peer.username,
            },
            required_existing_metadata=owner,
        )
        return enrollment_manifest(issued)


def request_local_console(
    payload: Mapping[str, Any],
    *,
    socket_path: Optional[str] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """Send one bounded request to the local enrollment service."""
    path = configured_socket_path(socket_path)
    try:
        current = path.lstat()
    except FileNotFoundError as exc:
        raise LocalConsoleError(
            "local-console socket is missing at %s; verify the MAC API service is running" % path
        ) from exc
    if not stat.S_ISSOCK(current.st_mode):
        raise LocalConsoleError("local-console path is not a Unix-domain socket")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(max(1, int(timeout)))
    try:
        conn.connect(str(path))
        raw_request = (json.dumps(dict(payload), separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw_request) > MAX_FRAME_BYTES:
            raise LocalConsoleError("local-console request exceeds the size limit")
        conn.sendall(raw_request)
        raw = _receive_frame(conn, MAX_RESPONSE_BYTES)
    except PermissionError as exc:
        raise LocalConsoleError(
            "access denied to local-console socket; use an authorized account or group"
        ) from exc
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            raise LocalConsoleError(
                "access denied to local-console socket; use an authorized account or group"
            ) from exc
        raise LocalConsoleError("could not reach the local-console socket") from exc
    finally:
        conn.close()
    try:
        response = _json_without_duplicates(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LocalConsoleError("local-console service returned malformed JSON") from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = str(response.get("error") or "") if isinstance(response, dict) else ""
        allowed = {
            "access denied by local-console policy",
            "local-console request was rejected",
            "local-console enrollment failed",
        }
        raise LocalConsoleError(detail if detail in allowed else "local-console request failed")
    manifest = response.get("manifest")
    if not isinstance(manifest, dict):
        raise LocalConsoleError("local-console service returned no enrollment manifest")
    return manifest
