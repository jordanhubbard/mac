"""Inbound A2A (Agent2Agent) federation for mac (ACP roadmap Phase 4).

A2A (https://a2a-protocol.org, Linux Foundation; absorbed IBM's "Agent
Communication Protocol") is the open JSON-RPC 2.0 standard for **agent <->
agent** delegation. This package lets an *external* A2A agent discover mac via
its AgentCard and delegate work to it; the work lands on mac's existing task
ledger (no parallel store).

This is the agent<->agent axis -- distinct from :mod:`mac.acp`, which is the
host<->agent runtime seam. Layers:

* :mod:`mac.a2a.card` -- the AgentCard discovery document (pure data).
* :mod:`mac.a2a.protocol` -- JSON-RPC 2.0 envelope + A2A wire types
  (Message / Part / Task / TaskState); pure.
* :mod:`mac.a2a.service` -- :class:`~mac.a2a.service.A2AService`, mapping the
  A2A RPCs (``message/send`` / ``tasks/get`` / ``tasks/cancel``) onto the mac
  control plane (inbound).
* :mod:`mac.a2a.client` -- :class:`~mac.a2a.client.A2AClient`, the reciprocal
  *outbound* client: discover a remote A2A agent and delegate work to it.

The HTTP wiring (``GET /.well-known/agent-card.json`` + ``POST /a2a``) lives in
:mod:`mac.api`.

Deferred (out of scope this phase): ``message/stream`` SSE streaming and push
notifications. Both the inbound service and the outbound client poll
``tasks/get`` for long-running work.
"""

from __future__ import annotations

from .card import agent_card
from .client import A2AClient, A2AClientError
from .protocol import Message, Task, TaskState
from .service import A2AService

__all__ = [
    "agent_card",
    "A2AService",
    "A2AClient",
    "A2AClientError",
    "Message",
    "Task",
    "TaskState",
]
