"""Which merge-serialization mechanism a repository actually has.

## Why this is a stored project attribute and not a per-merge probe

#400 asked the forge on every publication whether the canonical branch had a
merge queue (``gitops.merge_queue_enabled`` -> ``GET /repos/{o}/{r}/rules/
branches/{b}``).  That is one API call per land against a rate limit, to answer
a question whose answer changes maybe twice a year -- and it answers it at the
worst possible moment, inside the merge window, where a rate-limited or flaky
response has to be turned into a landing decision.

So the answer is resolved once and stored on the project's repository record,
in ``project_repositories.metadata`` under
``merge_serialization_capability``.  ``metadata`` is used rather than a new
column deliberately: ``schema.sql`` is ``CREATE TABLE IF NOT EXISTS`` with no
migration framework, so a new column would never appear on the live hub.

## Why it is refreshed by the poller and not by a new timer

``GitHubIssueIngestor`` already visits every registered repository on a timer,
already parses owner/repo out of the remote, and already holds a forge
credential.  Capability resolution rides along on that pass, behind a TTL:
issue ingest wants to run every 60 seconds and merge-queue configuration
essentially never changes, so re-probing the ruleset on every poll would spend
1,440 API calls a day per repository to re-learn the same boolean.  The TTL
(``MAC_MERGE_QUEUE_CAPABILITY_TTL_SECONDS``, default 24h) makes that one call.

Forcing a refresh now needs no new endpoint: ``POST /github-ingest/run`` (CLI:
``mac fleet github-ingest run``) already exists and runs the pass on demand, and
its report carries the capability section, so "not refreshed yet" and
"refreshing and failing every time" are distinguishable from outside -- which is
the whole point, because they look identical when the only signal is a boolean.

## What is recorded, and why each field is separate

``supported`` and ``enabled`` are NOT the same question and are stored
separately.  GitHub merge queues are an organization-only feature: a
User-owned repository CANNOT have one (adding the rule returns HTTP 422
``Invalid rule 'merge_queue'``), while an org-owned repository can have one and
may simply not have turned it on.  Collapsing those into one boolean loses the
difference between "mac must serialize this itself, forever" and "an operator
could switch the forge queue on".  ``forge`` and ``credential`` record whether
the question could be asked at all, and ``resolved_at`` / ``resolver`` record
when and by what -- so an operator can see that an answer is six weeks old
rather than trusting it silently.

## The decision rule

* forge queue supported AND enabled -> the forge queue (#400's path).
* anything else, INCLUDING unknown -> mac's native queue.

"Unknown" is never permission to do an unserialized squash.  The native queue
serializes correctly regardless of what the forge does, so it is the safe branch
and it is where every ambiguous answer lands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from mac.models import JsonDict, ensure_json_object, parse_time, utcnow

CAPABILITY_KEY = "merge_serialization_capability"
CAPABILITY_SCHEMA = "mac.merge_serialization_capability.v1"

DEFAULT_TTL_SECONDS = 86400


def capability_ttl_seconds(environ: Optional[Mapping[str, str]] = None) -> int:
    """How long a resolved capability is trusted before it is re-probed.

    ``MAC_MERGE_QUEUE_CAPABILITY_TTL_SECONDS``, default 86400 (24 hours).
    """

    env = os.environ if environ is None else environ
    try:
        value = int(
            str(env.get("MAC_MERGE_QUEUE_CAPABILITY_TTL_SECONDS", "")).strip()
            or DEFAULT_TTL_SECONDS
        )
    except (TypeError, ValueError):
        value = DEFAULT_TTL_SECONDS
    return max(60, value)


@dataclass(frozen=True)
class MergeCapability:
    """What mac knows about how a repository's branch can be serialized."""

    forge: str = ""
    credential: bool = False
    supported: Optional[bool] = None
    enabled: Optional[bool] = None
    branch: str = ""
    remote: str = ""
    resolved_at: str = ""
    resolver: str = ""
    error: str = ""

    @property
    def use_forge_queue(self) -> bool:
        """Only an unambiguous yes-and-yes routes to the forge."""

        return self.supported is True and self.enabled is True

    def to_dict(self) -> JsonDict:
        return {
            "schema": CAPABILITY_SCHEMA,
            "forge": self.forge,
            "credential": self.credential,
            "supported": self.supported,
            "enabled": self.enabled,
            "branch": self.branch,
            "remote": self.remote,
            "resolved_at": self.resolved_at,
            "resolver": self.resolver,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Any) -> Optional["MergeCapability"]:
        record = ensure_json_object(value if isinstance(value, Mapping) else None)
        if not record or record.get("schema") != CAPABILITY_SCHEMA:
            return None

        def _tri(name: str) -> Optional[bool]:
            raw = record.get(name)
            return None if raw is None else bool(raw)

        return cls(
            forge=str(record.get("forge") or ""),
            credential=bool(record.get("credential")),
            supported=_tri("supported"),
            enabled=_tri("enabled"),
            branch=str(record.get("branch") or ""),
            remote=str(record.get("remote") or ""),
            resolved_at=str(record.get("resolved_at") or ""),
            resolver=str(record.get("resolver") or ""),
            error=str(record.get("error") or ""),
        )

    def is_stale(
        self,
        *,
        branch: str = "",
        ttl_seconds: Optional[int] = None,
        now: Optional[str] = None,
    ) -> bool:
        """A missing, unparseable, wrong-branch, or expired answer is stale.

        Erring toward stale is deliberate: re-probing costs one API call, and
        trusting a stale capability is exactly the "reports healthy while
        enforcing nothing" shape this repository keeps producing.
        """

        if not self.resolved_at or self.enabled is None:
            return True
        if branch and self.branch and branch != self.branch:
            return True
        ttl = capability_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
        try:
            age = (parse_time(now or utcnow()) - parse_time(self.resolved_at)).total_seconds()
        except Exception:  # noqa: BLE001 - an unparseable stamp is stale
            return True
        return age >= ttl or age < 0


