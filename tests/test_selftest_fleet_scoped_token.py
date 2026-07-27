"""Regression for fleet-scoped hub-token resolution in the deploy self-test/wrapper.

Crash fingerprint (crash_1473ec862b1b4c52b99c26407eac9129): on a node migrated to
fleet-scoped credentials (mac-g55y) the correct hub bearer lives in
``MAC_WORKER_TOKEN__<FLEET>`` while the legacy flat ``MAC_WORKER_TOKEN`` is stale
or absent. The embedded ``mac-agent-startup-self-test`` Python heredoc and the
``mac-agent-service`` wrapper in ``deploy/fleet-node-install.sh`` previously read
the flat form only, so the startup heartbeat was rejected with ``HTTP Error 403``.

Both call sites must now mirror :mod:`mac.fleet_env`: prefer the fleet-scoped
form across the whole chain (``MAC_WORKER_TOKEN`` > ``MAC_TOKEN`` >
``MAC_API_TOKEN``) before any flat form, keyed off ``MAC_FLEET`` /
``MAC_FLEET_NAME``, while keeping the flat fallback for un-migrated nodes.

This follows the extract-and-run pattern in
tests/test_selftest_transient_timeout_crash.py.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _self_test_resolver() -> "callable":
    """Extract and compile the self-test's ``_resolve_hub_token`` helper."""
    text = _script_text()
    match = re.search(
        r"(def _resolve_hub_token\(\) -> str:\n(?:.*?\n)*?    return \"\"\n)",
        text,
    )
    assert match, "_resolve_hub_token not found in self-test heredoc"
    namespace: dict = {"os": os}
    exec(compile(match.group(1), "<selftest-resolver>", "exec"), namespace)
    return namespace["_resolve_hub_token"]


def _run_self_test_resolver(env: dict) -> str:
    resolver = _self_test_resolver()
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        return resolver()
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _wrapper_resolver_snippet() -> str:
    """Extract the wrapper's inline fleet-scoped token resolver and echo its result."""
    text = _script_text()
    start = text.index('mac_token_value=""')
    end = text.index("export MAC_WORKER_TOKEN", start) + len("export MAC_WORKER_TOKEN")
    return text[start:end] + "\nprintf '%s' \"$MAC_WORKER_TOKEN\"\n"


def _run_wrapper_resolver(env: dict) -> str:
    snippet = _wrapper_resolver_snippet()
    result = subprocess.run(
        ["bash", "-c", "set -u\n" + snippet],
        env={k: v for k, v in env.items()},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# (env, expected) cases exercised identically against both resolvers.
_CASES = [
    # Migrated node: scoped token wins over a stale flat token.
    ({"MAC_FLEET_NAME": "mac-g55y", "MAC_WORKER_TOKEN": "STALE",
      "MAC_WORKER_TOKEN__MAC_G55Y": "GOOD"}, "GOOD"),
    # Un-migrated node: flat token is still honored (one deprecation cycle).
    ({"MAC_FLEET_NAME": "mac-g55y", "MAC_WORKER_TOKEN": "FLAT_ONLY"}, "FLAT_ONLY"),
    # No fleet configured: flat fallback.
    ({"MAC_WORKER_TOKEN": "FLAT_NOFLEET"}, "FLAT_NOFLEET"),
    # MAC_FLEET overrides MAC_FLEET_NAME; scoped value later in the chain wins.
    ({"MAC_FLEET": "rocky", "MAC_FLEET_NAME": "ignored",
      "MAC_TOKEN__ROCKY": "SCOPED_MAC_TOKEN"}, "SCOPED_MAC_TOKEN"),
    # Cross-chain: a scoped MAC_API_TOKEN outranks a flat MAC_WORKER_TOKEN.
    ({"MAC_FLEET_NAME": "rocky", "MAC_WORKER_TOKEN": "flat",
      "MAC_API_TOKEN__ROCKY": "scopedapi"}, "scopedapi"),
    # An explicitly empty scoped token counts as set (mirrors mac.fleet_env).
    ({"MAC_FLEET_NAME": "rocky", "MAC_WORKER_TOKEN__ROCKY": "",
      "MAC_WORKER_TOKEN": "flat"}, ""),
    # Nothing set: empty result (the wrapper :? guard would then fire).
    ({"MAC_FLEET_NAME": "rocky"}, ""),
]


def test_self_test_prefers_fleet_scoped_token():
    for env, expected in _CASES:
        assert _run_self_test_resolver(env) == expected, env


def test_wrapper_prefers_fleet_scoped_token():
    for env, expected in _CASES:
        assert _run_wrapper_resolver(env) == expected, env


def test_self_test_heredoc_no_longer_reads_flat_token_only():
    text = _script_text()
    assert 'token = os.environ.get("MAC_WORKER_TOKEN") or ""' not in text
    assert "token = _resolve_hub_token()" in text
