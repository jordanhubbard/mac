"""``ACPExecutor`` -- standalone adapter that runs one ACP agent prompt turn.

This is the seam that ``task_executor`` will sit behind once
``MAC_EXECUTOR_BACKEND=acp`` is wired up (the Phase-1 integration follow-up).
For Phase 0/1-core it is a self-contained, independently testable adapter: give
it an agent command (argv) and a prompt, and it

1. spawns the agent over stdio,
2. runs ``initialize`` -> ``session/new`` -> ``session/prompt``,
3. forwards every ``session/update`` notification to an ``on_update`` callback
   (the future ``/action-events`` sink), and
4. returns an :class:`ACPRunResult` carrying the final stop reason.

It deliberately does **not** import or touch ``task_executor``, ``api``,
``services``, or any other existing module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .client import ACPClient
from .peer import Peer, stdio_peer
from .protocol import (
    ClientCapabilities,
    InitializeResult,
    RequestPermissionParams,
    RequestPermissionResult,
)


__all__ = ["ACPExecutor", "ACPRunResult", "OnUpdate", "OnPermission"]


#: The action-events sink: receives each raw ``session/update`` params dict.
OnUpdate = Callable[[Dict[str, Any]], None]

#: Optional permission policy; defaults to "cancel" (deny) when unset.
OnPermission = Callable[[RequestPermissionParams], RequestPermissionResult]


@dataclass
class ACPRunResult:
    """Outcome of a single :meth:`ACPExecutor.run` invocation."""

    session_id: str
    stop_reason: str
    initialize: InitializeResult
    updates: List[Dict[str, Any]] = field(default_factory=list)


class ACPExecutor:
    """Run a prompt against an ACP agent, streaming updates to a callback.

    Parameters
    ----------
    argv:
        The agent command to spawn (e.g. ``["claude-code", "acp"]``). Ignored
        when ``peer_factory`` is supplied (tests inject an in-memory peer).
    cwd:
        Working directory advertised to the agent in ``session/new``.
    peer_factory:
        Optional hook returning a ready :class:`Peer` given a ``send`` callable.
        Used to inject a paired in-memory transport in tests; when ``None`` the
        executor spawns ``argv`` over stdio via :func:`stdio_peer`.
    """

    def __init__(
        self,
        argv: List[str],
        *,
        cwd: str = ".",
        client_capabilities: Optional[ClientCapabilities] = None,
        client_info: Optional[Dict[str, Any]] = None,
        peer_factory: Optional[Callable[[], Peer]] = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._client_capabilities = client_capabilities
        self._client_info = client_info
        self._peer_factory = peer_factory

    def run(
        self,
        prompt: Any,
        *,
        on_update: Optional[OnUpdate] = None,
        on_permission: Optional[OnPermission] = None,
        timeout: Optional[float] = None,
    ) -> ACPRunResult:
        """Execute one prompt turn end-to-end and return the result.

        Every ``session/update`` is appended to :attr:`ACPRunResult.updates` and
        also forwarded to ``on_update`` if provided. Blocks until the agent
        returns a stop reason (or ``timeout`` elapses).
        """

        peer, closer = self._build_peer()
        try:
            client = ACPClient(
                peer,
                client_capabilities=self._client_capabilities,
                client_info=self._client_info,
            )
            collected: List[Dict[str, Any]] = []

            def _sink(update: Dict[str, Any]) -> None:
                collected.append(update)
                if on_update is not None:
                    on_update(update)

            client.on_update(_sink)
            if on_permission is not None:
                client.on_request_permission(on_permission)

            init = client.initialize(timeout=timeout)
            session_id = client.session_new(self._cwd, timeout=timeout)
            prompt_result = client.session_prompt(
                session_id, prompt, timeout=timeout
            )
            return ACPRunResult(
                session_id=session_id,
                stop_reason=prompt_result.stop_reason,
                initialize=init,
                updates=collected,
            )
        finally:
            if closer is not None:
                closer()

    def _build_peer(self):
        """Return ``(peer, closer)`` where ``closer`` (if any) tears it down."""

        if self._peer_factory is not None:
            return self._peer_factory(), None
        stdio = stdio_peer(self._argv)
        return stdio, stdio.close
