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
    "plugin_dir",
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


def plugin_dir() -> Path:
    """Canonical Agent Plugins package the installer owns: ``$MAC_HOME/plugin``.

    One copy, then client-specific pointers. Copying the plugin into four
    harness directories is how they go stale independently.
    """
    return mac_home() / "plugin"


def gateway_home() -> Path:
    """The Hermes gateway / agent-personal home.

    Hermes is MAC's only human interface. ``HERMES_HOME`` is authoritative.
    Without it the home is ``~/.hermes``, upstream Hermes' own default and the
    one ``deploy/hermes/install-hermes-gateway.sh`` uses -- unless ``MAC_HOME``
    relocates the MAC tree, in which case it moves with it to
    ``$MAC_HOME/hermes`` so a relocated MAC never reads or writes the user's
    real Hermes profile.
    """
    explicit = _env_path("HERMES_HOME")
    if explicit is not None:
        return explicit
    if _env_path("MAC_HOME") is not None:
        return mac_home() / "hermes"
    return Path.home() / ".hermes"


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


# --- Gateway files (under gateway_home) ------------------------------------


def gateway_env_file() -> Path:
    """Gateway secrets file the gateway process sources: ``$HERMES_HOME/.env``."""
    return gateway_home() / ".env"
