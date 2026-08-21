"""Pin the premises a from-scratch node deploy depends on.

`deploy/fleet-node-install.sh` was written for the upgrade case: a node that
already carries a complete prior generation ($SRC_DIR + $VENV) which phase 2
backs up and can restore.  Several functions on the shared main()/legacy
one-shot path silently assume that premise, and each one that does is a
separate hard stop for a node that has never been deployed.

These tests do not assert whether `arm_phase2_rollback` admits a from-scratch
node -- that gate is deliberately left to whichever deploy mode declares a
from-scratch install.  They pin the surrounding invariants that such a mode
depends on, so the "assumes an upgrade" premise cannot quietly reappear in the
snapshot helpers, in the generated rollback program's preflight, or in the two
distinct meanings the name `rollback_capable` carries across phase 1 and
phase 2.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"
PHASE1_QUIESCE = ROOT / "deploy" / "fleet-node-phase1-quiesce.sh"


def _region(source: str, opening: str, closing: str) -> str:
    return source.split(opening, 1)[1].split(closing, 1)[0]


def test_rollback_snapshot_helpers_tolerate_a_node_with_no_prior_generation() -> None:
    """Both pre-mutation snapshot helpers must no-op, not die, from scratch.

    They run before anything has been installed, so on a node with no prior
    generation there is nothing to snapshot.  Each must return success on its
    first statement rather than reaching a snapshot call with absent inputs.
    """

    source = NODE_INSTALL.read_text(encoding="utf-8")

    mutable = _region(
        source,
        "capture_mutable_runtime_state_for_rollback() {",
        "\n}\n\ncapture_auxiliary_rollback_artifacts() {",
    )
    guard = mutable.index(
        '[ -d "$SRC_DIR" ] && [ ! -L "$SRC_DIR" ] \\\n'
        '    && [ -d "$VENV" ] && [ ! -L "$VENV" ] || return 0'
    )
    assert guard < mutable.index("snapshot_rollback_directory")
    assert "die" not in mutable[:guard]

    auxiliary = _region(
        source,
        "capture_auxiliary_rollback_artifacts() {",
        "\n}\n\nwrite_rollback_script() {",
    )
    guard = auxiliary.index('[ -d "$SRC_DIR" ] && [ -d "$VENV" ] || return 0')
    assert guard < auxiliary.index("snapshot_bin_directory_for_rollback")
    assert guard < auxiliary.index("track_auxiliary_rollback_artifact")
    assert guard < auxiliary.index("write_rollback_script")


def test_bin_backup_is_reachable_only_through_the_prior_generation_guard() -> None:
    """$BIN_BACKUP is a snapshot of an existing tree, so from scratch it is empty.

    `snapshot_bin_directory_for_rollback` is the only producer of a
    pre-mutation $BIN_BACKUP, and it sits behind the guard above.  A deploy
    mode that admits a from-scratch node therefore arms with `BIN_BACKUP=''`
    and cannot assume otherwise.
    """

    source = NODE_INSTALL.read_text(encoding="utf-8")

    assert source.count("snapshot_bin_directory_for_rollback") == 2
    definition = source.index("snapshot_bin_directory_for_rollback() {")
    call = source.index("\n  snapshot_bin_directory_for_rollback\n")
    auxiliary = source.index("capture_auxiliary_rollback_artifacts() {")
    auxiliary_end = source.index("\n}\n\nwrite_rollback_script() {", auxiliary)
    assert definition < auxiliary < call < auxiliary_end

    producer = _region(
        source,
        "snapshot_bin_directory_for_rollback() {",
        "\n}\n",
    )
    assert 'BIN_BACKUP="$MAC_HOME/backups/bin.${AGENT}.${DEPLOY_TS}"' in producer


def test_generated_rollback_program_fails_closed_without_a_prior_generation() -> None:
    """The armed rollback program restores; it never invents a prior generation.

    Its preflight requires a durable bin backup and, for source and virtualenv,
    either a durable backup or the untouched prior directory.  From scratch it
    has neither, and under `set -eEuo pipefail` the failing preflight aborts the
    rollback rather than deleting or guessing.  A from-scratch deploy mode that
    wants "delete what this invocation uploaded" needs its own program; it
    cannot reuse this one.
    """

    source = NODE_INSTALL.read_text(encoding="utf-8")
    generated = _region(
        source,
        "write_rollback_script() {",
        "\n}\n\nverify_phase2_rollback_intent() {",
    )

    assert "set -eEuo pipefail" in generated

    state = _region(
        generated,
        "rollback_directory_state() {",
        "\n}\n",
    )
    assert (
        "rollback failed: neither a durable backup nor the untouched prior "
        "directory is available" in state
    )
    assert "rm -rf" not in state

    preflight = generated.index('require_rollback_directory "\\$BIN_BACKUP"')
    quiesce = generated.index(
        'python "\\$ROLLBACK_SUPERVISOR_HELPER" '
        '"\\$ROLLBACK_SUPERVISOR_HELPER_SHA256" quiesce'
    )
    restore = generated.index('restore_dir "\\$BIN_BACKUP" "\\$MAC_HOME/bin" 1')
    assert preflight < quiesce < restore
    assert (
        generated.index('SRC_ROLLBACK_STATE="\\$(rollback_directory_state')
        < preflight
    )


def test_rollback_capable_is_derived_in_phase1_and_only_asserted_in_phase2() -> None:
    """One field name, two meanings; neither may drift into the other.

    Phase 1 derives `rollback_capable` from whether a restorable prior
    generation actually exists, and that is the gate a synchronized cutover
    enforces against a new node.  The phase-2 rollback intent publishes the
    same field as an unconditional literal meaning only "a rollback program was
    armed"; every reader of it checks identity against True and derives nothing
    further.  Reading the phase-2 field as evidence of a restorable generation
    would be wrong.
    """

    phase1 = PHASE1_QUIESCE.read_text(encoding="utf-8")
    assert (
        '"rollback_capable": regular_directory(source) and regular_directory(venv),'
        in phase1
    )
    assert (
        "rollback_capable = all(\n"
        "    path.is_dir() and not path.is_symlink() for path in (source, venv)\n"
        ")" in phase1
    )

    source = NODE_INSTALL.read_text(encoding="utf-8")
    intent = _region(
        source,
        "write_phase2_rollback_intent() {",
        "\n}\n\nverify_existing_phase2_sealed_state() {",
    )
    assert '"rollback_capable": True,' in intent

    for line in source.splitlines():
        if "rollback_capable" not in line:
            continue
        stripped = line.strip()
        if stripped == '"rollback_capable": True,':
            continue
        assert stripped.endswith(
            ('intent.get("rollback_capable") is not True',
             'contract.get("rollback_capable") is not True',
             'intent.get("rollback_capable") is True),',
             'value.get("rollback_capable") is not True')
        ), stripped
