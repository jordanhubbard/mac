"""One-time conversion of host credential aliases to canonical names."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Mapping

from mac.fleet_env import FLEET_SCOPED_VARS, _render_assignment, parse_env_file
from mac.mac_paths import mac_home

# These are host credentials. Fleet-scoped API tokens in the operator's
# ~/.mac/.env remain a separate multi-fleet CLI feature.
CANONICAL_CREDENTIALS = {
    "MAC_API_TOKEN": "hub API administrator token",
    "MAC_API_TOKENS": "hub API principal map",
    "MAC_WORKER_TOKEN": "worker bearer token",
    "MAC_DEPLOY_HUB_TOKEN": "deployment hub token",
    "MAC_DEPLOY_TOKENHUB_API_KEY": "legacy TokenHub deployment key",
    "MAC_DEPLOY_TAILSCALE_AUTH_KEY": "Tailscale enrollment key",
    "MAC_DEPLOY_GITHUB_REVIEW_KEY_B64": "GitHub review key",
}


class CredentialAliasError(ValueError):
    pass


def _suffix(fleet: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", fleet.strip()).strip("_").upper()


def aliases_for(canonical: str, fleet: str | None) -> tuple[str, ...]:
    aliases: list[str] = []
    if fleet and canonical in FLEET_SCOPED_VARS:
        aliases.append("%s__%s" % (canonical, _suffix(fleet)))
    if canonical == "MAC_WORKER_TOKEN":
        aliases.append("MAC_TOKEN")
        if fleet:
            aliases.append("MAC_TOKEN__%s" % _suffix(fleet))
    return tuple(aliases)


def canonical_value(
    canonical: str,
    *,
    environ: Mapping[str, str] | None = None,
    fleet: str | None = None,
) -> str | None:
    """Read only ``canonical`` and fail clearly when an alias is present."""
    env = os.environ if environ is None else environ
    value = env.get(canonical)
    if value is not None:
        return value
    active_fleet = fleet or env.get("MAC_FLEET") or env.get("MAC_FLEET_NAME")
    present = [name for name in aliases_for(canonical, active_fleet) if name in env]
    if present:
        raise CredentialAliasError(
            "%s is no longer accepted; run `python -m mac.credential_aliases --convert` "
            "to set canonical variable %s" % (present[0], canonical)
        )
    return None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def convert(path: Path, *, fleet: str | None = None) -> dict[str, object]:
    """Convert aliases in ``path`` atomically, backing up only when changed."""
    if not path.is_file():
        return {"changed": False, "path": str(path), "converted": []}
    original = path.read_text(encoding="utf-8")
    values = parse_env_file(path)
    active_fleet = fleet or values.get("MAC_FLEET") or values.get("MAC_FLEET_NAME")
    replacements: dict[str, str] = {}
    removed: set[str] = set()
    converted: list[dict[str, str]] = []
    for canonical in CANONICAL_CREDENTIALS:
        candidates = [(canonical, values[canonical])] if canonical in values else []
        candidates += [
            (name, values[name]) for name in aliases_for(canonical, active_fleet) if name in values
        ]
        if not candidates:
            continue
        distinct = {value for _, value in candidates}
        if len(distinct) != 1:
            names = ", ".join(name for name, _ in candidates)
            raise CredentialAliasError(
                "refusing to convert conflicting credentials %s; canonical variable is %s"
                % (names, canonical)
            )
        value = candidates[0][1]
        aliases = [name for name, _ in candidates if name != canonical]
        if not aliases:
            continue
        replacements[canonical] = value
        removed.update(aliases)
        converted.append({"canonical": canonical, "fingerprint": _fingerprint(value)})
    if not converted:
        return {"changed": False, "path": str(path), "converted": []}

    lines: list[str] = []
    written: set[str] = set()
    for raw in original.splitlines():
        stripped = raw.strip()
        candidate = stripped[7:] if stripped.startswith("export ") else stripped
        key = candidate.partition("=")[0].strip() if "=" in candidate else ""
        if key in removed:
            continue
        if key in replacements:
            if key not in written:
                lines.append(
                    _render_assignment(
                        key, replacements[key], export=stripped.startswith("export ")
                    )
                )
                written.add(key)
            continue
        lines.append(raw)
    for canonical, value in replacements.items():
        if canonical not in written:
            lines.append(_render_assignment(canonical, value))
    output = "\n".join(lines) + "\n"
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = path.with_name("%s.bak-credential-aliases-%s" % (path.name, timestamp))
    shutil.copy2(path, backup)
    backup.chmod(0o600)
    from mac.fleet_env import _atomic_write_private

    _atomic_write_private(path, output)
    return {
        "changed": True,
        "path": str(path),
        "backup": str(backup),
        "converted": converted,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--convert", action="store_true", required=True)
    parser.add_argument("--env-file", default=str(mac_home() / "mac.env"))
    parser.add_argument("--fleet")
    args = parser.parse_args(argv)
    try:
        result = convert(Path(args.env_file).expanduser(), fleet=args.fleet)
    except CredentialAliasError as exc:
        print("credential alias conversion: %s" % exc, file=sys.stderr)
        return 2
    if result["changed"]:
        details = ", ".join(
            "%s sha256:%s" % (item["canonical"], item["fingerprint"])
            for item in result["converted"]  # type: ignore[union-attr]
        )
        print("converted %s; backup: %s" % (details, result["backup"]))
    else:
        print("credential aliases already converted; no changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
