"""Declaring a task's sandbox egress, at the right trust tier.

PR #297 grants sandbox egress to hosts listed in a task's TOP-LEVEL
``metadata.egress_contract.hosts``, at the ``hub_declared`` tier. Until now the
only way to set that was ``mac task create --metadata-file``, i.e. hand-
authoring the whole metadata object -- and getting the location wrong does not
fail loudly. It silently downgrades the hosts to the untrusted ``derived`` tier,
where they are refused, and the operator sees a host they granted simply not
work (task_97b97266).

The location IS the security model:

``metadata.egress_contract``      top level, set through an authenticated hub
                                  credential -> hub_declared
``metadata.runtime.…``            written by the worker from repo content, so
                                  it carries only repo trust -> derived, and
                                  refused unless it matches the reviewed
                                  registry allowlist

A CLI that wrote to the wrong subtree would quietly defeat that distinction
while appearing to work, which is why it is asserted here rather than left to
a reader's care.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


@pytest.fixture()
def task(tmp_path):
    rc, _ = _run(tmp_path, "project", "create", "mac")
    assert rc == 0
    rc, created = _run(tmp_path, "task", "create", "needs egress", "--project", "mac")
    assert rc == 0
    return created


def _metadata(tmp_path, task_id):
    _rc, detail = _run(tmp_path, "task", "show", task_id)
    record = detail.get("task", detail) if isinstance(detail, dict) else detail
    return record.get("metadata") or {}


# --------------------------------------------------------------------------
# The security property
# --------------------------------------------------------------------------


def test_a_granted_host_lands_at_top_level_not_under_runtime(tmp_path, task):
    """The whole point. Under `runtime` these hosts would be classified
    `derived` and refused, and nothing would say so."""
    rc, _ = _run(tmp_path, "task", "egress", "grant", task["id"], "api.example.com")
    assert rc == 0

    metadata = _metadata(tmp_path, task["id"])

    assert metadata["egress_contract"]["hosts"] == ["api.example.com"]
    runtime = metadata.get("runtime") or {}
    assert "egress_contract" not in runtime
    assert "api.example.com" not in json.dumps(runtime)


def test_the_listing_names_the_tier_it_grants(tmp_path, task):
    """An operator should not have to read the policy module to learn which
    tier they just used."""
    _run(tmp_path, "task", "egress", "grant", task["id"], "api.example.com")

    _rc, listed = _run(tmp_path, "task", "egress", "list", task["id"])

    assert listed["trust_tier"] == "hub_declared"
    assert listed["source"] == "metadata.egress_contract"


# --------------------------------------------------------------------------
# Validation at write time
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "https://api.example.com",   # scheme
        "api.example.com:443",       # port
        "api.example.com/v1",        # path
        "*.example.com",             # glob
        "10.0.0.1",                  # IP literal
        "user:pw@example.com",       # credentials
        "api example.com",           # whitespace
        "",
    ],
)
def test_a_malformed_host_is_refused_at_the_cli(tmp_path, task, bad):
    """Rejected here, not silently dropped at sandbox build time.

    Dropping it later shows the operator a host they granted that simply does
    not work, with nothing pointing at why.
    """
    # The CLI maps a domain error to a non-zero exit and a stderr line rather
    # than letting the exception escape, so that is the contract to assert.
    rc, _ = _run(tmp_path, "task", "egress", "grant", task["id"], bad)

    assert rc != 0
    assert not (_metadata(tmp_path, task["id"]).get("egress_contract") or {}).get("hosts")


def test_a_hostname_is_normalized(tmp_path, task):
    """The DNS root dot and case are not two different hosts."""
    _run(tmp_path, "task", "egress", "grant", task["id"], "API.Example.COM.")

    listed = _metadata(tmp_path, task["id"])["egress_contract"]["hosts"]

    assert listed == ["api.example.com"]


# --------------------------------------------------------------------------
# Ordinary behaviour
# --------------------------------------------------------------------------


def test_hosts_accumulate_and_stay_sorted(tmp_path, task):
    _run(tmp_path, "task", "egress", "grant", task["id"], "b.example.com")
    _run(tmp_path, "task", "egress", "grant", task["id"], "a.example.com")

    _rc, listed = _run(tmp_path, "task", "egress", "list", task["id"])

    assert listed["hosts"] == ["a.example.com", "b.example.com"]


def test_granting_the_same_host_twice_is_a_no_op(tmp_path, task):
    _run(tmp_path, "task", "egress", "grant", task["id"], "a.example.com")

    _rc, again = _run(tmp_path, "task", "egress", "grant", task["id"], "a.example.com")

    assert again["unchanged"] is True
    assert _metadata(tmp_path, task["id"])["egress_contract"]["hosts"] == ["a.example.com"]


def test_revoke_removes_only_that_host(tmp_path, task):
    _run(tmp_path, "task", "egress", "grant", task["id"], "a.example.com")
    _run(tmp_path, "task", "egress", "grant", task["id"], "b.example.com")

    _run(tmp_path, "task", "egress", "revoke", task["id"], "a.example.com")

    _rc, listed = _run(tmp_path, "task", "egress", "list", task["id"])
    assert listed["hosts"] == ["b.example.com"]


def test_revoking_a_host_that_was_not_granted_is_an_error(tmp_path, task):
    """Silently succeeding would let an operator believe they closed something."""
    rc, _ = _run(tmp_path, "task", "egress", "revoke", task["id"], "never.example.com")

    assert rc != 0


def test_listing_a_task_with_no_contract_is_empty_not_an_error(tmp_path, task):
    _rc, listed = _run(tmp_path, "task", "egress", "list", task["id"])

    assert listed["hosts"] == []


def test_a_reason_is_recorded_with_the_grant(tmp_path, task):
    """Why a host was opened is the thing a later reviewer needs."""
    _run(
        tmp_path, "task", "egress", "grant", task["id"], "a.example.com",
        "--reason", "aviation_apis moved per-repo",
    )

    _rc, listed = _run(tmp_path, "task", "egress", "list", task["id"])
    assert "aviation_apis" in (listed["reason"] or "")


def test_granting_preserves_unrelated_metadata(tmp_path):
    """A grant must not be a metadata overwrite."""
    _run(tmp_path, "project", "create", "mac")
    _rc, created = _run(
        tmp_path, "task", "create", "keep my metadata", "--project", "mac",
        "--metadata", json.dumps({"origin": {"kind": "operator"}}),
    )

    _run(tmp_path, "task", "egress", "grant", created["id"], "a.example.com")

    metadata = _metadata(tmp_path, created["id"])
    assert metadata["origin"]["kind"] == "operator"
    assert metadata["egress_contract"]["hosts"] == ["a.example.com"]