def stored_capability(metadata: Any) -> Optional[MergeCapability]:
    """Read the capability out of a ``project_repositories.metadata`` blob."""

    record = ensure_json_object(metadata if isinstance(metadata, Mapping) else None)
    return MergeCapability.from_dict(record.get(CAPABILITY_KEY))


def merge_serialization_mode(capability: Optional[MergeCapability]) -> str:
    """Map a capability (possibly missing) onto a ``merge_serialization`` mode.

    Imported lazily by callers to avoid a cycle; the string values are the same
    contract #400 established.
    """

    from mac.native_merge_queue import MODE_FORGE_QUEUE, MODE_NATIVE_QUEUE

    if capability is not None and capability.use_forge_queue:
        return MODE_FORGE_QUEUE
    return MODE_NATIVE_QUEUE


def resolve_merge_capability(
    remote_url: str,
    branch: str,
    *,
    resolver: str = "github-ingest",
    now: Callable[[], str] = utcnow,
    resolve_forge: Optional[Callable[[str], Optional[str]]] = None,
    queue_enabled: Optional[Callable[..., Optional[bool]]] = None,
    owner_is_organization: Optional[Callable[[str], Optional[bool]]] = None,
) -> MergeCapability:
    """Ask the forge once.  Never raises; an unanswerable question is recorded.

    The injected callables exist so this is testable without a forge and so the
    ingest pass can hand in a token-scoped client.
    """

    from mac import gitops as _gitops

    _resolve_forge = resolve_forge or _gitops.resolve_forge
    _queue_enabled = queue_enabled or _gitops.merge_queue_enabled
    _is_org = owner_is_organization or forge_owner_is_organization

    remote = str(remote_url or "").strip()
    target = str(branch or "").strip()
    stamp = now()
    if not remote or not target:
        return MergeCapability(
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
            error="repository has no canonical remote/branch to probe",
        )
    try:
        forge = str(_resolve_forge(remote) or "")
    except Exception as exc:  # noqa: BLE001 - probing must not raise
        return MergeCapability(
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
            error=_safe(exc),
        )
    if not forge:
        # No API-reachable forge, or no credential for its host.  There is
        # nothing that could serialize this for us, which is a definite answer
        # rather than an unknown: mac's own queue is the only mechanism.
        return MergeCapability(
            forge="",
            credential=False,
            supported=False,
            enabled=False,
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
            error="",
        )
    if forge != "github":
        # gitea has no merge-queue equivalent at all.
        return MergeCapability(
            forge=forge,
            credential=True,
            supported=False,
            enabled=False,
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
        )
    try:
        enabled = _queue_enabled(remote, target)
    except Exception as exc:  # noqa: BLE001
        return MergeCapability(
            forge=forge,
            credential=True,
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
            error=_safe(exc),
        )
    if enabled is None:
        return MergeCapability(
            forge=forge,
            credential=True,
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
            error="forge could not be asked whether %s has a merge queue" % target,
        )
    if enabled:
        # A queue that is on is a queue that is supported.
        return MergeCapability(
            forge=forge,
            credential=True,
            supported=True,
            enabled=True,
            branch=target,
            remote=remote,
            resolved_at=stamp,
            resolver=resolver,
        )
    try:
        organization = _is_org(remote)
    except Exception:  # noqa: BLE001
        organization = None
    return MergeCapability(
        forge=forge,
        credential=True,
        # Support tracks repository ownership: GitHub merge queues are an
        # organization-only feature, so a User-owned repo can never enable one
        # however the operator configures it.  Unknown ownership leaves
        # ``supported`` unknown, which is honest -- and irrelevant to routing,
        # because ``enabled`` is already a definite False.
        supported=None if organization is None else bool(organization),
        enabled=False,
        branch=target,
        remote=remote,
        resolved_at=stamp,
        resolver=resolver,
    )


