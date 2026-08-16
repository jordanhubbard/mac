"""mac's A2A AgentCard builder (inbound A2A federation, ACP roadmap Phase 4).

The AgentCard is the A2A "business card": a small JSON document served at the
well-known discovery path that lets an *external* A2A client discover this
agent's identity, service endpoint, capabilities, and skills. See the A2A
specification (https://a2a-protocol.org, Linux Foundation; absorbed IBM's
Agent Communication Protocol) -- this module pins mac's card shape.

Spec notes pinned by this implementation (verified against
``a2a-protocol.org`` / the agent-discovery topic on 2026-06-17):

* The canonical well-known path is ``/.well-known/agent-card.json`` (A2A
  v0.3+). Earlier drafts used ``/.well-known/agent.json``; api.py serves both
  (the latter as a legacy alias) so older clients still resolve.
* Property names are ``camelCase`` (``defaultInputModes`` etc.).
* In the JSON-RPC binding, a text :class:`~mac.a2a.protocol.Part` is
  ``{"kind": "text", "text": ...}`` and ``TaskState`` values are the
  lowercase-hyphenated strings (``submitted`` / ``working`` /
  ``input-required`` / ...). This module only emits identity/capability data;
  the wire types live in :mod:`mac.a2a.protocol`.

This module performs **no I/O** -- it is pure data. ``base_url`` is the only
input, supplied by the HTTP layer from the inbound request so the advertised
``url`` matches however the caller reached mac.
"""

from __future__ import annotations

from typing import Any, Dict, List

from mac import __version__ as _mac_version


__all__ = [
    "A2A_PROTOCOL_VERSION",
    "MAC_VERSION",
    "A2A_ENDPOINT_PATH",
    "WELL_KNOWN_CARD_PATH",
    "WELL_KNOWN_CARD_PATH_LEGACY",
    "DEFAULT_INPUT_MODES",
    "DEFAULT_OUTPUT_MODES",
    "mac_skills",
    "agent_card",
]


#: The A2A protocol version mac's card declares. A2A versions its spec with a
#: ``MAJOR.MINOR.PATCH`` string (unlike ACP's single integer), so this is a str.
A2A_PROTOCOL_VERSION: str = "0.3.0"

#: mac's own software version, surfaced as the card's ``version``. Imported
#: from the package so it cannot drift from ``pyproject.toml`` or the FastAPI
#: app version in :func:`mac.api.create_app`.
MAC_VERSION: str = _mac_version

#: The path, relative to the card's ``base_url``, where mac serves the A2A
#: JSON-RPC endpoint (see ``POST /a2a`` in api.py).
A2A_ENDPOINT_PATH: str = "/a2a"

#: Canonical A2A discovery path (v0.3+).
WELL_KNOWN_CARD_PATH: str = "/.well-known/agent-card.json"

#: Legacy discovery path (pre-v0.3 drafts). Served as an alias for old clients.
WELL_KNOWN_CARD_PATH_LEGACY: str = "/.well-known/agent.json"

#: Content the agent accepts / produces by default. mac's task ledger is
#: text-driven in this phase (title + description), so plain text only.
DEFAULT_INPUT_MODES: List[str] = ["text/plain"]
DEFAULT_OUTPUT_MODES: List[str] = ["text/plain"]


def mac_skills() -> List[Dict[str, Any]]:
    """The :class:`AgentSkill` list mac advertises.

    A mac agent is a general software-task executor backed by the fleet task
    ledger: an A2A peer delegates a unit of work (a message describing a task)
    and mac runs it through the same create -> dispatch -> review -> complete
    lifecycle as any locally-filed task. We advertise a single broad skill for
    that surface in this phase; per-capability skills can be derived from the
    fleet's role/capability catalog in a later phase.

    AgentSkill fields per the A2A spec: ``id``, ``name``, ``description``,
    ``tags``, ``examples``, plus optional ``inputModes`` / ``outputModes``
    (omitted here so the card's defaults apply).
    """

    return [
        {
            "id": "software-task",
            "name": "General agent task",
            "description": (
                "Delegate a software / general agent task to the mac fleet. "
                "The message text becomes a task in mac's ledger, which is "
                "dispatched to a capable agent, executed, reviewed, and "
                "driven to a terminal state."
            ),
            "tags": ["software", "agent", "task", "mac", "fleet"],
            "examples": [
                "Fix the failing test in module X and open a PR.",
                "Investigate why the nightly job is timing out.",
                "Summarize the open issues in the repo.",
            ],
        }
    ]


def agent_card(base_url: str) -> Dict[str, Any]:
    """Build mac's A2A AgentCard as a plain dict.

    ``base_url`` is the externally-visible origin the caller used to reach mac
    (scheme + host[:port], no trailing slash required). The advertised A2A
    service ``url`` is ``base_url`` + :data:`A2A_ENDPOINT_PATH` so a client that
    fetched the card can immediately address the JSON-RPC endpoint.

    Capabilities are conservative for Phase 4 (inbound, polling-based):
    ``streaming`` (``message/stream`` SSE) and ``pushNotifications`` are both
    advertised as ``false`` -- they are explicitly deferred. A client therefore
    drives long-running work by polling ``tasks/get``.
    """

    base = base_url.rstrip("/")
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "mac",
        "description": (
            "MAC multi-agent control plane. Accepts delegated tasks over A2A "
            "and runs them on its fleet via the shared task ledger."
        ),
        "url": base + A2A_ENDPOINT_PATH,
        "version": MAC_VERSION,
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
        },
        "defaultInputModes": list(DEFAULT_INPUT_MODES),
        "defaultOutputModes": list(DEFAULT_OUTPUT_MODES),
        "skills": mac_skills(),
    }
