"""Reported-version adapter: agent self-report → fleet-target pin fields.

This module is the *integration glue* between two independently-owned pieces:

* the authoritative fleet target-of-record pin (``mac.fleet_target.v1``,
  :mod:`mac.fleet_target`), which says *which version each role should run*; and
* the version each live agent *reports* about itself (its running MAC source
  revision and, for chat-gateway hosts, the stock OpenClaw gateway
  ``version``/``revision`` of the image it launched).

The self-report *investigation* — why 7 of 10 live agents report no gateway
image, and how the missing report should be produced — is tracked separately
(``investigate-deploy-version-skew`` / the version self-report task) and is a
prerequisite of this one. This module does **not** collect or fix the reports.
It only guarantees *field-level compatibility*: whatever a self-report emits is
normalized here into the *exact* fields the pin uses, so ``verify`` can compare
reported-vs-target without translation.

Contract
--------
A normalized report carries the same two tracks the pin does:

* ``source`` — the MAC source revision (git commit) the agent is running.
* ``openclaw`` — the OpenClaw gateway ``version`` and image ``revision`` the
  agent launched, or the explicit sentinel :data:`UNKNOWN` when the agent
  reported no gateway image. A worker-only role legitimately has no gateway, so
  the pin may omit the track; a *node that should* run a gateway but reported
  none is representable as ``unknown`` rather than absent — the two cases are
  distinct and both comparable.

Normalization keeps the pin and the report on the same footing:

* commit hashes are lower-cased and length-validated the same way the pin
  validates ``source`` (7-40 hex chars), so a full-SHA report matches a
  short-SHA pin (or vice versa) by common-prefix;
* the gateway ``revision`` is compared as a string so a numeric build id ("19")
  and a commit-hash image revision both round-trip without information loss,
  matching how :class:`mac.fleet_target.OpenClawTrack` stores it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Union

from mac.fleet_target import (
    FleetTargetError,
    OpenClawTrack,
    RoleTarget,
    normalize_commit,
)
from mac.models import MACError


class ReportedVersionError(MACError):
    """Raised for a malformed reported-version document."""


class _Unknown(Enum):
    """Singleton sentinel for a track an agent did not report."""

    token = 0

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNKNOWN"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "unknown"


#: Explicit "the agent reported no value for this track" sentinel.
UNKNOWN = _Unknown.token

# Accepted aliases for each pin field, so a self-report can use its own
# vocabulary and still normalize into the pin's field names. All values are
# compared case-insensitively against the report's keys.
_SOURCE_KEYS = ("source", "source_commit", "source_revision", "revision", "commit")
_OPENCLAW_KEYS = ("openclaw", "gateway", "gateway_image", "image")
_VERSION_KEYS = ("version", "gateway_version", "openclaw_version")
_REVISION_KEYS = ("revision", "gateway_revision", "image_revision", "build")

# Values a self-report may use to say "no gateway image reported".
_UNKNOWN_TOKENS = frozenset({"", "unknown", "unreported", "none", "null", "n/a"})


def _lookup(doc: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(k).lower(): v for k, v in doc.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def _is_unknown_token(value: Any) -> bool:
    if value is None:
        return True
    if value is UNKNOWN:
        return True
    return isinstance(value, str) and value.strip().lower() in _UNKNOWN_TOKENS


@dataclass(frozen=True)
class ReportedOpenClaw:
    """A normalized OpenClaw gateway report, in the pin's field names."""

    version: str
    revision: str

    def to_dict(self) -> dict[str, str]:
        return {"version": self.version, "revision": self.revision}


