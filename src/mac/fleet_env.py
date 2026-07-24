"""Fleet-scoped environment variable resolution.

mac-g55y: ~/.mac/.env historically stored credentials under unscoped
names like MAC_API_TOKEN, MAC_DEPLOY_HUB_TOKEN, MAC_WORKER_TOKEN. When
a workstation participates in more than one fleet, the second fleet's
setup overwrites the first fleet's value because they share the name.

This module gives every fleet-bound credential a scoped form:

    MAC_API_TOKEN__<FLEET>

where ``<FLEET>`` is the fleet name uppercased with non-alphanumeric
chars replaced by ``_`` (e.g. ``MAC_API_TOKEN__JORDANH_HUB``).

Readers should call :func:`resolve` with ``fleet`` set to the active
fleet (CLI ``--fleet``, ``MAC_FLEET`` env, or hub-derived). When the
scoped form is missing the resolver falls back to the legacy flat form
and emits a one-time deprecation warning per (var, fleet).
"""
from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

_LOG = logging.getLogger("mac.fleet_env")
_DEPRECATION_SEEN: set = set()

# Credential-bearing env vars whose flat form collides across fleets.
# Keep this list small and explicit so we don't accidentally scope
# things like MAC_DB or MAC_SECRET_KEY that should stay shared.
FLEET_SCOPED_VARS = frozenset(
    {
        "MAC_TOKEN",
        "MAC_API_TOKEN",
        "MAC_API_TOKENS",
        "MAC_DEPLOY_HUB_TOKEN",
        "MAC_WORKER_TOKEN",
        "MAC_DEPLOY_TOKENHUB_API_KEY",
        "MAC_DEPLOY_TAILSCALE_AUTH_KEY",
        "MAC_DEPLOY_GITHUB_REVIEW_KEY_B64",
    }
)


def _normalize_fleet(fleet: str) -> str:
    """Map a fleet name to its env-var suffix form.

    >>> _normalize_fleet("jordanh-hub")
    'JORDANH_HUB'
    >>> _normalize_fleet("rocky")
    'ROCKY'
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", fleet.strip()).strip("_").upper()
    if not cleaned:
        raise ValueError("fleet name produces an empty env suffix: %r" % fleet)
    return cleaned


def scoped_var(base_name: str, fleet: str) -> str:
    """Return the fleet-scoped variant of ``base_name``."""
    return "%s__%s" % (base_name, _normalize_fleet(fleet))


def resolve(
    base_name: str,
    fleet: Optional[str] = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve a fleet-scoped env var with legacy fallback.

    Lookup order:
      1. ``BASE_NAME__<fleet>`` if ``fleet`` is set (explicit arg, then
         ``MAC_FLEET`` env var).
      2. ``BASE_NAME`` (legacy flat form). Emits a one-time deprecation
         warning when ``base_name`` is a fleet-scoped credential.
    """
    src = os.environ if env is None else env
    active_fleet = fleet or src.get("MAC_FLEET")
    scoped_value = _resolve_scoped(base_name, active_fleet, env=src)
    if scoped_value is not None:
        return scoped_value
    return _resolve_legacy(base_name, active_fleet, env=src)


def _resolve_scoped(
    base_name: str,
    active_fleet: Optional[str],
    *,
    env: Dict[str, str],
) -> Optional[str]:
    """Return the fleet-scoped value for ``base_name`` or ``None``.

    Never falls back to the legacy flat form and never emits a warning, so
    callers can prefer scoped values across an entire credential chain before
    considering any legacy value (see :func:`resolve_first`).
    """
    if not active_fleet:
        return None
    return env.get(scoped_var(base_name, active_fleet))


