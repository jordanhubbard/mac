"""Deploy contracts for backup-gated PostgreSQL schema migration ordering."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _installer() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def _migration_function(source: str) -> str:
    return source.split("migrate_control_plane_schema() {", 1)[1].split(
        "\n}\n\nretire_spoke_local_control_plane_database", 1
    )[0]


def test_hub_schema_migration_runs_quiesced_before_any_upgraded_hub_start() -> None:
    source = _installer()
    call = source.index(
        "\n# The upgraded package is installed and the typed hub is still quiesced here."
    )
    package = source.index('"$VENV/bin/python" -m pip install -e')
    legacy_quiescence = source.index("stop_existing_services_for_deploy")
    systemd_start = source.index('run_systemctl restart "$MAC_SERVICE_NAME"')
    launchd_start = source.index(
        "mac_launchd_bootstrap_job", source.index("install_darwin_service")
    )
    supervisord_start = source.index(
        'start_supervisord_program "$MAC_SUPERVISORD_PROG"',
        source.index("install_supervisord_service"),
    )

    assert legacy_quiescence < package < call
    assert call < systemd_start
    assert call < launchd_start
    assert call < supervisord_start
    assert "phase1-cohort-quiescence-${DEPLOY_GENERATION}.json" in _migration_function(source)


def test_schema_preflight_skips_backup_when_current_and_backup_gates_migration() -> None:
    body = _migration_function(_installer())
    preflight = body.index('"$VENV/bin/mac-schema-migrate" --status')
    current = body.index('if [ "$pending_count" -eq 0 ]')
    backup = body.index('"$VENV/bin/mac-pg-backup" --json')
    proof = body.index('payload.get("restore_verified") is not True')
    pending_receipt = body.index("backup_verified_migration_pending")
    migration = body.index(
        '"$VENV/bin/mac-schema-migrate" --database-url',
        backup,
    )

    assert preflight < current < backup < proof < pending_receipt < migration
    assert "skipped backup and migration" in body
    assert "schema migration backup was not restore-verified" in body


def test_existing_unversioned_baseline_requires_explicit_deploy_authority() -> None:
    installer = _installer()
    body = _migration_function(installer)
    deploy = DEPLOY.read_text(encoding="utf-8")

    assert 'if [ "$state" = existing-unversioned ]' in body
    assert 'truthy "$SCHEMA_BASELINE_AUTHORIZED"' in body
    assert "--authorize-existing-baseline" in body
    assert "MAC_DEPLOY_AUTHORIZE_EXISTING_SCHEMA_BASELINE=1" in body
    assert "add_remote_env MAC_DEPLOY_AUTHORIZE_EXISTING_SCHEMA_BASELINE" in deploy


def test_legacy_prune_has_separate_authority_after_verified_backup() -> None:
    installer = _installer()
    body = _migration_function(installer)
    deploy = DEPLOY.read_text(encoding="utf-8")

    prune_preflight = body.index('payload.get("requires_legacy_schema_prune_authority")')
    prune_authority = body.index('truthy "$SCHEMA_LEGACY_PRUNE_AUTHORIZED"')
    backup = body.index('"$VENV/bin/mac-pg-backup" --json')
    backup_proof = body.index('payload.get("restore_verified") is not True')
    migration = body.index('if ! "$VENV/bin/mac-schema-migrate"')

    assert prune_preflight < prune_authority < backup < backup_proof < migration
    assert "--authorize-legacy-schema-prune" in body
    assert "MAC_DEPLOY_AUTHORIZE_LEGACY_SCHEMA_PRUNE=1" in body
    assert "add_remote_env MAC_DEPLOY_AUTHORIZE_LEGACY_SCHEMA_PRUNE" in deploy
    # Both preflight and migration JSON are embedded unchanged in the durable
    # deploy receipt, preserving the before-drop row counts.
    assert '"preflight": load("MAC_SCHEMA_PREFLIGHT")' in installer
    assert '"migration": load("MAC_SCHEMA_RESULT")' in installer


def test_failed_migration_retains_backup_receipt_and_cannot_start_hub() -> None:
    source = _installer()
    body = _migration_function(source)
    migration = body.index('if ! "$VENV/bin/mac-schema-migrate"')
    failure_receipt = body.index("migration_failed_backup_retained", migration)
    failure = body.index('die "PostgreSQL schema migration failed transactionally', migration)
    success = body.index("write_schema_migration_receipt \\\n    migrated", migration)

    assert migration < failure_receipt < failure < success
    assert "SCHEMA_MIGRATION_RECEIPT" in body
    # `die` exits under the deployment rollback trap before execution reaches
    # any supervisor-specific service installer.
    assert source.index("migrate_control_plane_schema\n") < source.index(
        'case "$SUPERVISOR_KIND" in', source.index("migrate_control_plane_schema\n")
    )


def test_spokes_never_backup_or_migrate_postgresql() -> None:
    body = _migration_function(_installer())
    first_command = next(line.strip() for line in body.splitlines() if line.strip())

    assert first_command == "control_plane_enabled || return 0"
    assert '"$VENV/bin/mac-pg-backup"' in body
    assert '"$VENV/bin/mac-schema-migrate"' in body


def test_empty_backup_and_baseline_args_expand_under_nounset() -> None:
    """Versioned pending migrations leave both arrays empty; Darwin bash 3.2
    with `set -u` aborts `"${empty[@]}"` (rocky 2026-08-28 deploy of #671)."""
    body = _migration_function(_installer())

    assert "local -a baseline_args=() backup_args=()" in body
    assert '    ${backup_args[@]+"${backup_args[@]}"} > "$backup"' in body
    assert '      ${baseline_args[@]+"${baseline_args[@]}"} > "$migration"' in body
    unsafe = body.replace('${backup_args[@]+"${backup_args[@]}"}', "").replace(
        '${baseline_args[@]+"${baseline_args[@]}"}', ""
    )
    assert '"${backup_args[@]}"' not in unsafe
    assert '"${baseline_args[@]}"' not in unsafe
