"""Reflect-request handler extracted from worker.py.

Contains:
  - ReflectMixin: mixin that provides _handle_reflect_request_stream,
    _run_reflect_query, _reflect_runtime_query, and _publish_reflect_result
    to MacWorker

These are imported back into worker.py; callers that import from mac.worker
see no change.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote, urlencode

from mac.agentbus_control import (
    REFLECT_REQUEST_CONTENT_TYPE,
    REFLECT_RESULT_CONTENT_TYPE,
    REFLECT_RESULT_TOPIC,
    reflect_result_payload,
)

JsonDict = Dict[str, Any]


def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def hermes_oneshot_command(prompt: str) -> List[str]:
    """argv for one non-interactive turn of this host's Hermes runtime.

    ``hermes --oneshot PROMPT`` runs a single turn and prints the reply. The
    launcher is the one the Hermes installer owns (``~/.local/bin/hermes``);
    ``MAC_HERMES_BIN`` overrides it. The prompt goes straight through argv,
    which no shell interprets, so multi-line text needs no escaping.
    """
    binary = os.environ.get("MAC_HERMES_BIN") or str(Path.home() / ".local" / "bin" / "hermes")
    return [binary, "--oneshot", prompt]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


class ReflectMixin:
    """Mixin that provides the reflect-request subsystem to MacWorker.

    Relies on the following attributes being set by MacWorker.__init__:
      self.client, self.agent_id
    """

    def _handle_reflect_request_stream(self, stream: JsonDict) -> JsonDict:
        """Dispatch a reflect request to this agent's Hermes runtime.

        The request payload may include a *query* field.  A bounded subprocess
        call is made so a slow LLM response cannot block the poll loop.  The
        result (truncated to 300 words) is published back to the sender as a
        REFLECT_RESULT_CONTENT_TYPE chunk on the agentbus.
        """
        stream_id = str(stream.get("id") or "")
        chunks = self.client.get(
            "/agentbus/streams/%s/chunks?%s"
            % (
                quote(stream_id, safe=""),
                urlencode({"agent_id": self.agent_id, "after_sequence": 0, "limit": 10}),
            )
        )
        payload: Any = None
        if isinstance(chunks, list) and chunks:
            payload = chunks[-1].get("payload") if isinstance(chunks[-1], dict) else None

        request: JsonDict = payload if isinstance(payload, dict) else {}
        request_id = str(request.get("request_id") or "")
        sender = str(stream.get("sender_agent_id") or request.get("sender_agent_id") or "")

        # --- MAC_REFLECT_ENABLED guard ---
        reflect_enabled = _env_bool("MAC_REFLECT_ENABLED", True)
        if not reflect_enabled:
            error_result = reflect_result_payload(
                request_id=request_id,
                agent_id=self.agent_id,
                response="reflect is disabled on this agent (MAC_REFLECT_ENABLED=false)",
                word_count=0,
            )
            self._observe_log(
                "worker.agentbus.reflect.error",
                level="warning",
                detail={"stream_id": stream_id, "reason": "disabled"},
            )
            self._publish_reflect_result(stream, error_result)
            return {"status": "error", "summary": "reflect disabled", "stream_id": stream_id}

        default_query = (
            "Describe your current runtime identity, memory context, host inventory, "
            "active task, capabilities, and status."
        )
        query = str(request.get("query") or default_query).strip() or default_query

        response_text = self._run_reflect_query(query, stream_id=stream_id)

        # Truncate to 300 words
        words = response_text.split()
        if len(words) > 300:
            words = words[:300]
            response_text = " ".join(words)

        result = reflect_result_payload(
            request_id=request_id,
            agent_id=self.agent_id,
            response=response_text,
            word_count=len(words),
        )
        self._observe_log(
            "worker.agentbus.reflect.completed",
            level="info",
            detail={
                "stream_id": stream_id,
                "sender": sender,
                "query_len": len(query),
                "word_count": len(words),
            },
        )
        self._publish_reflect_result(stream, result)
        return {"status": "completed", "summary": "reflect completed", "stream_id": stream_id}

    def _reflect_runtime_query(self, query: str) -> str:
        """Build the bounded prompt sent into this agent's Hermes runtime."""
        request = str(query or "").strip()
        return "\n".join(
            [
                "You are answering a MAC reflect request from inside your own Hermes runtime.",
                "Use your active Hermes context, MAC runtime context, and any "
                "visible host or command inventory.",
                "Answer with concrete details about your runtime identity, memory context, "
                "host inventory or capabilities, active task/status, and the requester query.",
                "Keep the final answer at or below 300 words.",
                "",
                "Requester query:",
                request or "Describe your current runtime identity and status.",
            ]
        )

    def _run_reflect_query(self, query: str, *, stream_id: str = "") -> str:
        """Run *query* as one non-interactive turn of this agent's Hermes runtime.

        Hermes is the only human interface, so a reflect answer comes from the
        host ``hermes`` CLI's one-shot mode (see :func:`hermes_oneshot_command`).
        No direct provider fallback is allowed. The bounded timeout prevents a
        slow LLM response from blocking the worker poll loop.
        """
        runtime_query = self._reflect_runtime_query(query)
        timeout_s = _bounded_float(os.environ.get("MAC_REFLECT_TIMEOUT"), 1.0, 600.0, 120.0)
        try:
            env = os.environ.copy()
            env["MAC_AGENT_ID"] = self.agent_id
            env["MAC_WORKER_AGENT_ID"] = self.agent_id
            result = subprocess.run(
                hermes_oneshot_command(runtime_query),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=env,
                check=False,
            )
            output = (result.stdout or "").strip()
            if result.returncode != 0 or not output:
                # Fall back to stderr summary so something useful is returned.
                stderr_summary = (result.stderr or "").strip()[:500]
                return (
                    "reflect query completed with returncode %d. %s"
                    % (result.returncode, stderr_summary)
                ).strip()
            return output
        except subprocess.TimeoutExpired:
            self._observe_log(
                "worker.agentbus.reflect.error",
                level="warning",
                detail={"stream_id": stream_id, "reason": "timeout", "timeout_s": timeout_s},
            )
            return "reflect query timed out after %d seconds." % int(timeout_s)
        except Exception as exc:  # noqa: BLE001 - reflect must not crash the poll loop
            self._observe_log(
                "worker.agentbus.reflect.error",
                level="warning",
                detail={"stream_id": stream_id, "reason": "subprocess_error", "error": str(exc)},
            )
            return "reflect query failed: %s" % exc

    def _publish_reflect_result(self, stream: JsonDict, result: JsonDict) -> None:
        """Publish a reflect result chunk back to the sender."""
        sender = str(stream.get("sender_agent_id") or "")
        if not sender:
            return
        try:
            self.client.post(
                "/agentbus",
                {
                    "sender_agent_id": self.agent_id,
                    "recipient_agent_id": sender,
                    "content_type": REFLECT_RESULT_CONTENT_TYPE,
                    "topic": REFLECT_RESULT_TOPIC,
                    "payload": result,
                },
            )
        except Exception as exc:  # noqa: BLE001 - result publishing is best-effort.
            self._observe_log(
                "worker.agentbus.reflect.publish_failed",
                level="warning",
                detail={"stream_id": stream.get("id"), "error": str(exc)},
            )
