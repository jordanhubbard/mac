#!/usr/bin/env python3
"""Detect and repair the OpenShell SandboxSpec field-9 wire migration.

OpenShell v0.0.69 reused SandboxSpec field 9 for a message after releases
through v0.0.68 had encoded that field as a boolean.  A current gateway cannot
enumerate a database containing the old varint.  This helper keeps detection
read-only and makes the repair an explicit, backup-first operation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA = "mac.openshell_storage_compatibility.v1"
RECEIPT_SCHEMA = "mac.openshell_storage_migration.v1"
MAX_OUTPUT = 8 * 1024 * 1024

# Top-level SandboxLifecycle wire layout: field 1 metadata, field 2 SandboxSpec,
# field 3 lifecycle status.  A status submessage carries the lifecycle phase as
# its own field 1 enum.  Provisioning/Error rows are recorded before a spec is
# attached, so a cleanly parsing row with zero field-2 specs is a valid no-spec
# lifecycle row rather than corruption or a legacy field-9 spec.
SANDBOX_STATUS_FIELD = 3
SANDBOX_PHASE_FIELD = 1
UNKNOWN_PHASE = "unknown"
SANDBOX_PHASE_LABELS = {
    0: "unspecified",
    1: "provisioning",
    2: "running",
    3: "ready",
    4: "error",
    5: "terminating",
    6: "terminated",
}


class CompatibilityError(RuntimeError):
    """The database or its service ownership could not be proved safely."""


@dataclass(frozen=True)
class WireField:
    number: int
    wire_type: int
    start: int
    key_end: int
    value_start: int
    end: int
    varint: int | None = None


def read_varint(raw: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if offset >= len(raw):
            raise CompatibilityError("truncated protobuf varint")
        octet = raw[offset]
        offset += 1
        value |= (octet & 0x7F) << (index * 7)
        if not octet & 0x80:
            return value, offset
    raise CompatibilityError("oversized protobuf varint")


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varints must be non-negative")
    output = bytearray()
    while True:
        octet = value & 0x7F
        value >>= 7
        output.append(octet | (0x80 if value else 0))
        if not value:
            return bytes(output)


def parse_fields(raw: bytes) -> list[WireField]:
    fields: list[WireField] = []
    offset = 0
    while offset < len(raw):
        start = offset
        key, offset = read_varint(raw, offset)
        number, wire_type = key >> 3, key & 7
        if number <= 0:
            raise CompatibilityError("protobuf field number is invalid")
        key_end = offset
        if wire_type == 0:
            value, offset = read_varint(raw, offset)
            fields.append(
                WireField(number, wire_type, start, key_end, key_end, offset, value)
            )
        elif wire_type == 1:
            end = offset + 8
            if end > len(raw):
                raise CompatibilityError("truncated fixed64 protobuf field")
            fields.append(WireField(number, wire_type, start, key_end, offset, end))
            offset = end
        elif wire_type == 2:
            length, value_start = read_varint(raw, offset)
            end = value_start + length
            if end > len(raw):
                raise CompatibilityError("truncated length-delimited protobuf field")
            fields.append(
                WireField(number, wire_type, start, key_end, value_start, end)
            )
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(raw):
                raise CompatibilityError("truncated fixed32 protobuf field")
            fields.append(WireField(number, wire_type, start, key_end, offset, end))
            offset = end
        else:
            raise CompatibilityError("unsupported protobuf group wire type")
    return fields


def rewrite_sandbox_spec(raw: bytes) -> tuple[bytes, bool]:
    fields = [field for field in parse_fields(raw) if field.number == 9]
    if not fields:
        return raw, False
    if len(fields) != 1:
        raise CompatibilityError("SandboxSpec field 9 is ambiguous")
    field = fields[0]
    if field.wire_type == 2:
        # Validate the nested ResourceRequirements message before accepting it.
        parse_fields(raw[field.value_start : field.end])
        return raw, False
    if field.wire_type != 0 or field.varint not in {0, 1}:
        raise CompatibilityError("SandboxSpec field 9 has an unsupported legacy value")
    replacement = b""
    if field.varint == 1:
        # ResourceRequirements { gpu: GpuResourceRequirements {} }
        body = b"\x0a\x00"
        replacement = encode_varint((9 << 3) | 2) + encode_varint(len(body)) + body
    return raw[: field.start] + replacement + raw[field.end :], True


def sandbox_lifecycle_phase(raw: bytes) -> str:
    """Return the lifecycle phase label for a well-formed sandbox payload.

    The read stays read-only and fail-open: the top-level status field
    (:data:`SANDBOX_STATUS_FIELD`) is decoded when it is present exactly once,
    and its phase enum (:data:`SANDBOX_PHASE_FIELD`) is mapped to a stable
    label.  Anything that cannot be resolved to a phase buckets under
    :data:`UNKNOWN_PHASE` rather than raising, so classification never fails a
    row that already parsed cleanly.
    """

    def label_for(number: int) -> str:
        return SANDBOX_PHASE_LABELS.get(number, f"phase_{number}")

    statuses = [
        field for field in parse_fields(raw) if field.number == SANDBOX_STATUS_FIELD
    ]
    if len(statuses) != 1:
        return UNKNOWN_PHASE
    status = statuses[0]
    if status.wire_type == 0:
        return label_for(status.varint if status.varint is not None else 0)
    if status.wire_type != 2:
        return UNKNOWN_PHASE
    try:
        nested = parse_fields(raw[status.value_start : status.end])
    except CompatibilityError:
        return UNKNOWN_PHASE
    phases = [field for field in nested if field.number == SANDBOX_PHASE_FIELD]
    if len(phases) != 1 or phases[0].wire_type != 0 or phases[0].varint is None:
        return UNKNOWN_PHASE
    return label_for(phases[0].varint)


def rewrite_sandbox_payload(raw: bytes) -> tuple[bytes, bool]:
    specs = [field for field in parse_fields(raw) if field.number == 2]
    if not specs:
        # A cleanly parsing lifecycle row with no field-2 SandboxSpec is a
        # valid no-spec row (Provisioning/Error shape) recorded before a spec
        # is attached.  It is not corruption and not a legacy field-9 spec, so
        # leave it untouched and let the caller inventory it by phase.
        return raw, False
    if len(specs) != 1 or specs[0].wire_type != 2:
        raise CompatibilityError("sandbox payload spec is ambiguous")
    spec = specs[0]
    rewritten, changed = rewrite_sandbox_spec(raw[spec.value_start : spec.end])
    if not changed:
        return raw, False
    replacement = (
        encode_varint((2 << 3) | 2) + encode_varint(len(rewritten)) + rewritten
    )
    return raw[: spec.start] + replacement + raw[spec.end :], True


def payload_has_spec(raw: bytes) -> bool:
    """Report whether a well-formed sandbox payload carries a field-2 spec."""

    return any(field.number == 2 for field in parse_fields(raw))


def require_private_regular(
    path: Path, *, exact_mode: int | None = None
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CompatibilityError(f"required path is unavailable: {path}") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise CompatibilityError(f"required path is not an owned regular file: {path}")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise CompatibilityError(f"required path has an unsafe mode: {path}")
    return metadata


def resolve_database(home: Path, explicit: str | None, expected_os: str) -> Path | None:
    if explicit:
        candidates = [Path(explicit).expanduser()]
    elif expected_os == "darwin":
        candidates = [
            home / ".mac/openshell/ghome/.local/state/openshell/gateway/openshell.db",
            home / ".local/state/openshell/gateway/openshell.db",
        ]
    else:
        state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
        candidates = [state_home / "openshell/gateway/openshell.db"]
    existing = [path for path in candidates if path.exists() or path.is_symlink()]
    if len(existing) > 1:
        raise CompatibilityError("multiple OpenShell gateway databases are present")
    return existing[0] if existing else None


def open_database(path: Path, *, readonly: bool) -> sqlite3.Connection:
    require_private_regular(path, exact_mode=0o600)
    if readonly:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(str(path), timeout=30)
    return connection


def sandbox_rows(connection: sqlite3.Connection) -> list[tuple[str, str, bytes]]:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(objects)").fetchall()
    }
    if not {"id", "name", "object_type", "payload"}.issubset(columns):
        raise CompatibilityError("OpenShell objects table has an unsupported schema")
    values: list[tuple[str, str, bytes]] = []
    for object_id, name, payload in connection.execute(
        "SELECT id, name, payload FROM objects "
        "WHERE object_type = 'sandbox' ORDER BY id"
    ):
        if not isinstance(object_id, str) or not isinstance(name, str):
            raise CompatibilityError("OpenShell sandbox identity is malformed")
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if not isinstance(payload, bytes):
            raise CompatibilityError("OpenShell sandbox payload is not binary")
        values.append((object_id, name, payload))
    return values


def inspect_rows(rows: Iterable[tuple[str, str, bytes]]) -> dict[str, object]:
    count = 0
    legacy: list[tuple[str, str, bytes, bytes]] = []
    nospec_by_phase: dict[str, int] = {}
    digest = hashlib.sha256()
    for object_id, name, payload in rows:
        count += 1
        digest.update(object_id.encode("utf-8") + b"\0")
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(payload).digest())
        rewritten, changed = rewrite_sandbox_payload(payload)
        if changed:
            legacy.append((object_id, name, payload, rewritten))
        elif not payload_has_spec(payload):
            phase = sandbox_lifecycle_phase(payload)
            nospec_by_phase[phase] = nospec_by_phase.get(phase, 0) + 1
    nospec_phases = {phase: nospec_by_phase[phase] for phase in sorted(nospec_by_phase)}
    return {
        "sandbox_count": count,
        "legacy_count": len(legacy),
        "nospec_count": sum(nospec_phases.values()),
        "nospec_phases": nospec_phases,
        "inventory_sha256": digest.hexdigest(),
        "legacy": legacy,
    }


def inspect_database(path: Path) -> dict[str, object]:
    with open_database(path, readonly=True) as connection:
        return inspect_rows(sandbox_rows(connection))


def run_command(
    argv: list[str], *, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompatibilityError(f"could not run bounded command: {argv[0]}") from exc


class GatewayManager:
    def __init__(self, kind: str, command: list[str], home: Path) -> None:
        self.kind = kind
        self.command = command
        self.home = home

    def active(self) -> bool:
        if self.kind == "systemd-user":
            return (
                run_command(
                    self.command + ["is-active", "--quiet", "openshell-gateway.service"]
                ).returncode
                == 0
            )
        if self.kind == "supervisord":
            result = run_command(self.command + ["status", "openshell-gateway"])
            return result.returncode == 0 and "RUNNING" in result.stdout
        result = run_command(
            self.command
            + ["ps", "--filter", "name=^/openshell-gw$", "--format", "{{.Status}}"]
        )
        return result.returncode == 0 and any(
            line.startswith("Up ") for line in result.stdout.splitlines()
        )

    def stop(self) -> None:
        action = "stop"
        target = (
            "openshell-gateway.service"
            if self.kind == "systemd-user"
            else "openshell-gateway"
        )
        if self.kind == "docker":
            target = "openshell-gw"
        result = run_command(self.command + [action, target], timeout=45)
        if result.returncode != 0:
            raise CompatibilityError("reviewed OpenShell gateway could not be stopped")
        deadline = time.monotonic() + 30
        while self.active() and time.monotonic() < deadline:
            time.sleep(0.25)
        if self.active():
            raise CompatibilityError("reviewed OpenShell gateway did not stop")

    def start(self) -> None:
        target = (
            "openshell-gateway.service"
            if self.kind == "systemd-user"
            else "openshell-gateway"
        )
        if self.kind == "docker":
            target = "openshell-gw"
        result = run_command(self.command + ["start", target], timeout=45)
        if result.returncode != 0:
            raise CompatibilityError("reviewed OpenShell gateway could not be started")
        deadline = time.monotonic() + 30
        while not self.active() and time.monotonic() < deadline:
            time.sleep(0.25)
        if not self.active():
            raise CompatibilityError("reviewed OpenShell gateway did not become active")


def require_gateway_wrapper(home: Path) -> Path:
    wrapper = home / ".mac/openshell/run-gateway.sh"
    require_private_regular(wrapper, exact_mode=0o700)
    expected = f'exec "{home}/.local/bin/openshell-gateway" --config "{home}/.mac/openshell/gateway.toml"'
    text = wrapper.read_text(encoding="utf-8", errors="strict")
    if expected not in text:
        raise CompatibilityError(
            "OpenShell gateway wrapper is not the reviewed MAC identity"
        )
    return wrapper


def detect_gateway_manager(home: Path, expected_os: str) -> GatewayManager:
    if expected_os == "darwin":
        docker = shutil.which("docker")
        if not docker:
            raise CompatibilityError("Docker is unavailable for the managed gateway")
        inspect = run_command(
            [
                docker,
                "inspect",
                "--format",
                '{{ index .Config.Labels "mac.owner" }}:{{ index .Config.Labels "mac.kind" }}',
                "openshell-gw",
            ]
        )
        if inspect.returncode != 0 or inspect.stdout.strip() != "mac:openshell-gateway":
            raise CompatibilityError(
                "Docker gateway does not have the reviewed MAC identity"
            )
        return GatewayManager("docker", [docker], home)

    wrapper = require_gateway_wrapper(home)
    unit = home / ".config/systemd/user/openshell-gateway.service"
    if unit.exists() or unit.is_symlink():
        require_private_regular(unit)
        text = unit.read_text(encoding="utf-8", errors="strict")
        if (
            "ExecStart=%h/.mac/openshell/run-gateway.sh" not in text
            and f"ExecStart={wrapper}" not in text
        ):
            raise CompatibilityError(
                "systemd gateway unit does not select the reviewed wrapper"
            )
        systemctl = shutil.which("systemctl")
        if not systemctl:
            raise CompatibilityError("systemctl is unavailable for the managed gateway")
        return GatewayManager("systemd-user", [systemctl, "--user"], home)

    config = Path("/etc/supervisor/conf.d/openshell-gateway.conf")
    if config.exists() or config.is_symlink():
        metadata = config.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or config.is_symlink()
            or metadata.st_uid != 0
        ):
            raise CompatibilityError("supervisord gateway configuration is untrusted")
        text = config.read_text(encoding="utf-8", errors="strict")
        if (
            f"command={wrapper}" not in text
            or 'MAC_OPENSH_GATEWAY_OWNER="mac"' not in text
        ):
            raise CompatibilityError(
                "supervisord gateway does not select the reviewed wrapper"
            )
        sudo = shutil.which("sudo")
        supervisorctl = shutil.which("supervisorctl")
        if not sudo or not supervisorctl:
            raise CompatibilityError("supervisor control is unavailable")
        return GatewayManager("supervisord", [sudo, "-n", supervisorctl], home)
    raise CompatibilityError("reviewed OpenShell gateway manager is unavailable")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_backup(database: Path, backup_dir: Path) -> tuple[Path, str]:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = backup_dir.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or backup_dir.is_symlink()
    ):
        raise CompatibilityError("OpenShell migration backup directory is untrusted")
    os.chmod(backup_dir, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = backup_dir / f"openshell-before-field9-{stamp}-{os.getpid()}.db"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".backup-", dir=backup_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with (
            open_database(database, readonly=True) as source,
            sqlite3.connect(str(temporary)) as target,
        ):
            source.backup(target)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(final)
        fsync_directory(backup_dir)
        digest = hashlib.sha256(final.read_bytes()).hexdigest()
        return final, digest
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def migrate_database(database: Path) -> dict[str, object]:
    connection = open_database(database, readonly=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        before = inspect_rows(sandbox_rows(connection))
        legacy = before.pop("legacy")
        for object_id, _name, old, new in legacy:
            cursor = connection.execute(
                "UPDATE objects SET payload = ? WHERE id = ? AND payload = ?",
                (new, object_id, old),
            )
            if cursor.rowcount != 1:
                raise CompatibilityError("sandbox row changed during the migration")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(FULL)")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    after = inspect_database(database)
    after.pop("legacy")
    if after["legacy_count"] != 0 or after["sandbox_count"] != before["sandbox_count"]:
        raise CompatibilityError("OpenShell storage migration did not converge")
    return {"migrated_count": len(legacy), "before": before, "after": after}


def prove_inventory(home: Path) -> int:
    cli = home / ".mac/bin/openshell"
    require_private_regular(cli, exact_mode=0o700)
    environment = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "OPENSHELL_GATEWAY_ENDPOINT": "http://127.0.0.1:17670",
    }
    offset = 0
    count = 0
    while True:
        try:
            result = subprocess.run(
                [
                    str(cli),
                    "sandbox",
                    "list",
                    "--limit",
                    "1000",
                    "--offset",
                    str(offset),
                    "--output",
                    "json",
                ],
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=15,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CompatibilityError(
                "reviewed OpenShell inventory proof could not run"
            ) from exc
        if result.returncode != 0 or len(result.stdout.encode("utf-8")) > MAX_OUTPUT:
            raise CompatibilityError("reviewed OpenShell inventory proof failed")
        try:
            value = json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise CompatibilityError(
                "reviewed OpenShell inventory proof is malformed"
            ) from exc
        if not isinstance(value, list) or any(
            not isinstance(item, dict) for item in value
        ):
            raise CompatibilityError(
                "reviewed OpenShell inventory proof is not a list of objects"
            )
        count += len(value)
        if len(value) < 1000:
            return count
        offset += len(value)


def wait_for_gateway_endpoint(timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 17670), timeout=0.25):
                return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise CompatibilityError(
                    "reviewed OpenShell gateway endpoint did not become ready"
                ) from exc
            time.sleep(0.1)


def atomic_receipt(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def receipt_path(home: Path) -> Path:
    return home / ".mac/logs/openshell-storage-migrations/latest.json"


def current_receipt_is_valid(home: Path) -> bool:
    path = receipt_path(home)
    if not path.exists() and not path.is_symlink():
        return False
    require_private_regular(path, exact_mode=0o600)
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CompatibilityError(
            "OpenShell storage migration receipt is malformed"
        ) from exc
    if not isinstance(value, dict):
        raise CompatibilityError("OpenShell storage migration receipt is malformed")
    return (
        value.get("schema") == RECEIPT_SCHEMA
        and value.get("status") == "migrated"
        and isinstance(value.get("backup_sha256"), str)
        and len(value["backup_sha256"]) == 64
    )


def pending_migration_backup(
    home: Path, current: dict[str, object]
) -> tuple[Path, str, dict[str, object]] | None:
    backup_dir = home / ".mac/logs/openshell-storage-migrations"
    if not backup_dir.exists() and not backup_dir.is_symlink():
        return None
    metadata = backup_dir.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or backup_dir.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CompatibilityError("OpenShell migration backup directory is untrusted")
    for backup in sorted(
        backup_dir.glob("openshell-before-field9-*.db"),
        key=lambda path: path.name,
        reverse=True,
    ):
        require_private_regular(backup, exact_mode=0o600)
        before = inspect_database(backup)
        before.pop("legacy")
        if (
            int(before["legacy_count"]) > 0
            and before["sandbox_count"] == current["sandbox_count"]
        ):
            return backup, hashlib.sha256(backup.read_bytes()).hexdigest(), before
    return None


def preflight(
    home: Path,
    database: Path | None,
    expected_os: str,
    controller_sha256: str = "",
) -> dict[str, object]:
    if database is None:
        value: dict[str, object] = {
            "schema": SCHEMA,
            "status": "ready",
            "reason": "storage_absent",
            "expected_os": expected_os,
            "sandbox_count": 0,
            "legacy_count": 0,
            "nospec_count": 0,
            "nospec_phases": {},
        }
        if controller_sha256:
            value["helper_sha256"] = controller_sha256
        return value
    inspected = inspect_database(database)
    inspected.pop("legacy")
    database_sha256 = hashlib.sha256(database.read_bytes()).hexdigest()
    pending = None
    if not inspected["legacy_count"] and not current_receipt_is_valid(home):
        pending = pending_migration_backup(home, inspected)
    if pending is not None:
        backup, backup_sha256, before = pending
        value = {
            "schema": SCHEMA,
            "status": "proof_required",
            "reason": "compatible_storage_lacks_migration_receipt",
            "expected_os": expected_os,
            "database_sha256": database_sha256,
            "recovery_backup_path": str(backup),
            "recovery_backup_sha256": backup_sha256,
            "recovery_legacy_count": before["legacy_count"],
            **inspected,
        }
        if controller_sha256:
            value["helper_sha256"] = controller_sha256
        return value
    value = {
        "schema": SCHEMA,
        "status": "migration_required" if inspected["legacy_count"] else "ready",
        "reason": "legacy_sandbox_spec_field9"
        if inspected["legacy_count"]
        else "storage_compatible",
        "expected_os": expected_os,
        **inspected,
    }
    if controller_sha256:
        value["helper_sha256"] = controller_sha256
    return value


def migrate(
    home: Path,
    database: Path | None,
    expected_os: str,
    controller_sha256: str = "",
) -> dict[str, object]:
    initial = preflight(home, database, expected_os, controller_sha256)
    if initial["status"] == "ready":
        return initial
    if database is None:
        raise CompatibilityError("migration-required storage is unavailable")
    manager = detect_gateway_manager(home, expected_os)
    if not manager.active():
        raise CompatibilityError(
            "reviewed OpenShell gateway is not active before migration"
        )
    if initial["status"] == "proof_required":
        # A prior attempt can commit the database rewrite, publish a newer
        # gateway binary, and then fail before proving inventory.  The running
        # process may still be the old inode.  Reload the exact managed binary
        # before proof so crash-resume cannot repeatedly exercise stale code.
        manager.stop()
        manager.start()
        wait_for_gateway_endpoint()
        inventory_count = prove_inventory(home)
        completed = {
            "schema": RECEIPT_SCHEMA,
            "status": "migrated",
            "reason": "legacy_sandbox_spec_field9_rewritten",
            "expected_os": expected_os,
            "database_sha256": initial["database_sha256"],
            "backup_path": initial["recovery_backup_path"],
            "backup_sha256": initial["recovery_backup_sha256"],
            "gateway_manager": manager.kind,
            "inventory_count": inventory_count,
            "helper_sha256": controller_sha256,
            "migrated_count": initial["recovery_legacy_count"],
            "recovered_pending_proof": True,
            "receipt_path": str(receipt_path(home)),
        }
        atomic_receipt(receipt_path(home), completed)
        return completed
    start_error: Exception | None = None
    try:
        manager.stop()
        backup, backup_sha = create_backup(
            database, home / ".mac/logs/openshell-storage-migrations"
        )
        migration = migrate_database(database)
    finally:
        if not manager.active():
            try:
                manager.start()
            except Exception as exc:  # keep the migrated generation for in-place repair
                start_error = exc
    if start_error is not None:
        raise CompatibilityError(
            "storage migrated but the gateway restart failed"
        ) from start_error
    wait_for_gateway_endpoint()
    inventory_count = prove_inventory(home)
    completed = {
        "schema": RECEIPT_SCHEMA,
        "status": "migrated",
        "reason": "legacy_sandbox_spec_field9_rewritten",
        "expected_os": expected_os,
        "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "backup_path": str(backup),
        "backup_sha256": backup_sha,
        "gateway_manager": manager.kind,
        "inventory_count": inventory_count,
        "helper_sha256": controller_sha256,
        "recovered_pending_proof": False,
        **migration,
    }
    receipt = receipt_path(home)
    completed["receipt_path"] = str(receipt)
    atomic_receipt(receipt, completed)
    return completed


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("preflight", "migrate"))
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--expected-os", required=True, choices=("linux", "darwin"))
    parser.add_argument("--database")
    parser.add_argument("--controller-sha256", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if len(args.controller_sha256) != 64 or any(
        value not in "0123456789abcdef" for value in args.controller_sha256
    ):
        print("ERROR: controller helper digest is invalid", file=sys.stderr)
        return 2
    home = Path(args.home).expanduser().resolve()
    try:
        database = resolve_database(home, args.database, args.expected_os)
        value = (
            migrate(home, database, args.expected_os, args.controller_sha256)
            if args.action == "migrate"
            else preflight(home, database, args.expected_os, args.controller_sha256)
        )
    except (CompatibilityError, OSError, sqlite3.Error, UnicodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    json.dump(value, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
