"""Renew a client credential before it lapses, over the SSH trust root.

A client credential used to be a cliff. Both the hub and the client know
``expires_at`` a month in advance, and neither did anything with it: at the
instant it passed, the client simply stopped authenticating. On 2026-09-23 the
hub-admin credential lapsed mid-session and the hub answered every subsequent
call with "unknown bearer token", which names the wrong cause (see
``client_principals._expiry_reason_from_registry``).

The corrosive part is not the outage, it is what the outage teaches. If a
credential that expires is a credential that silently severs the fleet, then
every finite lifetime is an outage waiting for a date, and the rational
operator response is to set the lifetime enormous. The security property that
expiry exists to provide is then lost -- not because anyone decided short
credentials were wrong, but because the mechanism punished using them. Short
lifetimes are only adoptable when renewal is automatic and invisible, so that
is what this module provides.

**Why SSH and not an API route.** Renewal is authenticated by the *existing
SSH trust root*, the same channel that minted the credential in the first
place (``mac admin client enroll`` is documented "hub-local: invoke through
SSH"). The obvious alternative -- a hub endpoint that lets a caller renew
using its current bearer token -- was rejected deliberately: it would make a
leaked token self-perpetuating. Today a stolen credential is bounded by its
expiry; with token-authenticated self-renewal it could extend itself forever,
which converts a time-boxed compromise into an unbounded one. Requiring SSH
keeps the blast radius of a leaked API token exactly where it is now.

Renewal runs ``mac admin client renew`` on the hub over SSH and installs the
resulting manifest locally. The manifest carries a secret, so it travels
process-to-process over the SSH pipe and is never written to a temporary file,
echoed, or logged.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

_LOG = logging.getLogger("mac.credential_renewal")

#: Fraction of a credential's lifetime after which renewal becomes due.
#: Half gives an equal span of retries between the first attempt and the hard
#: expiry, so a renewal that starts failing is reported while the current
#: credential still authenticates -- which is the entire point of not waiting
#: for the cliff.
DEFAULT_RENEW_AT_FRACTION = 0.5

#: Bounds for the configured fraction. The floor stops a typo from renewing on
#: every invocation; the ceiling keeps a meaningful retry window before expiry.
MIN_RENEW_AT_FRACTION = 0.05
MAX_RENEW_AT_FRACTION = 0.95


class CredentialRenewalError(RuntimeError):
    """Raised when a renewal cannot be planned or applied."""


def configured_renew_fraction(environ: Optional[Mapping[str, str]] = None) -> float:
    """Read ``MAC_CREDENTIAL_RENEW_AT_FRACTION``, falling back to the default.

    An unusable value falls back rather than refusing to renew: declining to
    renew because a setting is malformed would reproduce the exact failure
    this module exists to remove.
    """
    env = os.environ if environ is None else environ
    raw = str(env.get("MAC_CREDENTIAL_RENEW_AT_FRACTION") or "").strip()
    if not raw:
        return DEFAULT_RENEW_AT_FRACTION
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning(
            "MAC_CREDENTIAL_RENEW_AT_FRACTION=%r is not a number; using %.2f",
            raw,
            DEFAULT_RENEW_AT_FRACTION,
        )
        return DEFAULT_RENEW_AT_FRACTION
    if not MIN_RENEW_AT_FRACTION <= value <= MAX_RENEW_AT_FRACTION:
        _LOG.warning(
            "MAC_CREDENTIAL_RENEW_AT_FRACTION=%s outside [%.2f, %.2f]; using %.2f",
            value,
            MIN_RENEW_AT_FRACTION,
            MAX_RENEW_AT_FRACTION,
            DEFAULT_RENEW_AT_FRACTION,
        )
        return DEFAULT_RENEW_AT_FRACTION
    return value


def _parse(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def renewal_status(
    credential: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    fraction: Optional[float] = None,
) -> Dict[str, Any]:
    """Describe where a credential sits in its lifetime.

    Returns ``due`` (past the renewal point), ``expired``, the renewal
    instant, and seconds remaining. A credential with unparseable or missing
    timestamps is reported ``due`` so an unknown state is repaired rather than
    trusted -- an unrenewable credential fails loudly here instead of silently
    at some later request.
    """
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ratio = configured_renew_fraction() if fraction is None else fraction
    issued_at = _parse(credential.get("issued_at"))
    expires_at = _parse(credential.get("expires_at"))
    if expires_at is None:
        return {
            "due": True,
            "expired": False,
            "reason": "credential has no usable expires_at",
            "renew_at": None,
            "expires_at": None,
            "remaining_seconds": None,
        }
    if issued_at is None or expires_at <= issued_at:
        # Without a usable issue time there is no lifetime to take a fraction
        # of; fall back to the fraction applied backwards from expiry over the
        # configured default lifetime rather than guessing a window.
        renew_at = expires_at
        reason = "credential has no usable issued_at; renewing on expiry"
    else:
        span = (expires_at - issued_at).total_seconds()
        renew_at = issued_at + (expires_at - issued_at) * ratio
        reason = "renewal point is %.0f%% through a %.0f day lifetime" % (
            ratio * 100,
            span / 86400.0,
        )
    return {
        "due": instant >= renew_at,
        "expired": instant >= expires_at,
        "reason": reason,
        "renew_at": renew_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "remaining_seconds": int((expires_at - instant).total_seconds()),
    }


@contextlib.contextmanager
def _profile_lock(profile_name: str) -> Iterator[None]:
    """Serialize renewals of one profile on this host.

    Each hub-local renew rotates the token, so two concurrent renewals would
    leave the local profile holding the older of two credentials while the hub
    registry recorded the newer -- locking the client out exactly as if it had
    expired. One lock per profile makes a timer and a hand-run renewal
    converge instead of racing.
    """
    from mac.client_profiles import clients_root

    root = clients_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / (".%s.renew.lock" % profile_name)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - supported targets are POSIX
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except ImportError:  # pragma: no cover
            pass
        os.close(descriptor)


def _default_runner(argv: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def _renew_over_ssh(
    *,
    fleet: str,
    client_id: str,
    fleets_config: Optional[str],
    runner: Callable[[List[str]], Any],
) -> Dict[str, Any]:
    """Run the hub-local renew over SSH and return the enrollment manifest.

    The manifest contains the new token. It is returned in-process and must not
    be logged or written to disk by the caller.
    """
    from mac.fleet_creds import hub_ssh, load_fleets_config, ssh_command

    config = load_fleets_config(fleets_config)
    hub = hub_ssh(config, fleet)
    remote = "mac admin client renew %s" % client_id
    argv = ssh_command(hub, remote)
    result = runner(argv)
    if getattr(result, "returncode", 1) != 0:
        raise CredentialRenewalError(
            "hub renew for %s failed: %s"
            % (client_id, (getattr(result, "stderr", "") or "").strip()[-400:])
        )
    stdout = getattr(result, "stdout", "") or ""
    for chunk in (stdout[stdout.find("{") :], stdout):
        if not chunk.strip():
            continue
        try:
            manifest = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(manifest, Mapping) and manifest.get("credential"):
            return dict(manifest)
    raise CredentialRenewalError(
        "hub renew for %s produced no enrollment manifest" % client_id
    )


def renew_profile(
    profile_name: Optional[str] = None,
    *,
    force: bool = False,
    dry_run: bool = False,
    fleets_config: Optional[str] = None,
    now: Optional[datetime] = None,
    fraction: Optional[float] = None,
    runner: Optional[Callable[[List[str]], Any]] = None,
) -> Dict[str, Any]:
    """Renew one client profile if it is past its renewal point.

    Idempotent and safe to run on a timer: a profile that is not yet due is a
    no-op. Never returns or logs the token -- only the resulting expiry.
    """
    from mac.client_profiles import install_enrollment_manifest, load_profile

    profile = load_profile(profile_name)
    name = str(profile.get("profile") or profile_name or "")
    credential = dict(profile.get("credential") or {})
    fleet = str(profile.get("fleet") or "")
    client_id = str(profile.get("client_id") or "")
    status = renewal_status(credential, now=now, fraction=fraction)
    base: Dict[str, Any] = {
        "profile": name,
        "client_id": client_id,
        "fleet": fleet,
        "expires_at": status["expires_at"],
        "renew_at": status["renew_at"],
        "expired": status["expired"],
        "due": status["due"],
    }
    if not status["due"] and not force:
        return {**base, "status": "not_due", "reason": status["reason"]}
    if not client_id:
        return {**base, "status": "error", "reason": "profile records no client_id"}
    if not fleet:
        # The hub's SSH coordinates are resolved from fleets.yaml by fleet
        # name; without one there is no route to renew over.
        return {**base, "status": "error", "reason": "profile records no fleet"}
    if dry_run:
        return {**base, "status": "would_renew", "reason": status["reason"]}

    with _profile_lock(name):
        # Re-read under the lock: a concurrent renewal may have already moved
        # this profile forward while we waited.
        current = load_profile(name)
        recheck = renewal_status(dict(current.get("credential") or {}), now=now, fraction=fraction)
        if not recheck["due"] and not force:
            return {
                **base,
                "status": "not_due",
                "reason": "renewed concurrently; profile is current",
                "expires_at": recheck["expires_at"],
            }
        manifest = _renew_over_ssh(
            fleet=fleet,
            client_id=client_id,
            fleets_config=fleets_config,
            runner=runner or _default_runner,
        )
        installed = install_enrollment_manifest(manifest, profile_override=name)
    new_expiry = str((manifest.get("credential") or {}).get("expires_at") or "")
    return {
        **base,
        "status": "renewed",
        "expires_at": new_expiry,
        "previous_expires_at": status["expires_at"],
        "credential_id": (manifest.get("credential") or {}).get("id"),
        "profile_path": installed.get("profile_path"),
        "backup": installed.get("backup"),
    }


def renew_due_profiles(
    *,
    profile_names: Optional[List[str]] = None,
    force: bool = False,
    dry_run: bool = False,
    fleets_config: Optional[str] = None,
    now: Optional[datetime] = None,
    fraction: Optional[float] = None,
    runner: Optional[Callable[[List[str]], Any]] = None,
) -> Dict[str, Any]:
    """Renew every due profile, reporting per-profile outcomes.

    One profile's failure never suppresses another's renewal: a fleet with a
    single unreachable hub route must not lose the renewals it could have
    completed.
    """
    from mac.client_profiles import list_profiles

    names = profile_names or [
        str(entry.get("profile") or "")
        for entry in list_profiles()
        if entry.get("profile")
    ]
    results: List[Dict[str, Any]] = []
    for name in names:
        try:
            results.append(
                renew_profile(
                    name,
                    force=force,
                    dry_run=dry_run,
                    fleets_config=fleets_config,
                    now=now,
                    fraction=fraction,
                    runner=runner,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report per profile, keep going
            results.append({"profile": name, "status": "error", "reason": str(exc)})
    counts: Dict[str, int] = {}
    for entry in results:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    return {
        "schema": "mac.credential_renewal.report.v1",
        "renew_at_fraction": configured_renew_fraction() if fraction is None else fraction,
        "counts": counts,
        "profiles": results,
    }
