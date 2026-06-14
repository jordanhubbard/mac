"""Render the OpenShell guardrail policy from the operator template.

A real policy is fleet-specific — it names the actual hub / Qdrant / Firecrawl
hosts and the agent's home user — so it can't be committed verbatim; it's
rendered at deploy time from ``deploy/openshell/mac-hermes-policy.yaml`` and
installed at ``~/.mac/openshell-policy.yaml`` (where the executor's
``_resolve_openshell_policy`` finds it). This is the policy half of OpenShell
enforcement; flipping ``MAC_OPENSHELL_SANDBOX=1`` + fail-closed additionally
requires the Hermes-runtime sandbox image to be present (tracked separately).

``render_policy`` substitutes the ``__PLACEHOLDER__`` tokens and appends the
shared-service egress blocks (Qdrant, Firecrawl) the template leaves to the
operator, then verifies no placeholder survived (fail-closed against a
half-filled policy).
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

_PLACEHOLDER_RE = re.compile(r"__[A-Z0-9_]+__")


def render_policy(
    template_text: str,
    *,
    agent_user: str,
    hub_host: str,
    hub_port: int,
    model_gateway_host: Optional[str] = None,
    shared_services: Optional[Dict[str, int]] = None,
    image_runtime: Optional[str] = None,
) -> str:
    """Fill the operator policy template for one fleet.

    - ``agent_user`` -> ``__AGENT_USER__`` (the home owner; runtime + caches).
    - ``hub_host``/``hub_port`` -> the MAC hub (tasks/evidence/LLM router).
    - ``model_gateway_host`` -> the LLM gateway host; defaults to ``hub_host``
      (the in-mac router serves ``/v1`` from the hub).
    - ``shared_services`` -> {name: port} egress blocks appended to
      network_policies (e.g. {"qdrant": 6333, "firecrawl": 3002}), all on
      ``hub_host`` (the fleet's shared-services manager).
    - ``image_runtime`` -> when set (e.g. ``/opt/mac-venv``), the runtime/caches
      resolve to the in-image install paths instead of the host ``~/.mac`` —
      use this when the sandbox runs a prebuilt image via ``--from`` rather than
      a host-uploaded runtime. In image mode caches live under ``/tmp`` (the
      image bakes no per-user home).

    Raises ``ValueError`` if any ``__PLACEHOLDER__`` remains unresolved.
    """
    if not agent_user or not hub_host or not hub_port:
        raise ValueError("agent_user, hub_host and hub_port are required")
    if image_runtime:
        runtime_venv = image_runtime.rstrip("/")
        runtime_src = runtime_venv
        # No per-user home in the image; caches land under /tmp (already in the
        # writable set, so these resolve to existing, Landlock-classifiable dirs
        # rather than nonexistent leaves that would break hard_requirement).
        cache_dir = config_dir = "/tmp"
    else:
        base = "/home/%s/.mac" % agent_user
        runtime_venv = base + "/venv"
        runtime_src = base + "/src"
        cache_dir = "/home/%s/.cache" % agent_user
        config_dir = "/home/%s/.config" % agent_user
    runtime_py = runtime_venv + "/bin/python"
    text = template_text
    subs = {
        "__AGENT_USER__": agent_user,
        "__CACHE_DIR__": cache_dir,
        "__CONFIG_DIR__": config_dir,
        "__RUNTIME_VENV__": runtime_venv,
        "__RUNTIME_SRC__": runtime_src,
        "__RUNTIME_PY__": runtime_py,
        "__MAC_HUB_HOST__": hub_host,
        "__MAC_HUB_PORT__": str(hub_port),
        "__MODEL_GATEWAY_HOST__": str(model_gateway_host or hub_host),
    }
    for token, value in subs.items():
        text = text.replace(token, value)

    blocks = _shared_service_blocks(shared_services or {}, hub_host, runtime_py)
    if blocks:
        text = text.rstrip() + "\n" + blocks + "\n"

    # Only fail on placeholders in ACTIVE config — the template documents tokens
    # in prose comments and keeps an optional commented-out block, both of which
    # legitimately retain __TOKENS__.
    active = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    leftover = sorted(set(_PLACEHOLDER_RE.findall(active)))
    if leftover:
        raise ValueError("unresolved policy placeholders: %s" % ", ".join(leftover))
    return text


def _shared_service_blocks(services: Dict[str, int], host: str, py: str) -> str:
    """YAML network_policies blocks for the fleet's shared services (Qdrant,
    Firecrawl, …), appended under the template's existing network_policies.

    ``py`` is the runtime python binary allowed to make the egress (host or
    in-image path), matching the filesystem policy's runtime location."""
    lines: List[str] = []
    for name in sorted(services):
        port = int(services[name])
        lines += [
            "  %s:" % name,
            "    name: %s" % name,
            "    endpoints:",
            "      - host: %s" % host,
            "        port: %d" % port,
            "        protocol: rest",
            "        enforcement: enforce",
            "        access: full",
            "    binaries:",
            "      - { path: %s }" % py,
        ]
    return "\n".join(lines)
