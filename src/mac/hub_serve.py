"""Serve the hub HTTP API on one or more bind addresses.

Uvicorn's CLI accepts a single ``--host``. Mesh fleets need loopback (health
checks, local CLI) plus the Tailscale address and must refuse ``0.0.0.0``.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional, Sequence

from mac.mesh_bind import (
    MeshBindError,
    bind_sockets,
    format_bind_hosts,
    serve_bind_hosts,
)


def _int_port(raw: str, default: int = 8789) -> int:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError as exc:
        raise MeshBindError("MAC_PORT must be an integer, not %r" % raw) from exc
    if not 1 <= value <= 65535:
        raise MeshBindError("MAC_PORT out of range: %s" % value)
    return value


def bind_hosts_from_env(environ: Optional[Mapping[str, str]] = None) -> Sequence[str]:
    env = os.environ if environ is None else environ
    role = str(env.get("MAC_CONTROL_PLANE_ROLE") or "").strip().lower()
    return serve_bind_hosts(
        str(env.get("MAC_BIND_HOST") or "127.0.0.1"),
        network_provider=str(env.get("MAC_NETWORK_PROVIDER") or ""),
        is_hub=role != "client",
        environ=env,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    del argv
    try:
        hosts = list(bind_hosts_from_env())
        os.environ["MAC_BIND_HOST"] = format_bind_hosts(hosts)
        port = _int_port(os.environ.get("MAC_PORT") or "8789")
        sockets = bind_sockets(hosts, port)
    except MeshBindError as exc:
        sys.stderr.write("ERROR: %s\n" % exc)
        return 2
    except OSError as exc:
        sys.stderr.write("ERROR: hub bind failed: %s\n" % exc)
        return 2
    try:
        import uvicorn

        config = uvicorn.Config(
            "mac.api:create_app",
            factory=True,
            workers=1,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.run(sockets=sockets)
    finally:
        for sock in sockets:
            sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
