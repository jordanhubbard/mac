"""Behavioral coverage for the fleet-directive CLI lifecycle."""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main


def _run(tmp_path, *args):
    output = io.StringIO()
    previous = sys.stdout
    sys.stdout = output
    try:
        returncode = main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout = previous
    raw = output.getvalue().strip()
    return returncode, json.loads(raw) if raw else None


def test_directive_cli_full_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MAC_DIRECTIVES_ENABLED", "1")
    document_path = tmp_path / "directive.yaml"
    document_path.write_text(
        "\n".join(
            (
                "schema: mac.directive.v1",
                "name: test.require-review",
                "description: Exercise the complete directive CLI lifecycle.",
                "scope: fleet",
                "set:",
                "  review.required: true",
                "",
            )
        ),
        encoding="utf-8",
    )

    rc, proposed = _run(
        tmp_path,
        "directive",
        "propose",
        "--document-file",
        str(document_path),
        "--actor",
        "test",
    )
    assert rc == 0
    directive_id = proposed["id"]
    digest = proposed["versions"][0]["digest"]

    rc, listed = _run(tmp_path, "directive", "list")
    assert rc == 0 and any(item["id"] == directive_id for item in listed)
    rc, shown = _run(tmp_path, "directive", "show", directive_id)
    assert rc == 0 and shown["id"] == directive_id
    rc, versions = _run(tmp_path, "directive", "versions", directive_id)
    assert rc == 0 and versions[0]["digest"] == digest

    rc, binding = _run(
        tmp_path,
        "directive",
        "binding",
        "set",
        "fleet",
        "fleet",
        "build.primary_target",
        "--value",
        '"//app:all"',
        "--actor",
        "test",
    )
    assert rc == 0 and binding["value"] == "//app:all"
    rc, bindings = _run(tmp_path, "directive", "binding", "list")
    assert rc == 0 and bindings[0]["id"] == binding["id"]

    rc, checked = _run(
        tmp_path, "directive", "check", directive_id, "--version", "1", "--actor", "test"
    )
    assert rc == 0 and checked["status"] == "pass"
    rc, impact = _run(tmp_path, "directive", "impact", directive_id)
    assert rc == 0 and impact["latest_check"]["id"] == checked["id"]
    rc, approved = _run(
        tmp_path,
        "directive",
        "approve",
        directive_id,
        "--version",
        "1",
        "--digest",
        digest,
        "--check-id",
        checked["id"],
        "--actor",
        "test",
    )
    assert rc == 0 and approved["approved_by"] == "test"
    rc, activated = _run(
        tmp_path,
        "directive",
        "activate",
        directive_id,
        "--version",
        "1",
        "--digest",
        digest,
        "--actor",
        "test",
    )
    assert rc == 0 and activated["state"] == "active"
    rc, effective = _run(tmp_path, "directive", "effective")
    assert rc == 0 and effective["set"]["review.required"] is True

    rc, project = _run(tmp_path, "project", "create", "directive-cli-project")
    assert rc == 0
    rc, waiver = _run(
        tmp_path,
        "directive",
        "waiver",
        "create",
        directive_id,
        "--version",
        "1",
        "--target-type",
        "project",
        "--target-id",
        project["id"],
        "--reason",
        "route test",
        "--actor",
        "test",
    )
    assert rc == 0
    rc, waivers = _run(
        tmp_path, "directive", "waiver", "list", "--directive", directive_id
    )
    assert rc == 0 and waivers[0]["id"] == waiver["id"]
    rc, revoked = _run(
        tmp_path,
        "directive",
        "waiver",
        "revoke",
        waiver["id"],
        "--reason",
        "test complete",
        "--actor",
        "test",
    )
    assert rc == 0 and revoked["revoked_at"]

    rc, deactivated = _run(
        tmp_path,
        "directive",
        "deactivate",
        directive_id,
        "--reason",
        "test complete",
        "--actor",
        "test",
    )
    assert rc == 0 and deactivated["state"] == "deactivated"
