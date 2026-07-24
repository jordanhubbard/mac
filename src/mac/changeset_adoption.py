"""Controller changeset-adoption core: binding and attestation.

A controller adopts a proposed changeset by *binding* it to a concrete
target and generation, then *attesting* that binding so downstream consumers
can detect tampering.  The module is deliberately self-contained: pure
dataclasses and pure functions with no live I/O, no network, and no clock
reads beyond the ``mac.models`` helpers reused for deterministic identifiers
and timestamps.

Two cohesive halves
-------------------
1. BINDING
   ``ChangesetProposal`` captures the raw controller intent (changeset id,
   target, generation, optional labels).  ``build_adoption_binding`` /
   ``bind_changeset`` validate that intent, reject empty, null-byte, or
   duplicate-label inputs, and produce a deterministic ``AdoptionBinding``
   record whose ``content_hash`` is a stable SHA-256 over the canonical
   binding fields.
2. ATTESTATION
   ``attest_adoption`` computes an HMAC-SHA256 digest over the canonical
   binding payload using a controller-held key, yielding an
   ``AdoptionAttestation``.  ``verify_adoption_attestation`` recomputes the
   digest and constant-time compares it, detecting both binding tampering
   and wrong-key signatures.

Status vocabulary (``AdoptionBinding.status``)
    proposed   — binding built from a validated proposal, not yet attested
    attested   — an attestation has been produced over the binding
    revoked    — binding withdrawn; retained for audit but not adoptable

Stage vocabulary (``AdoptionBinding.stage``)
    bind       — the binding half has produced a deterministic record
    attest     — the attestation half has sealed the binding

Schema constants pin wire compatibility, mirroring ``ROLLOUT_PLAN_SCHEMA`` in
``mac.openclaw_fleet_rollout`` and the ``*_SCHEMA`` constants in
``mac.deployment_attestation``.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from mac.models import json_dumps, new_id, utcnow

# Exported schema identifiers.  Consumers pin compatibility against these
# values, following the ``mac.<module>.vN`` convention used by adjacent
# modules (e.g. ``ROLLOUT_PLAN_SCHEMA`` in openclaw_fleet_rollout.py).
ADOPTION_PROPOSAL_SCHEMA = "mac.changeset_adoption.v1"
ADOPTION_BINDING_SCHEMA = "mac.changeset_adoption.v1"
ADOPTION_ATTESTATION_SCHEMA = "mac.changeset_adoption.v1"

# Explicit status/stage vocabularies (documented in the module docstring).
BINDING_STATUS_PROPOSED = "proposed"
BINDING_STATUS_ATTESTED = "attested"
BINDING_STATUS_REVOKED = "revoked"
BINDING_STATUSES: Tuple[str, ...] = (
    BINDING_STATUS_PROPOSED,
    BINDING_STATUS_ATTESTED,
    BINDING_STATUS_REVOKED,
)

BINDING_STAGE_BIND = "bind"
BINDING_STAGE_ATTEST = "attest"
BINDING_STAGES: Tuple[str, ...] = (BINDING_STAGE_BIND, BINDING_STAGE_ATTEST)

# HMAC construction identifier baked into the attestation payload so the
# digest is domain-separated from other module signatures.
_ATTESTATION_ALGORITHM = "hmac-sha256"


class ChangesetAdoptionError(ValueError):
    """Raised when the changeset-adoption contract is violated."""


# ---------------------------------------------------------------------------
# Validation helpers (pure)
# ---------------------------------------------------------------------------


def _required(value: Any, label: str) -> str:
    """Return a trimmed non-empty, null-byte-free string or raise."""

    text = str(value if value is not None else "").strip()
    if not text:
        raise ChangesetAdoptionError("%s is required" % label)
    if "\x00" in text:
        raise ChangesetAdoptionError("%s must not contain a null byte" % label)
    return text


def _generation(value: Any) -> int:
    """Coerce and validate a non-negative integer generation."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise ChangesetAdoptionError("generation must be an integer")
    if value < 0:
        raise ChangesetAdoptionError("generation must be non-negative")
    return value


