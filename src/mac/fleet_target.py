"""Authoritative fleet version-pin target of record.

This module is the single source of truth for *which version each fleet role
should run*, per artifact track. It replaces the stale hardcoded pin that used
to live only as prose in ``deploy/openclaw/RUNBOOK.md`` (``…-mac.10``).

Home decision
-------------
``mac rollout`` and ``mac env`` are control-plane (SQLite/hub) surfaces keyed by
tenant/channel and artifact rollouts; they model *in-flight* rollouts, not a
durable, reviewable, per-role fleet target that must be diff-able and travel
with the source tree. The target of record needs to be:

* checked in and code-reviewed alongside the deploy scripts it pins;
* readable without a populated hub database (deploy hosts, CI, canaries);
* machine-readable with a documented, versioned schema.

Those requirements point to a checked-in manifest, so the target lives in
``deploy/openclaw/fleet-target.json`` (schema ``mac.fleet_target.v1``). This
module owns its schema, parsing, validation, round-trip, and read/write
accessors; the ``mac fleet target`` CLI is a thin wrapper over it.

Tracks
------
Each role pins up to two artifact tracks:

* ``source`` — the MAC source revision (git commit) the role should run.
* ``openclaw`` — the stock OpenClaw gateway ``version`` and image ``revision``.
  Only roles that run the chat gateway carry this track; a worker-only role may
  omit it.

Note that *live hosts* carry commit-hash image revisions; the manifest records
the numeric build ``revision`` that produced the pinned image, and the
``source`` commit is the authoritative MAC revision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from mac.models import MACError

SCHEMA = "mac.fleet_target.v1"

# The checked-in manifest path, relative to the repository root.
DEFAULT_MANIFEST_RELPATH = Path("deploy/openclaw/fleet-target.json")

# A git commit revision: a 7-40 char hex SHA (short or full). ``HEAD`` and other
# symbolic refs are intentionally rejected — the target of record must pin an
# immutable revision.
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")


class FleetTargetError(MACError):
    """Raised for malformed or invalid fleet-target manifests/inputs."""


@dataclass(frozen=True)
class OpenClawTrack:
    """The OpenClaw gateway artifact track for a role."""

    version: str
    revision: str

    def to_dict(self) -> Dict[str, str]:
        return {"version": self.version, "revision": self.revision}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OpenClawTrack":
        if not isinstance(data, Mapping):
            raise FleetTargetError("openclaw track must be an object")
        try:
            version = data["version"]
            revision = data["revision"]
        except KeyError as exc:
            raise FleetTargetError(
                "openclaw track requires 'version' and 'revision'"
            ) from exc
        version = _require_nonempty_str("openclaw.version", version)
        # A revision may be a numeric build id ("19") or a commit-hash carried by
        # a live host; both serialize as a string so no information is lost.
        revision = _require_nonempty_str("openclaw.revision", str(revision))
        return cls(version=version, revision=revision)


@dataclass(frozen=True)
class RoleTarget:
    """The pinned target for a single fleet role across both tracks."""

    source: str
    openclaw: Optional[OpenClawTrack] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"source": self.source}
        if self.openclaw is not None:
            out["openclaw"] = self.openclaw.to_dict()
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RoleTarget":
        if not isinstance(data, Mapping):
            raise FleetTargetError("role target must be an object")
        try:
            source = data["source"]
        except KeyError as exc:
            raise FleetTargetError("role target requires a 'source' commit") from exc
        source = _require_commit("source", source)
        openclaw = None
        if data.get("openclaw") is not None:
            openclaw = OpenClawTrack.from_dict(data["openclaw"])
        return cls(source=source, openclaw=openclaw)


@dataclass
class FleetTargetManifest:
    """The full fleet target-of-record manifest."""

    roles: Dict[str, RoleTarget] = field(default_factory=dict)
    schema: str = SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "roles": {name: role.to_dict() for name, role in sorted(self.roles.items())},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FleetTargetManifest":
        if not isinstance(data, Mapping):
            raise FleetTargetError("manifest must be a JSON object")
        schema = data.get("schema")
        if schema != SCHEMA:
            raise FleetTargetError(
                "unsupported fleet-target schema %r (expected %r)" % (schema, SCHEMA)
            )
        raw_roles = data.get("roles", {})
        if not isinstance(raw_roles, Mapping):
            raise FleetTargetError("manifest 'roles' must be an object")
        roles: Dict[str, RoleTarget] = {}
        for name, role in raw_roles.items():
            _require_nonempty_str("role name", name)
            roles[name] = RoleTarget.from_dict(role)
        return cls(roles=roles, schema=SCHEMA)

    @classmethod
    def from_json(cls, text: str) -> "FleetTargetManifest":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FleetTargetError("invalid fleet-target JSON: %s" % exc) from exc
        return cls.from_dict(data)

    def get_role(self, role: str) -> RoleTarget:
        try:
            return self.roles[role]
        except KeyError as exc:
            raise FleetTargetError("no fleet target pinned for role %r" % role) from exc

    def set_role(self, role: str, target: RoleTarget) -> None:
        _require_nonempty_str("role name", role)
        self.roles[role] = target


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _require_nonempty_str(label: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FleetTargetError("%s must be a non-empty string" % label)
    return value.strip()


def _require_commit(label: str, value: Any) -> str:
    text = _require_nonempty_str(label, value).lower()
    if not _COMMIT_RE.match(text):
        raise FleetTargetError(
            "%s must be a 7-40 char hex git commit, got %r" % (label, value)
        )
    return text


def normalize_commit(value: Any, label: str = "source") -> str:
    """Public validator: return a normalized commit SHA or raise FleetTargetError."""
    return _require_commit(label, value)


# ---------------------------------------------------------------------------
# Storage accessors
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    # src/mac/fleet_target.py -> src/mac -> src -> repo root
    return Path(__file__).resolve().parent.parent.parent


def default_manifest_path() -> Path:
    """Return the default fleet-target manifest path."""
    return _repo_root() / DEFAULT_MANIFEST_RELPATH


def load_manifest(path: Optional[Path] = None) -> FleetTargetManifest:
    """Load and validate the manifest from *path* (defaults to the checked-in file)."""
    manifest_path = Path(path) if path is not None else default_manifest_path()
    if not manifest_path.exists():
        raise FleetTargetError("fleet-target manifest not found: %s" % manifest_path)
    return FleetTargetManifest.from_json(manifest_path.read_text(encoding="utf-8"))


def save_manifest(manifest: FleetTargetManifest, path: Optional[Path] = None) -> Path:
    """Serialize *manifest* to *path* (defaults to the checked-in file)."""
    manifest_path = Path(path) if path is not None else default_manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    return manifest_path
