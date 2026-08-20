"""`mac admin persona-instance` — the renamed Hermes noun (ADR 0025).

These four subcommands sat in the coverage gate's ``KNOWN_UNTESTED`` list under
their old ``hermes`` spelling. Renaming the noun without covering it would have
moved the debt rather than paid it, and the rename is exactly the change most
likely to break quietly: an argparse alias that resolves nowhere fails as a
usage error, which reads like operator fault rather than a regression.

Structure: each subcommand gets a test under the canonical noun, and alias
parity is a single separate test that walks every subcommand and asserts the two
spellings return the same thing. Parametrizing the noun across all four would
have read as more coverage while testing the alias four times and the parity
claim zero times — and the coverage gate, which scans for literal string
arguments to ``_run``, would not have seen the subcommands at all.

The service layer underneath was already persona-named
(``register_persona_instance``, ``persona_context``, ...); only the CLI noun was
Hermes, which is why this rename changes spelling and not behaviour.
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
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


@pytest.fixture
def tenant_id(tmp_path):
    rc, tenant = _run(tmp_path, "admin", "tenant", "register", "acme")
    assert rc == 0
    return tenant["id"]


@pytest.fixture
def instance(tmp_path, tenant_id):
    rc, created = _run(
        tmp_path, "admin", "persona-instance", "register", tenant_id, "scribe"
    )
    assert rc == 0
    return created


def test_register_creates_a_persona_instance(tmp_path, tenant_id):
    rc, created = _run(
        tmp_path, "admin", "persona-instance", "register", tenant_id, "herald"
    )
    assert rc == 0
    assert created["name"] == "herald"
    assert created["tenant_id"] == tenant_id
    assert created["status"] == "active"


def test_context_reports_the_instance(tmp_path, instance):
    rc, context = _run(tmp_path, "admin", "persona-instance", "context", instance["id"])
    assert rc == 0
    assert context["persona_instance"]["id"] == instance["id"]
    # `hermes_instance` is the pre-persona key, kept for the same reason the CLI
    # alias is: deployed readers still ask for it by that name.
    assert context["hermes_instance"] == context["persona_instance"]


def test_work_context_is_empty_for_a_fresh_instance(tmp_path, instance):
    rc, work = _run(
        tmp_path, "admin", "persona-instance", "work-context", instance["id"]
    )
    assert rc == 0
    assert work["tasks"] == []


def test_runtime_proof_skipping_the_startup_report(tmp_path, instance):
    """``--skip-startup-report`` is what makes this testable off a fleet node.

    Without it the command calls ``build_hermes_startup_report()``, which probes
    the local gateway, tokenhub and Qdrant. That call is also why ADR 0025
    refuses to delete ``hermes_startup``: it is on the live path here and at four
    points in ``api.py``, so the "dead runtime" delete would have taken the hub
    down.
    """
    rc, proof = _run(
        tmp_path,
        "admin",
        "persona-instance",
        "runtime-proof",
        instance["id"],
        "--skip-startup-report",
    )
    assert rc == 0
    # Frozen wire string: deployed nodes read this exact spelling, so ADR 0025
    # keeps it even though the CLI noun moved off "hermes".
    assert proof["schema"] == "mac.hermes_runtime_proof.v1"


@pytest.mark.parametrize(
    "subcommand,extra",
    [
        ("context", ()),
        ("work-context", ()),
        ("runtime-proof", ("--skip-startup-report",)),
    ],
)
def test_hermes_alias_is_the_same_command(tmp_path, instance, subcommand, extra):
    """The alias is not a second code path — both spellings return one result.

    `register` is excluded: it mutates, so the two calls would create two
    different instances and comparing them would prove nothing.
    """
    rc_new, via_new = _run(
        tmp_path, "admin", "persona-instance", subcommand, instance["id"], *extra
    )
    rc_old, via_old = _run(
        tmp_path, "admin", "hermes", subcommand, instance["id"], *extra
    )
    assert rc_new == rc_old == 0
    assert via_new == via_old


def test_hermes_alias_still_registers(tmp_path, tenant_id):
    rc, created = _run(tmp_path, "admin", "hermes", "register", tenant_id, "legacy")
    assert rc == 0
    assert created["name"] == "legacy"
