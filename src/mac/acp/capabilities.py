"""mac's ACP capability set, builders, and the well-known manifest (Phase 3).

The :mod:`mac.acp.protocol` module defines the *wire shape* of capabilities
(:class:`~mac.acp.protocol.ClientCapabilities` /
:class:`~mac.acp.protocol.AgentCapabilities`). This module pins **mac's actual
values** for them and adds mac-specific extensions under a ``_meta`` key.

ACP permits vendor extension through a ``_meta`` field on any object (see
``agentclientprotocol.com/protocol/v1/extensibility``): the keys there are
ignored by implementations that don't understand them, so mac advertises its
own feature flags additively without breaking baseline interop. mac groups all
of its extensions under ``_meta.mac`` so a peer can detect "this is a mac
endpoint" with a single key check::

    {"mac": {"sandbox": true, "decomposition": true, "evidence": true}}

* ``sandbox`` -- runs agents under the OpenShell kernel sandbox (the real
  permission gate; see :mod:`mac.acp.permission`).
* ``decomposition`` -- can auto-split an over-scoped task into child tasks.
* ``evidence`` -- finalizes every run with a deterministic, typed evidence
  manifest (the correctness proof, independent of the agent's self-report).

:func:`acp_manifest` produces the ``GET /.well-known/acp`` discovery document.
This module performs **no I/O** -- it is pure data plus the dict builders.
"""

from __future__ import annotations

from typing import Any, Dict

from .protocol import (
    PROTOCOL_VERSION,
    AgentCapabilities,
    ClientCapabilities,
)


__all__ = [
    "MAC_EXTENSIONS",
    "mac_meta",
    "mac_client_capabilities",
    "mac_agent_capabilities",
    "acp_manifest",
]


#: mac's advertised feature flags. Additive and ignorable by other ACP
#: implementations (they live under the vendor ``_meta`` field).
MAC_EXTENSIONS: Dict[str, bool] = {
    "sandbox": True,
    "decomposition": True,
    "evidence": True,
}


def mac_meta() -> Dict[str, Any]:
    """The ``_meta`` block mac attaches to its capability/manifest objects.

    A fresh dict each call so callers can mutate the result without leaking
    state back into :data:`MAC_EXTENSIONS`.
    """

    return {"mac": dict(MAC_EXTENSIONS)}


def mac_client_capabilities() -> ClientCapabilities:
    """The :class:`ClientCapabilities` mac advertises when it *drives* an agent.

    mac is the host/client: it does not yet expose filesystem or terminal
    helpers to the driven agent (those would let the agent reach back into the
    host), so the baseline ACP capability fields stay off. The mac-specific
    extensions ride in ``_meta`` instead.
    """

    return ClientCapabilities(
        fs_read_text_file=False,
        fs_write_text_file=False,
        terminal=False,
        meta=mac_meta(),
    )


def mac_agent_capabilities() -> AgentCapabilities:
    """The :class:`AgentCapabilities` a mac agent advertises when *driven*.

    mac does not yet rehydrate prior sessions (``loadSession`` off; Phase-2
    note) but accepts the common prompt content types and surfaces its
    extensions under ``_meta``.
    """

    return AgentCapabilities(
        load_session=False,
        prompt_capabilities={"image": False, "audio": False, "embeddedContext": True},
        mcp_capabilities={"http": False},
        meta=mac_meta(),
    )


def acp_manifest() -> Dict[str, Any]:
    """The well-known ACP discovery manifest served at ``GET /.well-known/acp``.

    Shape mirrors the ``initialize`` result so a client can pre-flight a mac
    endpoint without opening a session::

        {
          "protocolVersion": 1,
          "agentCapabilities": {...},
          "authMethods": [...],
          "agentInfo": {...},
          "_meta": {"mac": {...}}
        }

    ``authMethods`` is empty here: mac's HTTP front door authenticates with the
    existing bearer-token ``TokenPrincipal`` (see ``api.py``), so a standalone
    ACP ``authenticate`` method is not advertised at the discovery layer.
    """

    return {
        "protocolVersion": PROTOCOL_VERSION,
        "agentCapabilities": mac_agent_capabilities().to_dict(),
        "authMethods": [],
        "agentInfo": {"name": "mac", "title": "MAC agent", "version": "0"},
        "_meta": mac_meta(),
    }
