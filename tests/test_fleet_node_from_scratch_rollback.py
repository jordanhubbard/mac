"""Behavioral contract for arming phase-2 rollback on a node with no prior generation.

``arm_phase2_rollback`` used to demand a complete prior generation before it
would arm anything, and ``main`` refuses to reach source replacement unless it
armed.  That made a from-scratch install impossible: the node has no ``SRC_DIR``
and no ``VENV`` by definition, so the precondition could never hold.

The from-scratch contract is *removal*, not restoration -- there is nothing to
restore to, so the armed intent must delete what the failed install created and
leave the node uninstalled.  These tests pin both halves: which starting states
``arm_phase2_rollback`` accepts, and that the rollback program it publishes
actually removes rather than tries to restore.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"

REFUSAL = "phase-2 apply requires a complete rollback-capable prior generation"


def _source() -> str:
    return NODE_INSTALL.read_text(encoding="utf-8")


def _function(name: str, following: str) -> str:
    """Return one function verbatim, delimited by the function that follows it."""
    body = _source().split(f"{name}() {{", 1)[1].split(f"\n}}\n\n{following}() {{", 1)[0]
    return f"{name}() {{{body}\n}}\n"


def _generated_rollback_template() -> str:
    """Return the generated rollback program as it is written, with $ unescaped.

    ``write_rollback_script`` emits the program through an expanding heredoc, so
    everything the program evaluates at *rollback* time is written ``\\$``.
    Undoing that escape yields the program text itself.
    """
    body = _function("write_rollback_script", "verify_phase2_rollback_intent")
    program = body.split('cat > "$rollback_stage" <<EOF\n', 1)[1]
    return program.replace("\\$", "$").replace("\\`", "`")


# --------------------------------------------------------------------------
# arm_phase2_rollback: which starting states are armable
# --------------------------------------------------------------------------


def _arm(
    tmp_path: Path,
    *,
    src: str | None = "directory",
    venv: str | None = "directory",
    require_quiescence: str = "0",
) -> subprocess.CompletedProcess[str]:
    mac_home = tmp_path / "mac-home"
    (mac_home / "backups").mkdir(parents=True)
    src_dir = mac_home / "src" / "mac"
    venv_dir = mac_home / "venv"
    other = tmp_path / "elsewhere"
    other.mkdir()

    for path, kind in ((src_dir, src), (venv_dir, venv)):
        if kind == "directory":
            path.mkdir(parents=True)
        elif kind == "symlink":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(other)
        elif kind == "file":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not a generation\n", encoding="utf-8")
        elif kind is not None:  # pragma: no cover - guards the test itself
            raise AssertionError(f"unsupported artifact kind: {kind}")

    values = {
        "MAC_HOME": str(mac_home),
        "SRC_DIR": str(src_dir),
        "VENV": str(venv_dir),
        "HERMES_DIR": str(mac_home / "hermes-agent"),
        "ROLLBACK_INTENT": str(mac_home / "rollback-intent.json"),
        "AGENT": "rocky",
        "DEPLOY_TS": "20260821T000000Z",
        "MAC_DEPLOY_REQUIRE_PHASE1_QUIESCENCE": require_quiescence,
    }
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + "DEPLOY_ROLLBACK_ARMED=0\nDEPLOY_FROM_SCRATCH=0\n"
        + "SRC_BACKUP=''\nVENV_BACKUP=''\nHERMES_BACKUP=''\nBIN_BACKUP=''\n"
        + "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())
        + "\nlog() { printf 'log %s\\n' \"$*\"; }\n"
        + "die() { printf '%s\\n' \"$*\" >&2; exit 1; }\n"
        + "capture_mutable_runtime_state_for_rollback() { :; }\n"
        + "capture_auxiliary_rollback_artifacts() { :; }\n"
        + "write_rollback_script() { :; }\n"
        + "write_phase2_rollback_intent() { :; }\n"
        + _function("truthy", "detect_supervisor")
        + _function("arm_phase2_rollback", "backup_existing_artifacts")
        + "arm_phase2_rollback\n"
        + 'printf "armed=%s from_scratch=%s src_backup=%s\\n" '
        '"$DEPLOY_ROLLBACK_ARMED" "$DEPLOY_FROM_SCRATCH" "$SRC_BACKUP"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        ["/bin/bash", str(harness)], check=False, capture_output=True, text=True
    )


def test_complete_prior_generation_still_arms_a_restoring_rollback(
    tmp_path: Path,
) -> None:
    result = _arm(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "armed=1 from_scratch=0" in result.stdout
    assert "backups/mac-src.rocky.20260821T000000Z" in result.stdout


def test_node_with_no_prior_generation_arms_a_from_scratch_rollback(
    tmp_path: Path,
) -> None:
    result = _arm(tmp_path, src=None, venv=None)
    assert result.returncode == 0, result.stderr
    assert "armed=1 from_scratch=1" in result.stdout
    assert "no prior generation present" in result.stdout


@pytest.mark.parametrize(
    ("src", "venv"),
    [
        ("directory", None),
        (None, "directory"),
        ("symlink", "directory"),
        ("directory", "symlink"),
        ("file", None),
        (None, "file"),
    ],
)
def test_partial_or_unsafe_prior_generation_is_still_refused(
    tmp_path: Path, src: str | None, venv: str | None
) -> None:
    result = _arm(tmp_path, src=src, venv=venv)
    assert result.returncode != 0
    assert REFUSAL in result.stderr


def test_synchronized_cutover_may_not_claim_the_from_scratch_exemption(
    tmp_path: Path,
) -> None:
    # A deploy that declared it is quiescing a running prior generation is
    # asserting that one exists; it may not then arm a removal rollback.
    result = _arm(tmp_path, src=None, venv=None, require_quiescence="1")
    assert result.returncode != 0
    assert REFUSAL in result.stderr


def test_synchronized_cutover_of_a_complete_prior_generation_still_arms(
    tmp_path: Path,
) -> None:
    result = _arm(tmp_path, require_quiescence="1")
    assert result.returncode == 0, result.stderr
    assert "armed=1 from_scratch=0" in result.stdout


# --------------------------------------------------------------------------
# The published rollback program: prior-absent restores by removal
# --------------------------------------------------------------------------


def _restore(
    tmp_path: Path,
    *,
    state: str,
    make_backup: bool,
    make_destination: bool,
) -> subprocess.CompletedProcess[str]:
    program = _generated_rollback_template()
    mac_home = tmp_path / "mac-home"
    (mac_home / "backups").mkdir(parents=True)
    destination = mac_home / "src" / "mac"
    backup = mac_home / "backups" / "mac-src.rocky.20260821T000000Z"
    if make_destination:
        destination.mkdir(parents=True)
        (destination / "generation").write_text("successor\n", encoding="utf-8")
    if make_backup:
        backup.mkdir(parents=True)
        (backup / "generation").write_text("prior\n", encoding="utf-8")

    harness = tmp_path / "restore.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -eEuo pipefail\n"
        + f"MAC_HOME={shlex.quote(str(mac_home))}\n"
        + "ROLLBACK_TS=20260821T000000Z\n"
        + "ROLLBACK_DIR_COUNT=0\n"
        + "ROLLBACK_DIR_DESTINATIONS=()\n"
        + "ROLLBACK_DIR_CURRENT_BACKUPS=()\n"
        + "ROLLBACK_DIR_CURRENT_EXISTED=()\n"
        + "mac_launchd_fsync_directory() { :; }\n"
        + _extract_program_function(program, "restore_dir")
        + _extract_program_function(program, "restore_absent_dir")
        + _extract_program_function(program, "restore_dir_or_keep_prior")
        + f"restore_dir_or_keep_prior {shlex.quote(str(backup))} "
        + f"{shlex.quote(str(destination))} {shlex.quote(state)}\n"
        + 'printf "destination_exists=%s\\n" '
        + f'"$([ -e {shlex.quote(str(destination))} ] && echo 1 || echo 0)"\n',
        encoding="utf-8",
    )
    return subprocess.run(
        ["/bin/bash", str(harness)], check=False, capture_output=True, text=True
    )


def _extract_program_function(program: str, name: str) -> str:
    body = program.split(f"\n{name}() {{\n", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{\n{body}\n}}\n"


def test_prior_absent_directory_is_removed_rather_than_restored(
    tmp_path: Path,
) -> None:
    # The failed from-scratch install left a source tree behind and there is no
    # backup to put back; rolling back means the node ends up uninstalled.
    result = _restore(
        tmp_path, state="prior-absent", make_backup=False, make_destination=True
    )
    assert result.returncode == 0, result.stderr
    assert "destination_exists=0" in result.stdout


def test_prior_absent_directory_tolerates_a_destination_that_was_never_created(
    tmp_path: Path,
) -> None:
    result = _restore(
        tmp_path, state="prior-absent", make_backup=False, make_destination=False
    )
    assert result.returncode == 0, result.stderr
    assert "destination_exists=0" in result.stdout


def test_prior_absent_refuses_a_contradictory_prior_generation_backup(
    tmp_path: Path,
) -> None:
    # A backup at the armed path contradicts "there was no prior generation".
    # Deleting the destination on that evidence could discard a real generation.
    result = _restore(
        tmp_path, state="prior-absent", make_backup=True, make_destination=True
    )
    assert result.returncode != 0
    assert "prior-absent directory has an unexpected backup" in result.stderr


def test_backup_state_still_restores_the_prior_generation(tmp_path: Path) -> None:
    result = _restore(
        tmp_path, state="backup", make_backup=True, make_destination=True
    )
    assert result.returncode == 0, result.stderr
    assert "destination_exists=1" in result.stdout
    assert (
        tmp_path / "mac-home" / "src" / "mac" / "generation"
    ).read_text(encoding="utf-8") == "prior\n"


def test_unknown_restoration_state_is_refused(tmp_path: Path) -> None:
    result = _restore(
        tmp_path, state="whatever", make_backup=False, make_destination=True
    )
    assert result.returncode != 0
    assert "invalid directory restoration state" in result.stderr


# --------------------------------------------------------------------------
# The published rollback program carries and validates the from-scratch intent
# --------------------------------------------------------------------------


def test_generated_program_transports_and_validates_the_from_scratch_intent() -> None:
    program = _generated_rollback_template()
    assert "ROLLBACK_FROM_SCRATCH='$DEPLOY_FROM_SCRATCH'" in _function(
        "write_rollback_script", "verify_phase2_rollback_intent"
    )
    validation = program.split('case "$ROLLBACK_FROM_SCRATCH" in', 1)[1].split(
        "esac", 1
    )[0]
    assert "0|1" in validation
    assert "invalid from-scratch rollback intent" in validation
    # A from-scratch generation never took a directory backup, so demanding one
    # would arm a rollback that can never run.
    assert 'SRC_ROLLBACK_STATE=prior-absent' in program
    assert 'VENV_ROLLBACK_STATE=prior-absent' in program
    assert (
        '[ "$ROLLBACK_FROM_SCRATCH" = 1 ] || require_rollback_directory "$BIN_BACKUP"'
        in program
    )
    bin_restore = program.split('if [ "$ROLLBACK_FROM_SCRATCH" = 1 ]; then\n', 1)[-1]
    assert 'restore_absent_dir "$MAC_HOME/bin"' in bin_restore


def test_generated_program_accepts_a_from_scratch_node_with_no_generation_marker() -> None:
    program = _generated_rollback_template()
    state = program.split("rollback_generation_state=successor", 1)[1].split(
        "rollback failed: current node generation is outside this rollback contract",
        1,
    )[0]
    assert '[ "$ROLLBACK_FROM_SCRATCH" = 1 ]' in state
    assert '[ -z "$rollback_current_generation" ]' in state
    assert '[ -z "$rollback_current_revision" ]' in state


def test_auxiliary_capture_runs_from_scratch_so_new_artifacts_are_removable() -> None:
    capture = _function(
        "capture_auxiliary_rollback_artifacts", "write_rollback_script"
    )
    from_scratch, upgrade = capture.split('if [ "$DEPLOY_FROM_SCRATCH" = 1 ]; then', 1)[
        1
    ].split("\n  else\n", 1)
    # Nothing exists to snapshot from scratch, but BIN_BACKUP keeps its
    # canonical name so the sealed-intent readback still recognises it.
    assert "snapshot_bin_directory_for_rollback" not in from_scratch
    assert 'BIN_BACKUP="$MAC_HOME/backups/bin.${AGENT}.${DEPLOY_TS}"' in from_scratch
    assert "snapshot_bin_directory_for_rollback" in upgrade
    # Both paths must fall through to the same artifact tracking; tracking is
    # what makes a from-scratch rollback able to delete what the install wrote.
    assert 'track_auxiliary_rollback_artifact "$ENV_FILE" user' in capture
    assert '"$MAC_HOME/deployed-source-revision" user' in capture


def test_arming_order_is_unchanged_for_a_from_scratch_node() -> None:
    arm = _function("arm_phase2_rollback", "backup_existing_artifacts")
    mutable = arm.index("capture_mutable_runtime_state_for_rollback")
    auxiliary = arm.index("capture_auxiliary_rollback_artifacts")
    publish = arm.index("write_rollback_script")
    intent = arm.index("write_phase2_rollback_intent")
    armed = arm.rindex("DEPLOY_ROLLBACK_ARMED=1")
    assert mutable < auxiliary < publish < intent < armed
    # The exemption is decided before any backup path is computed, so a
    # from-scratch node never claims a backup it did not take.
    assert arm.index("DEPLOY_FROM_SCRATCH=1") < arm.index('SRC_BACKUP="$MAC_HOME')
