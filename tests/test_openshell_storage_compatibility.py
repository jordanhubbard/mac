from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "openshell" / "storage-compatibility.py"
CONTROLLER = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _module():
    spec = importlib.util.spec_from_file_location("openshell_storage_compatibility", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _field(number: int, wire_type: int, value: bytes | int) -> bytes:
    module = _module()
    key = module.encode_varint((number << 3) | wire_type)
    if wire_type == 0:
        assert isinstance(value, int)
        return key + module.encode_varint(value)
    assert wire_type == 2 and isinstance(value, bytes)
    return key + module.encode_varint(len(value)) + value


def _sandbox(spec: bytes) -> bytes:
    return _field(1, 2, b"metadata") + _field(2, 2, spec) + _field(3, 2, b"status")


def test_legacy_gpu_true_becomes_resource_requirements_message() -> None:
    module = _module()
    unrelated = _field(2, 2, _field(9, 0, 1))
    old = _sandbox(unrelated + _field(9, 0, 1) + _field(12, 0, 7))

    new, changed = module.rewrite_sandbox_payload(old)

    assert changed is True
    spec = [field for field in module.parse_fields(new) if field.number == 2][0]
    spec_raw = new[spec.value_start : spec.end]
    field9 = [field for field in module.parse_fields(spec_raw) if field.number == 9]
    assert len(field9) == 1 and field9[0].wire_type == 2
    assert spec_raw[field9[0].value_start : field9[0].end] == b"\x0a\x00"
    assert unrelated in spec_raw
    assert _field(12, 0, 7) in spec_raw


def test_legacy_gpu_false_is_removed_as_the_new_default() -> None:
    module = _module()
    old = _sandbox(_field(4, 2, b"image") + _field(9, 0, 0))

    new, changed = module.rewrite_sandbox_payload(old)

    assert changed is True
    spec = [field for field in module.parse_fields(new) if field.number == 2][0]
    spec_raw = new[spec.value_start : spec.end]
    assert [field for field in module.parse_fields(spec_raw) if field.number == 9] == []
    assert _field(4, 2, b"image") in spec_raw


def test_current_resource_requirements_encoding_is_byte_stable() -> None:
    module = _module()
    current = _sandbox(_field(9, 2, _field(1, 2, b"")))

    rewritten, changed = module.rewrite_sandbox_payload(current)

    assert changed is False
    assert rewritten == current


@pytest.mark.parametrize(
    "spec",
    [
        _field(9, 0, 1) + _field(9, 2, b""),
        _field(9, 0, 2),
        bytes([(9 << 3) | 5]) + b"\0\0\0\0",
    ],
)
def test_ambiguous_or_unknown_field9_fails_closed(spec: bytes) -> None:
    module = _module()
    with pytest.raises(module.CompatibilityError):
        module.rewrite_sandbox_payload(_sandbox(spec))


def _database(path: Path, rows: list[tuple[str, str, str, bytes]]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE objects (id TEXT PRIMARY KEY, object_type TEXT, name TEXT, payload BLOB)"
        )
        connection.executemany(
            "INSERT INTO objects(id, object_type, name, payload) VALUES (?, ?, ?, ?)",
            rows,
        )
    path.chmod(0o600)


def test_sqlite_migration_changes_only_legacy_sandbox_payloads(tmp_path: Path) -> None:
    module = _module()
    database = tmp_path / "openshell.db"
    legacy = _sandbox(_field(4, 2, b"old") + _field(9, 0, 1))
    current = _sandbox(_field(4, 2, b"new") + _field(9, 2, _field(1, 2, b"")))
    policy = b"\x48\x01"
    _database(
        database,
        [
            ("legacy", "sandbox", "old-sandbox", legacy),
            ("current", "sandbox", "new-sandbox", current),
            ("policy", "policy", "unrelated", policy),
        ],
    )
    before = module.inspect_database(database)
    backup, backup_sha = module.create_backup(database, tmp_path / "backups")

    result = module.migrate_database(database)

    assert before["legacy_count"] == 1
    assert result["migrated_count"] == 1
    assert result["after"]["legacy_count"] == 0
    assert backup.stat().st_mode & 0o777 == 0o600
    assert len(backup_sha) == 64
    with sqlite3.connect(database) as connection:
        values = dict(connection.execute("SELECT id, payload FROM objects"))
    assert values["current"] == current
    assert values["policy"] == policy
    assert values["legacy"] != legacy


def test_sqlite_migration_rolls_back_every_row_on_malformed_payload(
    tmp_path: Path,
) -> None:
    module = _module()
    database = tmp_path / "openshell.db"
    legacy = _sandbox(_field(9, 0, 1))
    malformed = _field(1, 2, b"metadata") + b"\x12\xff"
    _database(
        database,
        [
            ("legacy", "sandbox", "old-sandbox", legacy),
            ("malformed", "sandbox", "bad-sandbox", malformed),
        ],
    )

    with pytest.raises(module.CompatibilityError):
        module.migrate_database(database)

    with sqlite3.connect(database) as connection:
        values = dict(connection.execute("SELECT id, payload FROM objects"))
    assert values["legacy"] == legacy
    assert values["malformed"] == malformed


def test_compatible_database_with_legacy_backup_requires_receipt_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    home = tmp_path / "home"
    home.mkdir()
    old_database = tmp_path / "old.db"
    database = tmp_path / "openshell.db"
    legacy = _sandbox(_field(9, 0, 1))
    current, changed = module.rewrite_sandbox_payload(legacy)
    assert changed is True
    _database(old_database, [("sandbox", "sandbox", "worker", legacy)])
    _database(database, [("sandbox", "sandbox", "worker", current)])
    module.create_backup(old_database, home / ".mac/logs/openshell-storage-migrations")

    preflight = module.preflight(home, database, "linux", "a" * 64)

    assert preflight["status"] == "proof_required"
    assert preflight["recovery_legacy_count"] == 1

    class Manager:
        kind = "systemd-user"
        stopped = False
        started = False

        @classmethod
        def active(cls) -> bool:
            return True

        @classmethod
        def stop(cls) -> None:
            cls.stopped = True

        @classmethod
        def start(cls) -> None:
            cls.started = True

    monkeypatch.setattr(module, "detect_gateway_manager", lambda *_: Manager())
    monkeypatch.setattr(module, "wait_for_gateway_endpoint", lambda: None)
    monkeypatch.setattr(module, "prove_inventory", lambda _home: 1)

    result = module.migrate(home, database, "linux", "a" * 64)

    assert result["recovered_pending_proof"] is True
    assert result["migrated_count"] == 1
    assert Manager.stopped is True
    assert Manager.started is True
    receipt = module.receipt_path(home)
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert module.preflight(home, database, "linux", "a" * 64)["status"] == "ready"


def test_controller_keeps_storage_repair_outside_the_cohort_transaction() -> None:
    text = CONTROLLER.read_text(encoding="utf-8")
    cohort = text.split("run_typed_cohort() {", 1)[1].split("\n}\n\n", 1)[0]
    assert cohort.index("classify_reviewed_openshell_cli_prerequisites") < cohort.index(
        "classify_openshell_storage_prerequisites"
    )
    assert cohort.index("classify_openshell_storage_prerequisites") < cohort.index(
        "phase1-prepare-start"
    )
    assert "prepare_openshell_storage_prerequisites" not in cohort
    main = text.split("main() {", 1)[1]
    explicit = main.split('if [ "$PREPARE_REVIEWED_OPENSHELL_CLI" = 1 ]; then', 1)[1]
    explicit = explicit.split("\n  fi", 1)[0]
    assert explicit.index("prepare_reviewed_openshell_cli_prerequisites") < explicit.index(
        "prepare_openshell_storage_prerequisites"
    )


def test_optional_execution_does_not_skip_existing_storage_classification() -> None:
    text = CONTROLLER.read_text(encoding="utf-8")
    helper = text.split("run_remote_openshell_storage_helper() {", 1)[1].split("\n}\n", 1)[0]
    assert "openshell_not_required" not in helper
    assert 'command="python3 - ' in helper
    assert helper.index('helper_sha256="$(sha256_file') < helper.index('command="python3 - ')
    preparation = text.split("prepare_openshell_storage_prerequisites() {", 1)[1].split("\n}\n", 1)[
        0
    ]
    assert '[ "$status" = proof_required ]' in preparation


def test_helper_is_repository_owned_and_executable() -> None:
    assert HELPER.is_file()
    assert os.access(HELPER, os.X_OK)
