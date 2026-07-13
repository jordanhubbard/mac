"""Guarded auto-repair path for high-confidence dream-cycle skill findings.

When the dream pipeline classifies a candidate at ``overall_confidence=high``
and the affected area is a checked-in skill asset, this module stages the edit
safely before any commit or push:

* **Allowlist guard** — only paths inside ``skills/`` or ``deploy/skills/``
  (relative to the repository root) may be written.  Absolute paths, ``..``
  traversals, and paths outside the allowlisted prefixes are rejected.
* **Evidence gate** — at least one evidence record with a non-empty excerpt
  must be attached to the candidate before staging is permitted.
* **Secret / identity scrubber** — the proposed patch text is scanned for
  bearer tokens, known credential prefixes, long opaque atoms, home-directory
  paths, agent identifiers, and email addresses.  Any finding causes a hard
  refusal; the caller receives a ``refused`` status with the reason recorded.
* **Fleet-generic documentation constraint** — staged content must not embed
  operator-identity tokens (the same token list enforced by
  ``test_docs_no_operator_identity``).  A violation is a refusal, not a
  warning.
* **Auditable patch summary** — every ``stage_skill_patch`` call returns a
  JSON-serialisable result dict that records the disposition
  (``staged`` / ``refused`` / ``error``), the target path, the evidence
  fingerprint, and the specific refusal reason when applicable.  No patch
  is written until all guards pass.

The module is intentionally dependency-light: it imports only ``re``,
``hashlib``, ``json``, ``os``, ``pathlib``, and ``typing`` from the standard
library plus ``mac.models.JsonDict``.  It never imports from ``mac.services``
so it can be called from any stage of the pipeline.

Schema
------
``stage_skill_patch`` returns::

    {
        "schema": "mac.skill_auto_repair.v1",
        "status": "staged" | "refused" | "error",
        "target_path": str,          # relative path as provided
        "evidence_fingerprint": str, # sha256 of evidence text
        "reason": str | None,        # set when status != "staged"
        "patch_lines": int | None,   # line count of patch when staged
        "audit": [                   # list of check names that passed
            "allowlist",
            "evidence",
            "secret_scan",
            "identity_scan",
        ],
    }
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from mac.models import JsonDict

# ---------------------------------------------------------------------------
# Public schema identifier
# ---------------------------------------------------------------------------

SKILL_AUTO_REPAIR_SCHEMA = "mac.skill_auto_repair.v1"

# ---------------------------------------------------------------------------
# Allowlisted path prefixes (POSIX, relative to repo root)
# ---------------------------------------------------------------------------

_ALLOWED_PREFIXES: tuple[str, ...] = (
    "skills/",
    "deploy/skills/",
)

# ---------------------------------------------------------------------------
# Operator-identity tokens (mirrors test_docs_no_operator_identity)
# ---------------------------------------------------------------------------

_IDENTITY_TOKENS: tuple[str, ...] = (
    "rocky",
    "natasha",
    "bullwinkle",
    "madmax",
    "puck",
    "sparky",
    "worker2",
    "jkh",
    "hosta",
    "hostb",
    "hostc",
    "hostd",
    "hoste",
    "hostf",
    "devuser",
    "agentuser",
)

_IDENTITY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(t) for t in _IDENTITY_TOKENS) + r")(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])do-host",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Secret / sensitive-value patterns
# ---------------------------------------------------------------------------

_URL_USERINFO_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@\s]+@")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)[\"']?(?:api[_-]?key|token|secret|password|authorization|"
    r"access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*[\"']?[^\"'\s,;}]{8,}"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_LONG_ATOM_RE = re.compile(r"\b[A-Za-z0-9_./+=-]{96,}\b")
_HOME_PATH_RE = re.compile(r"(?i)(/Users|/home)/[A-Za-z0-9._-]+")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

_SECRET_CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url_with_credentials", _URL_USERINFO_RE),
    ("bearer_token", _BEARER_RE),
    ("secret_assignment", _SECRET_ASSIGN_RE),
    ("known_token_pattern", _KNOWN_TOKEN_RE),
    ("long_opaque_atom", _LONG_ATOM_RE),
    ("home_directory_path", _HOME_PATH_RE),
    ("email_address", _EMAIL_RE),
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_path(raw: str) -> Optional[str]:
    """Return the POSIX path string if it is safe, else None.

    Safety means:
    * No absolute path component.
    * No ``..`` traversal segments after normalisation.
    * Matches at least one allowlisted prefix.
    """
    try:
        p = Path(raw)
    except Exception:  # noqa: BLE001
        return None
    if p.is_absolute():
        return None
    # Resolve traversal by assembling a fake root join and checking containment.
    fake_root = Path("/repo")
    resolved = (fake_root / p).resolve()
    if not str(resolved).startswith(str(fake_root)):
        return None
    rel = resolved.relative_to(fake_root).as_posix()
    for prefix in _ALLOWED_PREFIXES:
        if rel.startswith(prefix):
            return rel
    return None


def _evidence_fingerprint(evidence: Sequence[Mapping[str, Any]]) -> str:
    """Stable SHA-256 fingerprint of the evidence list."""
    canon = json.dumps(
        [
            {k: str(v) for k, v in item.items() if v is not None}
            for item in evidence
            if isinstance(item, Mapping)
        ],
        sort_keys=True,
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def _has_evidence_with_excerpt(evidence: Sequence[Mapping[str, Any]]) -> bool:
    """Return True if at least one evidence record has a non-empty excerpt."""
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        excerpt = str(item.get("excerpt") or "").strip()
        if excerpt:
            return True
    return False


def _scan_secrets(text: str) -> Optional[str]:
    """Return a description of the first secret pattern found, or None."""
    for name, pattern in _SECRET_CHECKS:
        if pattern.search(text):
            return name
    return None


def _scan_identity(text: str) -> Optional[str]:
    """Return the matched identity token if any fleet-specific identity is found, or None."""
    match = _IDENTITY_RE.search(text)
    if match:
        return match.group(0)
    return None


def _result(
    *,
    status: str,
    target_path: str,
    evidence_fingerprint: str,
    reason: Optional[str] = None,
    patch_lines: Optional[int] = None,
    audit: list[str],
) -> JsonDict:
    return {
        "schema": SKILL_AUTO_REPAIR_SCHEMA,
        "status": status,
        "target_path": target_path,
        "evidence_fingerprint": evidence_fingerprint,
        "reason": reason,
        "patch_lines": patch_lines,
        "audit": audit,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def stage_skill_patch(
    target_path: str,
    patch_text: str,
    evidence: Iterable[Mapping[str, Any]],
    *,
    repo_root: Optional[str | Path] = None,
    dry_run: bool = False,
) -> JsonDict:
    """Validate and optionally write a skill patch, returning an audit report.

    Parameters
    ----------
    target_path:
        Repository-relative path to the skill file to patch (e.g.
        ``skills/mac-agent-terminal-timeout/SKILL.md``).
    patch_text:
        The full new content to write to ``target_path``.  Must not contain
        secrets, operator identity tokens, or sensitive values.
    evidence:
        Iterable of evidence records (dicts).  At least one record must carry
        a non-empty ``excerpt`` field.
    repo_root:
        Optional filesystem path to the repository root.  When provided and
        ``dry_run`` is False, the file is written after all guards pass.
        When None or absent, the patch is validated but not written (useful
        for in-process testing).
    dry_run:
        When True, skip the write step even if ``repo_root`` is supplied.
        Useful for callers that want to validate without side effects.

    Returns
    -------
    dict
        Audit report dict (see module docstring for schema).
    """
    evidence_list = [item for item in evidence if isinstance(item, Mapping)]
    fingerprint = _evidence_fingerprint(evidence_list)
    audit: list[str] = []

    # 1. Allowlist guard.
    safe_path = _normalise_path(target_path)
    if safe_path is None:
        return _result(
            status="refused",
            target_path=target_path,
            evidence_fingerprint=fingerprint,
            reason="path_not_allowlisted: target must be under skills/ or deploy/skills/ "
                   "and must not contain traversal sequences",
            audit=audit,
        )
    audit.append("allowlist")

    # 2. Evidence gate.
    if not _has_evidence_with_excerpt(evidence_list):
        return _result(
            status="refused",
            target_path=target_path,
            evidence_fingerprint=fingerprint,
            reason="evidence_required: at least one evidence record with a non-empty "
                   "excerpt must be provided before staging a skill patch",
            audit=audit,
        )
    audit.append("evidence")

    # 3. Secret scan.
    secret_hit = _scan_secrets(patch_text)
    if secret_hit is not None:
        return _result(
            status="refused",
            target_path=target_path,
            evidence_fingerprint=fingerprint,
            reason="secret_detected: patch text contains a sensitive value (%s); "
                   "remove all credentials, tokens, and home-directory paths before "
                   "staging" % secret_hit,
            audit=audit,
        )
    audit.append("secret_scan")

    # 4. Identity scan.
    identity_hit = _scan_identity(patch_text)
    if identity_hit is not None:
        return _result(
            status="refused",
            target_path=target_path,
            evidence_fingerprint=fingerprint,
            reason="identity_detected: patch text contains an operator-identity token "
                   "(%r); use generic role names (hub, worker-1, worker-2, gpu-worker) "
                   "or placeholders (<user>, <host>, <mesh-ip>) instead" % identity_hit,
            audit=audit,
        )
    audit.append("identity_scan")

    patch_lines = len(patch_text.splitlines())

    # 5. Write (when repo_root is given and dry_run is False).
    if repo_root is not None and not dry_run:
        try:
            dest = Path(repo_root) / safe_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(patch_text, encoding="utf-8")
        except OSError as exc:
            return _result(
                status="error",
                target_path=target_path,
                evidence_fingerprint=fingerprint,
                reason="write_failed: %s" % exc,
                audit=audit,
            )

    return _result(
        status="staged",
        target_path=target_path,
        evidence_fingerprint=fingerprint,
        patch_lines=patch_lines,
        audit=audit,
    )


def stage_skill_patches(
    patches: Iterable[Mapping[str, Any]],
    *,
    repo_root: Optional[str | Path] = None,
    dry_run: bool = False,
) -> JsonDict:
    """Stage multiple skill patches in one call, returning a batch report.

    Each item in ``patches`` must be a mapping with keys:
    * ``target_path`` (str)
    * ``patch_text`` (str)
    * ``evidence`` (list of dicts)

    Returns
    -------
    dict
        Batch report with schema ``mac.skill_auto_repair_batch.v1``, including
        per-patch result dicts under ``results`` and aggregate counts.
    """
    results: list[JsonDict] = []
    staged = refused = errors = 0
    for item in patches:
        if not isinstance(item, Mapping):
            continue
        result = stage_skill_patch(
            str(item.get("target_path") or ""),
            str(item.get("patch_text") or ""),
            list(item.get("evidence") or []),
            repo_root=repo_root,
            dry_run=dry_run,
        )
        results.append(result)
        if result["status"] == "staged":
            staged += 1
        elif result["status"] == "refused":
            refused += 1
        else:
            errors += 1
    return {
        "schema": "mac.skill_auto_repair_batch.v1",
        "staged": staged,
        "refused": refused,
        "errors": errors,
        "results": results,
    }


__all__ = [
    "SKILL_AUTO_REPAIR_SCHEMA",
    "stage_skill_patch",
    "stage_skill_patches",
]
