"""Single sanctioned resolver for every MAC on-disk home path.

This module is the ONE place allowed to name the `.mac` / `.hermes` home
directories. Every other first-party module must resolve paths through the
functions here instead of hard-coding ``Path.home() / ".mac"`` or
``Path.home() / ".hermes"``. A test guard
(``tests/test_mac_paths_no_hardcode.py``) fails the build if a new literal
appears outside this module.

Design contract — behavior-preserving *and* relocatable:
  * With the environment unset (the production default today), every resolver
    returns exactly the path the old hard-coded literals returned — so routing
    existing call sites through this module changes nothing observable.
  * Setting ``MAC_HOME`` / ``HERMES_HOME`` relocates ALL derived paths together
    (the whole point: today ``MAC_HOME`` is a leaky knob that dozens of modules
    ignore). Per-file overrides (``MAC_DB``, ``MAC_JOURNAL_DIR``,
    ``MAC_FLEETS_CONFIG``, ``MAC_DEPLOY_ENV_FILE``) continue to win when set.

See docs/home-consolidation.md for the full consolidation plan this unblocks.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "mac_home",
    "gateway_home",
    "mac_env_file",
    "deploy_env_file",
    "gateway_env_file",
    "fleets_config",
    "ledger_db",
    "journal_dir",
    "backups_dir",
    "archive_dir",
    "dream_logs_dir",
    "openclaw_home",
    "script_jobs_dir",
    "script_jobs_scripts_dir",
    "script_jobs_output_dir",
    "legacy_gateway_scripts_dir",
]


def _env_path(name: str) -> Path | None:
    """Return an expanded Path for env var ``name`` if set and non-empty."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    return Path(value).expanduser()


# --- Roots -----------------------------------------------------------------

def mac_home() -> Path:
    """The control-plane / hub home. ``MAC_HOME`` overrides; default ``~/.mac``.

    Mirrors ``client_principals.mac_home()`` and becomes the single reliable
    relocation knob once callers route through it.
    """
    return _env_path("MAC_HOME") or (Path.home() / ".mac")


def gateway_home() -> Path:
    """The gateway / agent-personal home.

    ``HERMES_HOME`` still overrides, for a host that has not been migrated yet.
    The DEFAULT is now ``$MAC_HOME/openclaw`` -- this is the Phase 2 repoint the
    previous docstring promised, done without changing a single caller, which is
    the whole reason this module is the one place allowed to name a home.

    ``~/.hermes`` is not a fallback. The fleet migrated hard to OpenClaw and the
    directory was evicted from every host on 2026-08-21 (4.9GB across three),
    so defaulting there would resolve to something that no longer exists.
    """
    return _env_path("HERMES_HOME") or (mac_home() / "openclaw")


# --- Control-plane files (under mac_home) ----------------------------------

def mac_env_file() -> Path:
    """Hub/service secrets file: ``$MAC_HOME/mac.env``."""
    return mac_home() / "mac.env"


def deploy_env_file() -> Path:
    """Client deploy env (scoped fleet tokens). ``MAC_DEPLOY_ENV_FILE``
    overrides; default ``$MAC_HOME/.env``."""
    return _env_path("MAC_DEPLOY_ENV_FILE") or (mac_home() / ".env")


def fleets_config() -> Path:
    """Fleet registry. ``MAC_FLEETS_CONFIG`` overrides; default
    ``$MAC_HOME/fleets.yaml``."""
    return _env_path("MAC_FLEETS_CONFIG") or (mac_home() / "fleets.yaml")


def ledger_db() -> Path:
    """Hub SQLite ledger. ``MAC_DB`` overrides; default ``$MAC_HOME/mac.db``."""
    return _env_path("MAC_DB") or (mac_home() / "mac.db")


def journal_dir() -> Path:
    """Daily soul/memory snapshot dir. ``MAC_JOURNAL_DIR`` overrides; default
    ``$MAC_HOME/journal``."""
    return _env_path("MAC_JOURNAL_DIR") or (mac_home() / "journal")


def backups_dir() -> Path:
    """Ledger backups + deploy rollback artifacts: ``$MAC_HOME/backups``."""
    return mac_home() / "backups"


def archive_dir() -> Path:
    """Ledger archive: ``$MAC_HOME/archive``."""
    return mac_home() / "archive"