def _resolve_legacy(
    base_name: str,
    active_fleet: Optional[str],
    *,
    env: Dict[str, str],
) -> Optional[str]:
    """Return the legacy flat value for ``base_name`` or ``None``.

    Emits the one-time mac-g55y deprecation warning when the flat form of a
    fleet-scoped credential is used.
    """
    legacy = env.get(base_name)
    if legacy is not None and base_name in FLEET_SCOPED_VARS:
        key = (base_name, active_fleet or "")
        if key not in _DEPRECATION_SEEN:
            _DEPRECATION_SEEN.add(key)
            _LOG.warning(
                "using legacy flat env var %s; switch to %s to avoid cross-fleet collisions "
                "(see mac-g55y; run `mac config migrate-env-namespace` to migrate)",
                base_name,
                scoped_var(base_name, active_fleet) if active_fleet else "<base>__<FLEET>",
            )
    return legacy


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse a shell-style ``KEY=VALUE`` env file.

    Handles ``export``-prefixed lines and double-quoted values. Does NOT
    expand variables or run subshells; the file is treated as static
    config. Comments (``# ...``) and blank lines are ignored.
    """
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith('"') and raw_value.endswith('"') and len(raw_value) >= 2:
            raw_value = raw_value[1:-1]
        elif raw_value.startswith("'") and raw_value.endswith("'") and len(raw_value) >= 2:
            raw_value = raw_value[1:-1]
        values[key] = raw_value
    return values


def _render_assignment(key: str, value: str, *, export: bool = False) -> str:
    """Render a ``KEY=value`` line, double-quoting values that need it."""
    prefix = "export " if export else ""
    if value == "" or re.search(r"[\s\"'$`\\#]", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return '%s%s="%s"' % (prefix, key, escaped)
    return "%s%s=%s" % (prefix, key, value)


def set_env_key(path: Path, key: str, value: str, *, backup: bool = True) -> bool:
    """Idempotently set ``key=value`` in a shell-style env file.

    Replaces the first existing assignment for ``key`` in place (keeping an
    ``export`` prefix if present) or appends it when absent; the rest of the
    file is preserved byte-for-byte. Values containing whitespace or shell
    metacharacters are double-quoted. Creates the file (mode 0600) if missing.
    When ``backup`` is set and the file already exists, a timestamped copy is
    written next to it before the change. Returns True iff the file changed.
    """
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    out: list = []
    found = False
    for raw_line in (existing.splitlines() if existing is not None else []):
        stripped = raw_line.strip()
        is_export = stripped.startswith("export ")
        candidate = stripped[len("export "):] if is_export else stripped
        cur_key = candidate.split("=", 1)[0].strip() if "=" in candidate else None
        if cur_key == key and not found:
            out.append(_render_assignment(key, value, export=is_export))
            found = True
        else:
            out.append(raw_line)
    if not found:
        out.append(_render_assignment(key, value))
    new_text = "\n".join(out) + "\n"
    if existing == new_text:
        return False
    if backup and existing is not None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_path = path.parent / ("%s.bak-setkey-%s" % (path.name, ts))
        backup_path.write_text(existing, encoding="utf-8")
        try:
            backup_path.chmod(0o600)
        except OSError:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True


def migrate_env_file(
    path: Path,
    fleet: str,
    *,
    keep_legacy: bool = True,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Rewrite ``path`` to add fleet-scoped variants of flat credentials.

    For every key in :data:`FLEET_SCOPED_VARS` that exists in the file
    in flat form, append the scoped form ``KEY__<FLEET>`` with the same
    value. When ``keep_legacy`` is False, the flat key is removed; the
    default keeps both so other consumers (older mac releases on the
    same machine, sourcing the file directly) still work for one
    deprecation cycle.

    Returns ``(added, kept_legacy)`` mapping for caller-reporting.
    """
    values = parse_env_file(path)
    added: Dict[str, str] = {}
    kept: Dict[str, str] = {}
    for key, value in list(values.items()):
        if key not in FLEET_SCOPED_VARS:
            continue
        scoped = scoped_var(key, fleet)
        if scoped in values:
            continue  # already migrated for this fleet
        added[scoped] = value
        if keep_legacy:
            kept[key] = value
        else:
            values.pop(key, None)
    if not added:
        return added, kept
    values.update(added)
    # Rewrite preserving key order: keep existing lines, append new ones.
    lines: list = []
    existing_keys: set = set()
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(raw_line)
                continue
            check = stripped[len("export "):] if stripped.startswith("export ") else stripped
            key_part, sep, _ = check.partition("=")
            key_part = key_part.strip()
            if sep and not keep_legacy and key_part in FLEET_SCOPED_VARS:
                # Skip the legacy flat line when caller asked us to drop it.
                continue
            existing_keys.add(key_part)
            lines.append(raw_line)
    lines.append("")
    lines.append("# Added by `mac config migrate-env-namespace --fleet %s` (mac-g55y)" % fleet)
    for new_key, new_value in added.items():
        # Quote values containing spaces or shell meta chars.
        if re.search(r"[\s\"'$`\\]", new_value):
            escaped = new_value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append('%s="%s"' % (new_key, escaped))
        else:
            lines.append("%s=%s" % (new_key, new_value))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return added, kept


def resolve_first(
    base_names: Iterable[str],
    fleet: Optional[str] = None,
    *,
    env: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Resolve the first env var in ``base_names`` that has a value.

    Resolution runs in two passes so a *fleet-scoped* value always outranks a
    *legacy flat* value, regardless of where each sits in the chain:

    1. Walk ``base_names`` in argument order and return the first name whose
       fleet-scoped form (``NAME__<FLEET>``) is set.
    2. Only if no scoped value exists anywhere in the chain, walk the names
       again and return the first legacy flat value (emitting the mac-g55y
       deprecation warning for it).

    This prevents a stale legacy ``MAC_WORKER_TOKEN`` from shadowing the
    correct scoped ``MAC_TOKEN__<FLEET>`` later in the worker's chain
    ``MAC_WORKER_TOKEN > MAC_TOKEN > MAC_API_TOKEN`` (which manifested as a
    startup-heartbeat 403). Within a single pass, precedence still follows the
    *argument order*. Every name in a credential chain should also be listed in
    :data:`FLEET_SCOPED_VARS` so its flat form is migrated and warned about
    consistently.
    """
    src = os.environ if env is None else env
    active_fleet = fleet or src.get("MAC_FLEET")
    names = list(base_names)
    for name in names:
        value = _resolve_scoped(name, active_fleet, env=src)
        if value:
            return value
    for name in names:
        value = _resolve_legacy(name, active_fleet, env=src)
        if value:
            return value
    return None


def list_scoped_vars(env: Optional[Dict[str, str]] = None) -> Iterable[Tuple[str, str, str]]:
    """Yield ``(base_name, fleet_suffix, value)`` for every scoped var
    currently present in the environment.
    """
    src = os.environ if env is None else env
    for key, value in src.items():
        if "__" not in key:
            continue
        base, _, suffix = key.rpartition("__")
        if base in FLEET_SCOPED_VARS and suffix:
            yield base, suffix, value
