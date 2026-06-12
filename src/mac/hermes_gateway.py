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
import os
import shlex
import sys
from pathlib import Path
from typing import Callable, List, Optional


# ---------------------------------------------------------------------------
# OpenShell gateway sandboxing (sandbox-01)
#
# The gateway is a long-running service that runs tools in response to chat
# messages. With never-prompt approval on (approvals.mode=off) it acts silently,
# so OpenShell must be its enforcement layer. When MAC_OPENSHELL_GATEWAY is
# enabled, the entrypoint RE-EXECS itself as a confined child of an *ephemeral*
# OpenShell sandbox (no --keep/--name, so the sandbox lifetime == the gateway
# process lifetime and supervisor restarts never collide). A guard env breaks
# the re-exec loop once we're inside the sandbox.
#
# Default OFF: with MAC_OPENSHELL_GATEWAY unset the entrypoint runs exactly as
# before. A policy is ALWAYS passed (never OpenShell's image default); if none
# resolves we raise rather than run the gateway unconfined.
#
# Knobs:
#   MAC_OPENSHELL_GATEWAY              truthy -> sandbox the gateway service
#   MAC_OPENSHELL_BIN                 openshell binary (default "openshell")
#   MAC_OPENSHELL_GATEWAY_POLICY      explicit policy path (else resolved)
#   MAC_OPENSHELL_GATEWAY_CREATE_ARGS extra `sandbox create` args (shell-split)
# ---------------------------------------------------------------------------

_GATEWAY_ACTIVE_ENV = "_MAC_OPENSHELL_GATEWAY_ACTIVE"


def _gateway_sandbox_enabled() -> bool:
    return (os.environ.get("MAC_OPENSHELL_GATEWAY") or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_gateway_policy() -> str:
    """Resolve the gateway's OpenShell policy (always returns one, or raises).

    explicit MAC_OPENSHELL_GATEWAY_POLICY -> ~/.mac/openshell-gateway-policy.yaml
    -> bundled fail-closed default. Never returns empty, so the gateway can't
    silently run under OpenShell's image-default profile.
    """
    explicit = (os.environ.get("MAC_OPENSHELL_GATEWAY_POLICY") or "").strip()
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError("MAC_OPENSHELL_GATEWAY_POLICY=%r but no such file" % explicit)
        return explicit
    deployed = Path.home() / ".mac" / "openshell-gateway-policy.yaml"
    if deployed.is_file():
        return str(deployed)
    bundled = Path(__file__).resolve().parent / "openshell" / "gateway-default-policy.yaml"
    if bundled.is_file():
        return str(bundled)
    raise FileNotFoundError(
        "MAC_OPENSHELL_GATEWAY is enabled but no gateway policy could be resolved "
        "(set MAC_OPENSHELL_GATEWAY_POLICY, install %s, or ship %s)." % (deployed, bundled)
    )


def _build_gateway_sandbox_argv() -> List[str]:
    """``openshell sandbox create ... -- <python> -m mac.hermes_gateway`` (ephemeral)."""
    bin_ = (os.environ.get("MAC_OPENSHELL_BIN") or "openshell").strip() or "openshell"
    argv: List[str] = [bin_, "sandbox", "create", "--no-auto-providers"]
    argv += ["--policy", _resolve_gateway_policy()]
    extra = (os.environ.get("MAC_OPENSHELL_GATEWAY_CREATE_ARGS") or "").strip()
    if extra:
        argv += shlex.split(extra)
    argv += ["--", sys.executable, "-m", "mac.hermes_gateway"]
    return argv


def _maybe_reexec_under_openshell() -> None:
    """Re-exec the gateway under an OpenShell sandbox when enabled (no-op else).

    Replaces the current process (``os.execvp``) so the sandbox becomes the
    service's main process; the supervisor (systemd/launchd) manages restarts.
    The guard env stops the re-exec recursing once we're inside the sandbox.
    """
    if not _gateway_sandbox_enabled():
        return
    if os.environ.get(_GATEWAY_ACTIVE_ENV) == "1":
        return  # already inside the sandbox — run the gateway for real
    os.environ[_GATEWAY_ACTIVE_ENV] = "1"
    argv = _build_gateway_sandbox_argv()
    sys.stderr.write("[mac.hermes_gateway] re-exec under OpenShell: %s\n" % " ".join(argv))
    os.execvp(argv[0], argv)  # noqa: S606 — operator-enabled, fixed argv shape


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


def main(argv: Optional[list] = None, _cli_main: Optional[Callable[[], int]] = None) -> int:
    """Launch the vendored Hermes gateway in-process.

    Faithfully reproduces the deployed ``hermes gateway run --replace`` path:
    that command dispatches through ``hermes_cli.main.main()`` to
    ``hermes_cli.gateway.run_gateway(replace=True)`` — NOT ``gateway.run.main()``
    (which doesn't handle ``--replace`` or the profile/setup dispatch). We invoke
    the same CLI entry in-process with the same argv.

    ``argv`` overrides the gateway args (default ``["--replace"]``).
    ``_cli_main`` is injectable for testing so the launch path can be verified
    without booting the real gateway (which needs platform config).
    """
    if _cli_main is None:
        # Real launch path: confine the whole gateway under OpenShell when
        # enabled (replaces this process). Skipped when _cli_main is injected
        # (tests verify the launch path without re-exec'ing).
        _maybe_reexec_under_openshell()

    from mac.hermes_vendor import ensure_on_path

    ensure_on_path()
    log_provider_decision()
    extra = list(argv) if argv is not None else ["--replace"]
    sys.argv = ["hermes", "gateway", "run", *extra]
    if _cli_main is None:
        from hermes_cli.main import main as _cli_main  # same entry as `hermes ...`
    return _cli_main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