def openclaw_home() -> Path:
    """OpenClaw's runtime home. ``MAC_OPENCLAW_HOST_DIR`` overrides; default
    ``$MAC_HOME/openclaw`` (already nested under the root today)."""
    return _env_path("MAC_OPENCLAW_HOST_DIR") or (mac_home() / "openclaw")


# --- Host script jobs (one home: input, definitions and output together) ----
#
# A host two-stage cron job ("script job") has four artefacts. Before the
# 2026-08-21 untangle they were split across two homes: the runner READ its
# pre-run scripts from ``~/.hermes/scripts`` while WRITING its output to
# ``~/.mac/openclaw/script-jobs/output``, so Hermes-named reports accumulated in
# the OpenClaw tree and no single directory answered "where does this job live?".
# The decision recorded in docs/home-consolidation.md §5c is that every artefact
# a MAC-owned runner touches lives under ``script_jobs_dir()``:
#
#   scripts      -> script_jobs_scripts_dir()   ($OPENCLAW_HOST_DIR/script-jobs/scripts)
#   definitions  -> openclaw_home()/host-script-jobs.json  (already OpenClaw)
#   output       -> script_jobs_output_dir()    ($OPENCLAW_HOST_DIR/script-jobs/output)
#   schedule     -> launchd/systemd units       (host supervisor, no home)
#
# Session DB and gateway credentials stay in ``gateway_home()``: they are the
# gateway's own state, not the runner's, and relocating them is Phase 2 of the
# consolidation plan. The runner never reads either.

def script_jobs_dir() -> Path:
    """Single home for host two-stage cron-job artefacts:
    ``$MAC_OPENCLAW_HOST_DIR/script-jobs``."""
    return openclaw_home() / "script-jobs"


def script_jobs_scripts_dir() -> Path:
    """Pre-run scripts the host cron runner executes.
    ``MAC_OPENCLAW_SCRIPT_JOB_SCRIPTS_DIR`` overrides; default
    ``$MAC_OPENCLAW_HOST_DIR/script-jobs/scripts``."""
    return _env_path("MAC_OPENCLAW_SCRIPT_JOB_SCRIPTS_DIR") or (
        script_jobs_dir() / "scripts"
    )


def script_jobs_output_dir() -> Path:
    """Where non-deliverable script-job replies are written.
    ``MAC_OPENCLAW_SCRIPT_JOB_OUTPUT_DIR`` overrides; default
    ``$MAC_OPENCLAW_HOST_DIR/script-jobs/output``."""
    return _env_path("MAC_OPENCLAW_SCRIPT_JOB_OUTPUT_DIR") or (
        script_jobs_dir() / "output"
    )


# --- Gateway files (under gateway_home) ------------------------------------

def legacy_gateway_scripts_dir() -> Path:
    """Pre-untangle location of the script-job scripts: ``$HERMES_HOME/scripts``.

    This is the ONLY sanctioned name for that path, and it exists solely so the
    host runner can fall back READ-ONLY on a host that has not run the relocator
    (``deploy/openclaw/relocate-script-job-home.py``) yet — enabling the new
    default must never silently stop an enabled job. Nothing writes here, and
    the fallback disappears with the legacy tree.

    Phase 2 moved ``gateway_home()`` under ``$MAC_HOME/openclaw``. Do not follow
    it: the evicted tree is still ``~/.hermes`` (or ``$HERMES_HOME`` when a host
    has not migrated). Routing this through ``gateway_home()`` would make the
    fallback look at the live OpenClaw tree and miss leftover Hermes scripts.
    """
    return (_env_path("HERMES_HOME") or (Path.home() / ".hermes")) / "scripts"


def gateway_env_file() -> Path:
    """Gateway secrets file the gateway process sources: ``$HERMES_HOME/.env``."""
    return gateway_home() / ".env"


def dream_logs_dir() -> Path:
    """Directory the gateway dream-cycle cron writes human-readable reports to:
    ``$HERMES_HOME/dream_logs``.

    NOTE: this is *not* MAC's durable learning store — those are
    ``memory_records`` (record_type ``dream:*``) in the ledger. This resolver
    exists so the dream-log importer (``mac.dream_log_import``) can merge the
    otherwise-orphaned reports into that durable store.
    """
    return gateway_home() / "dream_logs"
