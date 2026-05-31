"""In-process launcher for the vendored Hermes gateway (ADR 0001, hu-03/hu-04).

Replaces the separate-venv ``hermes gateway run`` subprocess. Runs the vendored
Hermes gateway inside the mac process namespace — one venv, one dependency set,
one place mac can observe it. The gateway's own ``_resolve_runtime_agent_kwargs``
/ ``_resolve_gateway_model`` already call ``mac.agent_provider`` directly (hu-03),
so the per-agent provider/model override is owned in-process with no string
surgery. hu-04's deploy points the gateway service at ``mac-hermes-gateway``
instead of cloning upstream Hermes into a second venv.

Console script: ``mac-hermes-gateway`` -> ``mac.hermes_gateway:main``.
"""

from __future__ import annotations

import json
import sys
from typing import Callable, Optional


def log_provider_decision(stream=sys.stderr) -> Optional[dict]:
    """Write mac's (secret-free) provider decision so the chosen provider/model
    is legible in the gateway service journal at start — the dark-spot fix at
    launch. Best-effort; never fails the launch. Returns the observable dict."""
    try:
        from mac.agent_provider import resolve_agent_provider

        observable = resolve_agent_provider().observable()
        stream.write("mac.hermes_gateway provider decision: %s\n" % json.dumps(observable))
        return observable
    except Exception:
        return None


def main(argv: Optional[list] = None, _gateway_main: Optional[Callable[[], int]] = None) -> int:
    """Launch the vendored Hermes gateway in-process.

    ``_gateway_main`` is injectable for testing so the launch path can be
    verified without booting the real gateway (which needs platform config).
    """
    from mac.hermes_vendor import ensure_on_path

    ensure_on_path()
    if argv is not None:
        sys.argv = ["mac-hermes-gateway", *argv]
    log_provider_decision()
    if _gateway_main is None:
        from gateway.run import main as _gateway_main  # vendored gateway entrypoint
    return _gateway_main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
