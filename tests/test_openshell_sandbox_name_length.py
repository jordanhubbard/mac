"""Every OpenShell sandbox name MAC generates must fit 0.1.2's 19-char limit.

OpenShell 0.1.2 rejects sandbox names over 19 characters. A generator that is
only ever exercised on the legacy 0.0.x gateway is a latent verification
failure: the hub verifier's old ``mac-hubverify-<16 hex>`` name was 30
characters and kept working only because it ran against an older gateway.
"""

from __future__ import annotations

import importlib

import pytest

MAX_SANDBOX_NAME_LENGTH = 19


def _generators():
    sandbox = importlib.import_module("mac.executor_sandbox")
    services = importlib.import_module("mac.services")
    return {
        "task": sandbox._sandbox_name,
        "codingcap": sandbox._coding_agent_probe_sandbox_name,
        "read-only-verifier": sandbox._read_only_verifier_sandbox_name,
        "hubverify": services._hub_verify_sandbox_name,
    }


@pytest.mark.parametrize("kind", ["task", "codingcap", "read-only-verifier", "hubverify"])
def test_every_generated_sandbox_name_fits_the_limit(monkeypatch, kind):
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX_NAME", raising=False)
    monkeypatch.delenv("MAC_TASK_OPENSHELL_SANDBOX_NAME", raising=False)

    name = _generators()[kind]()

    assert name.startswith("mac-")
    assert len(name) <= MAX_SANDBOX_NAME_LENGTH


def test_hub_verify_name_uses_the_short_prefix(monkeypatch):
    services = importlib.import_module("mac.services")

    name = services._hub_verify_sandbox_name()

    assert name.startswith("mac-hv-")
    assert len(name) == len("mac-hv-") + 10