def forge_owner_is_organization(remote_url: str) -> Optional[bool]:
    """Whether the remote's owner is an Organization (so a queue is possible).

    ``None`` when it cannot be determined.  Reuses ``gitops``' own API context
    so the credential path and the URL parsing stay in one place.
    """

    from mac import gitops as _gitops

    try:
        _host, owner, repo, api_base, headers, _token = _gitops._forge_api_context(
            remote_url
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        payload = _gitops._http_get_json(
            "%s/repos/%s/%s" % (api_base, owner, repo), headers
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    owner_record = payload.get("owner")
    if not isinstance(owner_record, dict):
        return None
    kind = str(owner_record.get("type") or "").strip().lower()
    if not kind:
        return None
    return kind == "organization"


def _safe(exc: BaseException) -> str:
    from mac import gitops as _gitops

    return _gitops._scrub_secret(str(exc))[:300]


def repository_remote_and_branch(repository: Any) -> Dict[str, str]:
    """Pull the canonical remote/branch out of a registered repository record.

    The runtime contract stored in ``metadata['repository_contract']`` is the
    authority for both, exactly as publication treats it -- the canonical
    branch is never assumed to be ``main``.
    """

    metadata = ensure_json_object(getattr(repository, "metadata", None))
    contract = ensure_json_object(metadata.get("repository_contract"))
    remote = str(contract.get("canonical_remote_url") or "").strip()
    branch = str(
        contract.get("canonical_branch") or contract.get("default_branch") or ""
    ).strip()
    return {"remote": remote, "branch": branch}


__all__ = [
    "CAPABILITY_KEY",
    "CAPABILITY_SCHEMA",
    "MergeCapability",
    "capability_ttl_seconds",
    "forge_owner_is_organization",
    "merge_serialization_mode",
    "repository_remote_and_branch",
    "resolve_merge_capability",
    "stored_capability",
]
