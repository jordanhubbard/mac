"""Regression tests for sealed fail-forward phase-zero substrate adoption.

These cover the exact operational state described by the originating incident: a
fungible node's first normal deployment moved the canonical phase-zero source and
venv into generation backups, ``apply-phase2`` then failed, and retain-forward
recovery kept the failed successor in place without rolling back.  The next
repair attempt no longer has complete canonical source/venv directories, so it
must adopt the archived phase-zero substrate as its predecessor recovery contract
rather than activating it as a live generation or performing an implicit rollback.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "fleet-node-substrate-adopt.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("fleet_node_substrate_adopt", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def module():
    return _load_helper()


ONBOARDING_GENERATION = "20260720T000000Z"
FAILED_GENERATION = "20260723T172448Z"
FAILED_REVISION = "a" * 40


def _write_private_json(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
    os.chmod(path, 0o600)
    import hashlib

    return hashlib.sha256(body).hexdigest()


def _seed_first_deploy_failure(module, home: Path, mac_home: Path) -> dict:
    """Reproduce the interrupted first deployment + retain-forward terminal state.

    Returns the layout plus the two sealed inputs the next repair attempt hands
    to the adopter.
    """

    layout = module.Layout.for_home(home, mac_home)

    # The first normal deployment moved the pristine phase-zero source/venv into
    # generation backups.  The live source/venv are now the FAILED successor.
    backups = mac_home / "backups"
    source_backup = backups / f"mac-src.agent_jordanh-worker4.{FAILED_GENERATION}"
    venv_backup = backups / f"venv.agent_jordanh-worker4.{FAILED_GENERATION}"
    for archive in (source_backup, venv_backup):
        archive.mkdir(parents=True)
        (archive / "marker").write_text("phase-zero archived substrate\n")
        os.chmod(archive, 0o700)

    # Failed successor generation retained in place for diagnosis.
    layout.source.mkdir(parents=True)
    (layout.source / "broken").write_text("failed generation\n")
    layout.venv.mkdir(parents=True)
    (layout.venv / "broken").write_text("failed generation\n")

    # Phase-zero machine-onboarding receipt (the immutable baseline binding).
    _write_private_json(
        layout.onboarding_receipt,
        {
            "schema": module.ONBOARDING_RECEIPT_SCHEMA,
            "status": "published",
            "agent": "agent_jordanh-worker4",
            "instance_kind": "fungible",
            "generation": ONBOARDING_GENERATION,
            "paths": {"source": str(layout.source), "venv": str(layout.venv)},
        },
    )

    # Sealed rollback intent armed for the failed first deployment.  Its backups
    # point at the archived phase-zero substrate.
    intent_path = mac_home / "logs" / f"rollback-{FAILED_GENERATION}-intent.json"
    intent_sha = _write_private_json(
        intent_path,
        {
            "schema": module.ROLLBACK_INTENT_SCHEMA,
            "status": "armed",
            "agent": "agent_jordanh-worker4",
            "generation": FAILED_GENERATION,
            "revision": FAILED_REVISION,
            "prior_generation": ONBOARDING_GENERATION,
            "prior_revision": None,
            "rollback_capable": True,
            "artifacts": {
                "source": {"path": str(layout.source), "backup": str(source_backup)},
                "venv": {"path": str(layout.venv), "backup": str(venv_backup)},
            },
        },
    )

    # Terminal retained-forward receipt sealing that exact intent.
    receipt_path = mac_home / "logs" / f"retain-forward-{FAILED_GENERATION}.json"
    _write_private_json(
        receipt_path,
        {
            "schema": module.RETAIN_FORWARD_RECEIPT_SCHEMA,
            "status": "retained_forward",
            "recovery_policy": "retain-forward",
            "rolled_back": False,
            "agent": "agent_jordanh-worker4",
            "failed_generation": FAILED_GENERATION,
            "rollback_intent": {"path": str(intent_path), "sha256": intent_sha},
        },
    )

    return {
        "layout": layout,
        "intent_path": intent_path,
        "receipt_path": receipt_path,
        "source_backup": source_backup,
        "venv_backup": venv_backup,
    }


def test_interrupted_first_deploy_adopts_predecessor_contract(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    payload = module.adopt(
        layout,
        retain_forward_receipt=fixture["receipt_path"],
        rollback_intent=fixture["intent_path"],
    )

    assert payload["schema"] == module.ADOPTION_RECEIPT_SCHEMA
    assert payload["status"] == "adopted"
    # Never activated as a live generation, never an implicit rollback.
    assert payload["activated_as_generation"] is False
    assert payload["implicit_rollback"] is False
    assert payload["activation"] == "predecessor_recovery_contract"
    assert payload["onboarding_generation"] == ONBOARDING_GENERATION
    assert payload["failed_generation"] == FAILED_GENERATION
    assert payload["predecessor_recovery"]["source_backup"] == str(fixture["source_backup"])
    assert payload["predecessor_recovery"]["venv_backup"] == str(fixture["venv_backup"])
    assert payload["predecessor_recovery"]["role"] == "rollback_base_for_next_generation"

    # The failed generation and archived substrate are left exactly in place.
    assert (layout.source / "broken").exists()
    assert (fixture["source_backup"] / "marker").exists()
    assert (fixture["venv_backup"] / "marker").exists()

    # The durable adoption receipt is owner-private.
    metadata = layout.adoption_receipt.lstat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600


def test_retry_is_idempotent(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    first = module.adopt(
        layout,
        retain_forward_receipt=fixture["receipt_path"],
        rollback_intent=fixture["intent_path"],
    )
    receipt_before = layout.adoption_receipt.read_bytes()

    second = module.adopt(
        layout,
        retain_forward_receipt=fixture["receipt_path"],
        rollback_intent=fixture["intent_path"],
    )

    assert second == first
    # A retry never rewrites the durable commit marker.
    assert layout.adoption_receipt.read_bytes() == receipt_before


def test_retry_rejects_conflicting_failed_generation(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    module.adopt(
        layout,
        retain_forward_receipt=fixture["receipt_path"],
        rollback_intent=fixture["intent_path"],
    )

    # A second, different failed generation must not silently rebind the receipt.
    other_source_backup = mac_home / "backups" / "mac-src.agent_jordanh-worker4.OTHER"
    other_venv_backup = mac_home / "backups" / "venv.agent_jordanh-worker4.OTHER"
    for archive in (other_source_backup, other_venv_backup):
        archive.mkdir(parents=True)
        (archive / "marker").write_text("x\n")
        os.chmod(archive, 0o700)
    other_intent = mac_home / "logs" / "rollback-OTHER-intent.json"
    other_sha = _write_private_json(
        other_intent,
        {
            "schema": module.ROLLBACK_INTENT_SCHEMA,
            "status": "armed",
            "agent": "agent_jordanh-worker4",
            "generation": "20260724T000000Z",
            "revision": "b" * 40,
            "prior_generation": ONBOARDING_GENERATION,
            "prior_revision": None,
            "rollback_capable": True,
            "artifacts": {
                "source": {"path": str(layout.source), "backup": str(other_source_backup)},
                "venv": {"path": str(layout.venv), "backup": str(other_venv_backup)},
            },
        },
    )
    other_receipt = mac_home / "logs" / "retain-forward-OTHER.json"
    _write_private_json(
        other_receipt,
        {
            "schema": module.RETAIN_FORWARD_RECEIPT_SCHEMA,
            "status": "retained_forward",
            "recovery_policy": "retain-forward",
            "rolled_back": False,
            "agent": "agent_jordanh-worker4",
            "failed_generation": "20260724T000000Z",
            "rollback_intent": {"path": str(other_intent), "sha256": other_sha},
        },
    )

    with pytest.raises(module.AdoptionError, match="different failed generation"):
        module.adopt(
            layout,
            retain_forward_receipt=other_receipt,
            rollback_intent=other_intent,
        )


def test_rejects_missing_archived_backup(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    # The archived phase-zero source is gone: there is no rollback base to adopt.
    import shutil

    shutil.rmtree(fixture["source_backup"])

    with pytest.raises(module.AdoptionError, match="archived phase-zero source"):
        module.adopt(
            layout,
            retain_forward_receipt=fixture["receipt_path"],
            rollback_intent=fixture["intent_path"],
        )
    assert not layout.adoption_receipt.exists()


def test_rejects_receipt_not_sealing_this_intent(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    # Corrupt the receipt's sealed digest so it no longer binds the intent.
    receipt = json.loads(fixture["receipt_path"].read_bytes())
    receipt["rollback_intent"]["sha256"] = "0" * 64
    _write_private_json(fixture["receipt_path"], receipt)

    with pytest.raises(module.AdoptionError, match="does not seal this exact"):
        module.adopt(
            layout,
            retain_forward_receipt=fixture["receipt_path"],
            rollback_intent=fixture["intent_path"],
        )


def test_rejects_rolled_back_receipt(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    receipt = json.loads(fixture["receipt_path"].read_bytes())
    receipt["rolled_back"] = True
    _write_private_json(fixture["receipt_path"], receipt)

    with pytest.raises(module.AdoptionError, match="rollback occurred"):
        module.adopt(
            layout,
            retain_forward_receipt=fixture["receipt_path"],
            rollback_intent=fixture["intent_path"],
        )


def test_rejects_predecessor_not_phase_zero(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    intent = json.loads(fixture["intent_path"].read_bytes())
    intent["prior_generation"] = "some-other-generation"
    intent_sha = _write_private_json(fixture["intent_path"], intent)
    receipt = json.loads(fixture["receipt_path"].read_bytes())
    receipt["rollback_intent"]["sha256"] = intent_sha
    _write_private_json(fixture["receipt_path"], receipt)

    with pytest.raises(module.AdoptionError, match="phase-zero onboarding generation"):
        module.adopt(
            layout,
            retain_forward_receipt=fixture["receipt_path"],
            rollback_intent=fixture["intent_path"],
        )


def test_rejects_world_writable_backup(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    os.chmod(fixture["venv_backup"], 0o777)

    with pytest.raises(module.AdoptionError, match="writable by group or other"):
        module.adopt(
            layout,
            retain_forward_receipt=fixture["receipt_path"],
            rollback_intent=fixture["intent_path"],
        )


def test_refuses_when_live_path_is_the_archive(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    # Simulate someone having re-activated the archive as the live source via a
    # symlink: adoption must refuse to treat the archive as a live generation.
    import shutil

    shutil.rmtree(layout.source)
    layout.source.symlink_to(fixture["source_backup"])

    with pytest.raises(module.AdoptionError, match="symlink into the archive"):
        module.adopt(
            layout,
            retain_forward_receipt=fixture["receipt_path"],
            rollback_intent=fixture["intent_path"],
        )


def test_rejects_missing_receipt_file(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    with pytest.raises(module.AdoptionError, match="missing"):
        module.adopt(
            layout,
            retain_forward_receipt=mac_home / "logs" / "absent.json",
            rollback_intent=fixture["intent_path"],
        )


def test_inspect_reports_eligible_then_adopted(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)
    layout = fixture["layout"]

    before = module.inspect(layout)
    assert before["schema"] == module.STATUS_SCHEMA
    assert before["status"] == "eligible"
    assert before["checks"]["onboarding_receipt_present"] is True
    assert before["checks"]["adoption_receipt_present"] is False

    module.adopt(
        layout,
        retain_forward_receipt=fixture["receipt_path"],
        rollback_intent=fixture["intent_path"],
    )

    after = module.inspect(layout)
    assert after["status"] == "adopted"
    assert after["checks"]["adoption_receipt_present"] is True


def test_cli_adopt_and_inspect_roundtrip(module, tmp_path):
    home = tmp_path / "home"
    mac_home = home / ".mac"
    fixture = _seed_first_deploy_failure(module, home, mac_home)

    rc = module.main(
        [
            "adopt",
            "--home",
            str(home),
            "--mac-home",
            str(mac_home),
            "--retain-forward-receipt",
            str(fixture["receipt_path"]),
            "--rollback-intent",
            str(fixture["intent_path"]),
        ]
    )
    assert rc == 0

    rc = module.main(["inspect", "--home", str(home), "--mac-home", str(mac_home)])
    assert rc == 0
