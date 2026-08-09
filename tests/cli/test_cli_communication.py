from __future__ import annotations

import io
import json
import sys

from mac.test_support import dsn_for
from mac.cli import main


def _run(tmp_path, *args):
    out = io.StringIO()
    old_out = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old_out
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_communication_cli_shared_identity_delivery_lifecycle(tmp_path):
    rc, machine = _run(tmp_path, "admin", "machine", "register", "communication-host")
    assert rc == 0
    rc, agent = _run(
        tmp_path,
        "agent",
        "register",
        machine["id"],
        "communication-gateway",
    )
    assert rc == 0

    rc, identity = _run(
        tmp_path,
        "admin", "communication",
        "identity",
        "configure",
        "mac-hive",
        "--display-name",
        "MAC Hive",
        "--default",
    )
    assert rc == 0
    assert identity["name"] == "mac-hive"
    rc, identities = _run(tmp_path, "admin", "communication", "identity", "list")
    assert rc == 0
    assert [item["id"] for item in identities] == [identity["id"]]
    rc, shown_identity = _run(
        tmp_path, "admin", "communication", "identity", "show", "mac-hive"
    )
    assert rc == 0
    assert shown_identity["id"] == identity["id"]

    rc, account = _run(
        tmp_path,
        "admin", "communication",
        "account",
        "configure",
        identity["id"],
        "slack",
        "--account-id",
        "operations",
        "--credential-refs",
        '{"bot":"secret://channel-identity.mac-hive.slack.operations.bot"}',
        "--config",
        '{"default":true}',
    )
    assert rc == 0
    assert account["channel"] == "slack"
    rc, accounts = _run(
        tmp_path,
        "admin", "communication",
        "account",
        "list",
        "--identity",
        identity["id"],
    )
    assert rc == 0
    assert [item["id"] for item in accounts] == [account["id"]]
    rc, shown_account = _run(
        tmp_path, "admin", "communication", "account", "show", account["id"]
    )
    assert rc == 0
    assert shown_account["account_id"] == "operations"

    rc, binding = _run(
        tmp_path,
        "admin", "communication",
        "representation",
        "configure",
        "agent",
        agent["id"],
        "--identity",
        identity["id"],
        "--mode",
        "delegated",
    )
    assert rc == 0
    rc, bindings = _run(
        tmp_path,
        "admin", "communication",
        "representation",
        "list",
        "--subject-kind",
        "agent",
    )
    assert rc == 0
    assert [item["id"] for item in bindings] == [binding["id"]]
    rc, resolution = _run(
        tmp_path,
        "admin", "communication",
        "representation",
        "resolve",
        agent["id"],
    )
    assert rc == 0
    assert resolution["identity"]["id"] == identity["id"]

    rc, lease = _run(
        tmp_path,
        "admin", "communication",
        "lease",
        "acquire",
        account["id"],
        agent["id"],
    )
    assert rc == 0
    rc, leases = _run(
        tmp_path,
        "admin", "communication",
        "lease",
        "list",
        "--agent-id",
        agent["id"],
        "--active-only",
    )
    assert rc == 0
    assert [item["id"] for item in leases] == [lease["id"]]
    rc, renewed = _run(
        tmp_path,
        "admin", "communication",
        "lease",
        "renew",
        lease["id"],
        agent["id"],
        lease["fencing_token"],
        "--lease-seconds",
        "120",
    )
    assert rc == 0
    assert renewed["fencing_token"] == lease["fencing_token"]

    rc, delivery = _run(
        tmp_path,
        "admin", "communication",
        "send",
        "channel:C123",
        "Task complete",
        "--origin-agent-id",
        agent["id"],
        "--channel",
        "slack",
        "--idempotency-key",
        "cli-delivery",
    )
    assert rc == 0
    assert delivery["status"] == "pending"
    rc, deliveries = _run(
        tmp_path,
        "admin", "communication",
        "deliveries",
        "--identity",
        identity["id"],
    )
    assert rc == 0
    assert [item["id"] for item in deliveries] == [delivery["id"]]

    rc, released = _run(
        tmp_path,
        "admin", "communication",
        "lease",
        "release",
        lease["id"],
        agent["id"],
        lease["fencing_token"],
    )
    assert rc == 0
    assert released == {"released": lease["id"]}


def test_communication_cli_delete_surfaces(tmp_path):
    rc, identity = _run(
        tmp_path, "admin", "communication", "identity", "configure", "delete-hive"
    )
    assert rc == 0
    rc, account = _run(
        tmp_path,
        "admin", "communication",
        "account",
        "configure",
        identity["id"],
        "telegram",
    )
    assert rc == 0
    rc, binding = _run(
        tmp_path,
        "admin", "communication",
        "representation",
        "configure",
        "project",
        "delete-project",
        "--identity",
        identity["id"],
    )
    assert rc == 0

    rc, deleted_binding = _run(
        tmp_path,
        "admin", "communication",
        "representation",
        "delete",
        binding["id"],
    )
    assert rc == 0
    assert deleted_binding == {"deleted": binding["id"]}
    rc, deleted_account = _run(
        tmp_path, "admin", "communication", "account", "delete", account["id"]
    )
    assert rc == 0
    assert deleted_account == {"deleted": account["id"]}
    rc, deleted_identity = _run(
        tmp_path, "admin", "communication", "identity", "delete", identity["id"]
    )
    assert rc == 0
    assert deleted_identity == {"deleted": identity["id"]}