@dataclass(frozen=True)
class ReportedVersion:
    """A single agent's reported version, normalized to the pin's fields.

    ``openclaw`` is either a :class:`ReportedOpenClaw` or :data:`UNKNOWN` when
    the agent reported no gateway image (never silently absent — the "no report"
    case is explicit and comparable).
    """

    source: str
    openclaw: Union[ReportedOpenClaw, _Unknown]

    @property
    def gateway_unknown(self) -> bool:
        return self.openclaw is UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source}
        if self.openclaw is UNKNOWN:
            out["openclaw"] = "unknown"
        else:
            out["openclaw"] = self.openclaw.to_dict()
        return out

    @classmethod
    def from_report(cls, doc: Mapping[str, Any]) -> "ReportedVersion":
        """Normalize a raw self-report document into pin-aligned fields.

        The document may use any of the accepted aliases for each field and may
        express a missing gateway image either by omitting the openclaw track,
        by a null/empty value, or by an explicit ``"unknown"`` token — all three
        normalize to :data:`UNKNOWN`.
        """
        if not isinstance(doc, Mapping):
            raise ReportedVersionError("reported version must be an object")

        raw_source = _lookup(doc, _SOURCE_KEYS)
        if _is_unknown_token(raw_source):
            raise ReportedVersionError(
                "reported version requires a 'source' commit"
            )
        try:
            source = normalize_commit(raw_source, "source")
        except FleetTargetError as exc:
            raise ReportedVersionError(str(exc)) from exc

        openclaw = cls._normalize_openclaw(_lookup(doc, _OPENCLAW_KEYS), doc)
        return cls(source=source, openclaw=openclaw)

    @staticmethod
    def _normalize_openclaw(
        raw: Any, doc: Mapping[str, Any]
    ) -> Union[ReportedOpenClaw, _Unknown]:
        # A nested openclaw object carries its own version/revision. An explicit
        # unknown token (empty string, "unknown", "none", ...) in the openclaw
        # slot means the agent reported no gateway image. When the openclaw slot
        # is entirely absent, fall back to flat top-level keys
        # (gateway_version/gateway_revision, ...) before concluding "unknown".
        if isinstance(raw, Mapping):
            version = _lookup(raw, _VERSION_KEYS)
            revision = _lookup(raw, _REVISION_KEYS)
        elif raw is None:
            version = _lookup(doc, _VERSION_KEYS)
            revision = _lookup(doc, _REVISION_KEYS)
        elif _is_unknown_token(raw):
            # Explicit "no gateway image" sentinel in the openclaw slot.
            return UNKNOWN
        else:
            raise ReportedVersionError(
                "openclaw report must be an object or an 'unknown' token"
            )

        if _is_unknown_token(version) and _is_unknown_token(revision):
            return UNKNOWN
        if _is_unknown_token(version) or _is_unknown_token(revision):
            raise ReportedVersionError(
                "openclaw report requires both 'version' and 'revision' "
                "(or neither, for an unknown gateway)"
            )
        return ReportedOpenClaw(
            version=str(version).strip(),
            revision=str(revision).strip(),
        )


def _commits_match(reported: str, target: str) -> bool:
    """True when two commits refer to the same revision.

    Both are already lower-cased, hex, 7-40 chars. A short SHA on one side is a
    prefix of the full SHA on the other, so match by common-prefix of the
    shorter length.
    """
    shorter = min(len(reported), len(target))
    return reported[:shorter] == target[:shorter]


@dataclass(frozen=True)
class VersionComparison:
    """The result of comparing one agent's report against its role pin."""

    role: str
    source_matches: bool
    openclaw_matches: bool
    openclaw_unknown: bool

    @property
    def matches(self) -> bool:
        """True only when both tracks match with no unknowns."""
        return self.source_matches and self.openclaw_matches and not self.openclaw_unknown

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "matches": self.matches,
            "source_matches": self.source_matches,
            "openclaw_matches": self.openclaw_matches,
            "openclaw_unknown": self.openclaw_unknown,
        }


def compare_to_target(
    role: str,
    reported: ReportedVersion,
    target: RoleTarget,
) -> VersionComparison:
    """Compare a normalized report against the role's pinned target.

    Because both sides use the pin's fields, the comparison is direct:

    * ``source`` matches by commit-prefix (short vs full SHA);
    * ``openclaw`` matches only when the pin has a gateway track *and* the agent
      reported a gateway whose ``version`` and ``revision`` equal the pin's;
    * a pin with no gateway track (worker-only role) matches iff the agent also
      reported no gateway, so a worker is not spuriously flagged;
    * an agent that reported no gateway image against a gateway pin is
      ``openclaw_unknown`` — distinct from a mismatch — and never counts as a
      match.
    """
    source_matches = _commits_match(reported.source, target.source)

    target_gw: Optional[OpenClawTrack] = target.openclaw
    reported_unknown = reported.gateway_unknown

    if target_gw is None:
        # Worker-only pin: matches only when the agent also reported no gateway.
        openclaw_matches = reported_unknown
        openclaw_unknown = False
    elif reported_unknown:
        openclaw_matches = False
        openclaw_unknown = True
    else:
        gw = reported.openclaw
        assert isinstance(gw, ReportedOpenClaw)  # narrowed by reported_unknown
        openclaw_matches = (
            gw.version == target_gw.version and gw.revision == target_gw.revision
        )
        openclaw_unknown = False

    return VersionComparison(
        role=role,
        source_matches=source_matches,
        openclaw_matches=openclaw_matches,
        openclaw_unknown=openclaw_unknown,
    )