def _labels(value: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """Validate an optional label mapping, rejecting duplicate keys."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ChangesetAdoptionError("labels must be a mapping")
    result: Dict[str, str] = {}
    seen: set[str] = set()
    for raw_key, raw_val in value.items():
        key = _required(raw_key, "label key")
        if key in seen:
            raise ChangesetAdoptionError("duplicate label key: %s" % key)
        seen.add(key)
        result[key] = _required(raw_val, "label value for %s" % key)
    return dict(sorted(result.items()))


# ---------------------------------------------------------------------------
# BINDING
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangesetProposal:
    """Raw controller intent to adopt a changeset onto a target."""

    changeset_id: str
    target: str
    generation: int
    labels: Dict[str, str] = field(default_factory=dict)
    schema: str = ADOPTION_PROPOSAL_SCHEMA

    def canonical(self) -> Dict[str, Any]:
        """Deterministic, sorted representation of the proposal."""

        return {
            "changeset_id": self.changeset_id,
            "generation": self.generation,
            "labels": dict(sorted(self.labels.items())),
            "schema": self.schema,
            "target": self.target,
        }


@dataclass(frozen=True)
class AdoptionBinding:
    """A validated, deterministic binding of a changeset to a target."""

    binding_id: str
    changeset_id: str
    target: str
    generation: int
    labels: Dict[str, str]
    content_hash: str
    status: str
    stage: str
    created_at: str
    schema: str = ADOPTION_BINDING_SCHEMA

    def canonical(self) -> Dict[str, Any]:
        """Canonical fields covered by the content hash and attestation.

        Deliberately excludes ``binding_id``, ``created_at``, ``status`` and
        ``stage`` so the hash is a stable function of the adopted content and
        two identical proposals yield identical ``content_hash`` values.
        """

        return {
            "changeset_id": self.changeset_id,
            "generation": self.generation,
            "labels": dict(sorted(self.labels.items())),
            "schema": self.schema,
            "target": self.target,
        }


def _content_hash(canonical: Mapping[str, Any]) -> str:
    payload = json_dumps(canonical).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_adoption_binding(
    changeset_id: str,
    target: str,
    generation: int,
    *,
    labels: Optional[Mapping[str, Any]] = None,
) -> AdoptionBinding:
    """Validate inputs and produce a deterministic ``AdoptionBinding``.

    Rejects empty, whitespace-only, or null-byte inputs, non-integer or
    negative generations, and duplicate label keys.  The ``content_hash`` is a
    stable SHA-256 over the canonical binding fields, so repeated calls with
    equal content produce equal hashes.
    """

    resolved_id = _required(changeset_id, "changeset_id")
    resolved_target = _required(target, "target")
    resolved_generation = _generation(generation)
    resolved_labels = _labels(labels)

    binding = AdoptionBinding(
        binding_id=new_id("adopt"),
        changeset_id=resolved_id,
        target=resolved_target,
        generation=resolved_generation,
        labels=resolved_labels,
        content_hash="",
        status=BINDING_STATUS_PROPOSED,
        stage=BINDING_STAGE_BIND,
        created_at=utcnow(),
    )
    # Recompute with the finalized canonical fields.
    canonical = AdoptionBinding.canonical(binding)
    return _replace_hash(binding, _content_hash(canonical))


def bind_changeset(proposal: ChangesetProposal) -> AdoptionBinding:
    """Bind a ``ChangesetProposal`` into a deterministic ``AdoptionBinding``."""

    if not isinstance(proposal, ChangesetProposal):
        raise ChangesetAdoptionError("proposal must be a ChangesetProposal")
    if proposal.schema != ADOPTION_PROPOSAL_SCHEMA:
        raise ChangesetAdoptionError("unsupported proposal schema")
    return build_adoption_binding(
        proposal.changeset_id,
        proposal.target,
        proposal.generation,
        labels=proposal.labels,
    )


def _replace_hash(binding: AdoptionBinding, content_hash: str) -> AdoptionBinding:
    return AdoptionBinding(
        binding_id=binding.binding_id,
        changeset_id=binding.changeset_id,
        target=binding.target,
        generation=binding.generation,
        labels=binding.labels,
        content_hash=content_hash,
        status=binding.status,
        stage=binding.stage,
        created_at=binding.created_at,
        schema=binding.schema,
    )


# ---------------------------------------------------------------------------
# ATTESTATION
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptionAttestation:
    """An HMAC-SHA256 attestation sealing an ``AdoptionBinding``."""

    binding_id: str
    content_hash: str
    algorithm: str
    digest: str
    key_fingerprint: str
    created_at: str
    schema: str = ADOPTION_ATTESTATION_SCHEMA


def _resolve_key(key: Any) -> bytes:
    if isinstance(key, (bytes, bytearray)):
        raw = bytes(key)
    else:
        raw = _required(key, "attestation key").encode("utf-8")
    if not raw:
        raise ChangesetAdoptionError("attestation key is required")
    return raw


def _key_fingerprint(key: bytes) -> str:
    return "sha256:" + hashlib.sha256(key).hexdigest()[:16]


def _digest(key: bytes, binding: AdoptionBinding) -> str:
    payload = json_dumps(
        {
            "algorithm": _ATTESTATION_ALGORITHM,
            "binding": binding.canonical(),
            "content_hash": binding.content_hash,
            "schema": ADOPTION_ATTESTATION_SCHEMA,
        }
    ).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def attest_adoption(binding: AdoptionBinding, key: Any) -> AdoptionAttestation:
    """Produce an ``AdoptionAttestation`` over ``binding`` using ``key``.

    The digest is an HMAC-SHA256 over the canonical binding payload plus its
    content hash, so any change to adopted content or a wrong key yields a
    different digest that ``verify_adoption_attestation`` rejects.
    """

    if not isinstance(binding, AdoptionBinding):
        raise ChangesetAdoptionError("binding must be an AdoptionBinding")
    if not binding.content_hash:
        raise ChangesetAdoptionError("binding is missing a content hash")
    resolved_key = _resolve_key(key)
    return AdoptionAttestation(
        binding_id=binding.binding_id,
        content_hash=binding.content_hash,
        algorithm=_ATTESTATION_ALGORITHM,
        digest=_digest(resolved_key, binding),
        key_fingerprint=_key_fingerprint(resolved_key),
        created_at=utcnow(),
    )


def verify_adoption_attestation(
    binding: AdoptionBinding,
    attestation: AdoptionAttestation,
    key: Any,
) -> bool:
    """Return ``True`` iff ``attestation`` matches ``binding`` under ``key``.

    Detects tampering with either the binding content or the attestation
    fields, and rejects a valid-looking digest produced under a different key.
    Comparison is constant-time.
    """

    if not isinstance(binding, AdoptionBinding):
        raise ChangesetAdoptionError("binding must be an AdoptionBinding")
    if not isinstance(attestation, AdoptionAttestation):
        raise ChangesetAdoptionError("attestation must be an AdoptionAttestation")
    resolved_key = _resolve_key(key)

    if attestation.algorithm != _ATTESTATION_ALGORITHM:
        return False
    if attestation.schema != ADOPTION_ATTESTATION_SCHEMA:
        return False
    if attestation.binding_id != binding.binding_id:
        return False
    if attestation.content_hash != binding.content_hash:
        return False
    if attestation.key_fingerprint != _key_fingerprint(resolved_key):
        return False

    expected = _digest(resolved_key, binding)
    return hmac.compare_digest(expected, attestation.digest)
