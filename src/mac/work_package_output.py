"""Controller-side observation of immutable work-package attempt outputs.

Workers may publish an attempt ref and evidence, but neither is trusted to
describe the resulting repository tree.  This module reads the registered
canonical remote directly, proves the exact protected ref and base ancestry,
derives the changed paths, and checks those paths against the immutable plan
effects.  It deliberately performs no ledger mutation; the orchestration
service can persist the returned observation atomically with candidate state.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from mac.models import JsonDict, ValidationError, json_dumps, utcnow
from mac.repository_contract import resolve_repository_canonical_remote


WORK_PACKAGE_OUTPUT_VERIFIER_VERSION = "work-package-output-verifier-v1"
_FULL_OBJECT_ID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_ATTEMPT_REF_PREFIX = "refs/mac/attempts/"


@dataclass(frozen=True)
class AttemptPathChange:
    """One controller-observed path change.

    A rename/copy records both the old and new path because either side may
    escape a declared scope.  Ordinary changes have only ``path``.
    """

    status: str
    path: str
    source_path: Optional[str] = None

    def to_dict(self) -> JsonDict:
        value: JsonDict = {"status": self.status, "path": self.path}
        if self.source_path is not None:
            value["source_path"] = self.source_path
        return value


@dataclass(frozen=True)
class AttemptOutputObservation:
    repository_id: str
    attempt_ref: str
    base_sha: str
    head_sha: str
    tree_digest: str
    observed_effects_digest: str
    changes: Tuple[AttemptPathChange, ...]
    changed_paths: Tuple[str, ...]
    verifier: str
    verified_at: str

    def to_dict(self) -> JsonDict:
        return {
            "schema": "mac.work_package.output_observation.v1",
            "repository_id": self.repository_id,
            "attempt_ref": self.attempt_ref,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "tree_digest": self.tree_digest,
            "observed_effects_digest": self.observed_effects_digest,
            "changes": [change.to_dict() for change in self.changes],
            "changed_paths": list(self.changed_paths),
            "verifier": self.verifier,
            "verified_at": self.verified_at,
        }


class GitAttemptOutputVerifier:
    """Independently observe one exact attempt ref from the canonical remote."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 120,
        max_diff_entries: int = 20_000,
        git_binary: str = "git",
    ) -> None:
        timeout = int(timeout_seconds)
        maximum = int(max_diff_entries)
        if timeout < 1 or timeout > 3600:
            raise ValidationError("output verification timeout must be 1..3600 seconds")
        if maximum < 1 or maximum > 1_000_000:
            raise ValidationError("output verification diff limit must be 1..1000000")
        self.timeout_seconds = timeout
        self.max_diff_entries = maximum
        self.git_binary = str(git_binary or "git")

    def observe(
        self,
        repository: Mapping[str, Any],
        *,
        attempt_ref: str,
        base_sha: str,
        attempt_base_ref: Optional[str] = None,
        declared_effects: Mapping[str, Any],
        resource_namespace: Mapping[str, Any],
    ) -> AttemptOutputObservation:
        repository_id, source = self._repository_identity(repository)
        ref = self._attempt_ref(attempt_ref)
        base = self._object_id(base_sha, "attempt base")
        namespace = self._resource_namespace(resource_namespace)
        allowed = self._allowed_repository_effects(declared_effects, namespace)

        advertised = self._advertised_ref(source, ref)
        with tempfile.TemporaryDirectory(prefix="mac-attempt-observe-") as raw_root:
            root = Path(raw_root)
            self._run([self.git_binary, "init", "--bare", str(root)])
            local_ref = "refs/mac/controller-observation/attempt"
            self._run(
                [
                    self.git_binary,
                    "-C",
                    str(root),
                    "fetch",
                    "--no-tags",
                    "--force",
                    source,
                    "+%s:%s" % (ref, local_ref),
                ]
            )
            head = self._decode(
                self._run_bytes(
                    [
                        self.git_binary,
                        "-C",
                        str(root),
                        "rev-parse",
                        "--verify",
                        "%s^{commit}" % local_ref,
                    ]
                )
            ).strip().lower()
            head = self._object_id(head, "attempt head")
            if head != advertised:
                raise ValidationError(
                    "attempt ref changed between remote observation and fetch"
                )
            base_present = self._run_raw(
                [
                    self.git_binary,
                    "-C",
                    str(root),
                    "cat-file",
                    "-e",
                    "%s^{commit}" % base,
                ],
                allowed_returncodes=(0, 1, 128),
            )
            if base_present.returncode != 0 and attempt_base_ref:
                base_ref = self._safe_ref(attempt_base_ref, "attempt base ref")
                self._run(
                    [
                        self.git_binary,
                        "-C",
                        str(root),
                        "fetch",
                        "--no-tags",
                        "--force",
                        source,
                        "+%s:refs/mac/controller-observation/base-context" % base_ref,
                    ]
                )
                base_present = self._run_raw(
                    [
                        self.git_binary,
                        "-C",
                        str(root),
                        "cat-file",
                        "-e",
                        "%s^{commit}" % base,
                    ],
                    allowed_returncodes=(0, 1, 128),
                )
            if base_present.returncode != 0:
                raise ValidationError(
                    "assigned attempt base is unavailable from the canonical repository"
                )
            ancestry = self._run_raw(
                [
                    self.git_binary,
                    "-C",
                    str(root),
                    "merge-base",
                    "--is-ancestor",
                    base,
                    head,
                ],
                allowed_returncodes=(0, 1),
            )
            if ancestry.returncode != 0:
                raise ValidationError("attempt output does not descend from its assigned base")

            diff = self._run_bytes(
                [
                    self.git_binary,
                    "-C",
                    str(root),
                    "diff",
                    "--name-status",
                    "-z",
                    "--find-renames",
                    "--find-copies",
                    base,
                    head,
                    "--",
                ]
            )
            changes = self._parse_name_status(diff)
            if len(changes) > self.max_diff_entries:
                raise ValidationError("attempt output exceeds the controller diff-entry limit")
            changed_paths = tuple(
                sorted(
                    {
                        normalized
                        for change in changes
                        for path in (change.source_path, change.path)
                        if path is not None
                        for normalized in (self._normalize_path(path, namespace),)
                    }
                )
            )
            unauthorized = tuple(
                path
                for path in changed_paths
                if not any(self._effect_covers(effect, path) for effect in allowed)
            )
            if unauthorized:
                raise ValidationError(
                    "attempt output changed paths outside the declared effects: %s"
                    % ", ".join(unauthorized[:20])
                )

            tree_listing = self._run_bytes(
                [
                    self.git_binary,
                    "-C",
                    str(root),
                    "ls-tree",
                    "-r",
                    "-z",
                    "--full-tree",
                    head,
                ]
            )
            tree_digest = "sha256:%s" % hashlib.sha256(tree_listing).hexdigest()
            effect_payload = {
                "schema": "mac.work_package.observed_effects.v1",
                "base_sha": base,
                "head_sha": head,
                "changes": [change.to_dict() for change in changes],
                "normalized_changed_paths": list(changed_paths),
            }
            observed_digest = "sha256:%s" % hashlib.sha256(
                json_dumps(effect_payload).encode("utf-8")
            ).hexdigest()
        return AttemptOutputObservation(
            repository_id=repository_id,
            attempt_ref=ref,
            base_sha=base,
            head_sha=head,
            tree_digest=tree_digest,
            observed_effects_digest=observed_digest,
            changes=changes,
            changed_paths=changed_paths,
            verifier=WORK_PACKAGE_OUTPUT_VERIFIER_VERSION,
            verified_at=utcnow(),
        )

    def _advertised_ref(self, source: str, attempt_ref: str) -> str:
        output = self._run_bytes(
            [self.git_binary, "ls-remote", "--exit-code", source, attempt_ref]
        )
        matches = []
        for raw_line in output.splitlines():
            fields = raw_line.split()
            if len(fields) != 2:
                continue
            try:
                ref = fields[1].decode("ascii", errors="strict")
                sha = fields[0].decode("ascii", errors="strict").lower()
            except UnicodeDecodeError:
                continue
            if ref == attempt_ref:
                matches.append(self._object_id(sha, "advertised attempt head"))
        unique = sorted(set(matches))
        if len(unique) != 1:
            raise ValidationError(
                "canonical repository did not advertise exactly one protected attempt ref"
            )
        return unique[0]

    @staticmethod
    def _repository_identity(repository: Mapping[str, Any]) -> Tuple[str, str]:
        try:
            canonical = resolve_repository_canonical_remote(repository)
        except ValueError as exc:
            raise ValidationError(
                "attempt observation requires a valid canonical repository"
            ) from exc
        return canonical.repository_id, canonical.url

    @staticmethod
    def _attempt_ref(value: str) -> str:
        ref = GitAttemptOutputVerifier._safe_ref(value, "attempt output ref")
        if not ref.startswith(_ATTEMPT_REF_PREFIX):
            raise ValidationError("attempt output must use the protected refs/mac/attempts namespace")
        return ref

    @staticmethod
    def _safe_ref(value: str, label: str) -> str:
        ref = str(value or "").strip()
        if not ref.startswith("refs/"):
            raise ValidationError("%s must be a full ref" % label)
        if (
            any(ord(char) < 32 or ord(char) == 127 for char in ref)
            or ref.endswith(("/", ".", ".lock"))
            or ".." in ref
            or "//" in ref
            or "@{" in ref
            or any(char in ref for char in " ~^:?*[\\")
        ):
            raise ValidationError("%s is unsafe" % label)
        return ref

    @staticmethod
    def _object_id(value: str, label: str) -> str:
        sha = str(value or "").strip().lower()
        if not _FULL_OBJECT_ID.fullmatch(sha):
            raise ValidationError("%s must be a full Git object id" % label)
        return sha

    @staticmethod
    def _resource_namespace(value: Mapping[str, Any]) -> JsonDict:
        namespace = dict(value or {})
        if namespace.get("status") != "resolved":
            # Admission adds repo:* for unresolved mutation scopes.  Retaining
            # a conservative namespace here prevents a verifier from guessing
            # host-specific path semantics after the fact.
            return {
                "status": "unresolved",
                "case_sensitive": False,
                "unicode_normalization": "NFC",
            }
        normalization = str(namespace.get("unicode_normalization") or "").upper()
        if normalization not in {"NFC", "NFD", "NFKC", "NFKD"}:
            raise ValidationError("attempt resource namespace is malformed")
        if not isinstance(namespace.get("case_sensitive"), bool):
            raise ValidationError("attempt resource namespace is malformed")
        return {
            "status": "resolved",
            "case_sensitive": bool(namespace["case_sensitive"]),
            "unicode_normalization": normalization,
        }

    def _allowed_repository_effects(
        self,
        effects: Mapping[str, Any],
        namespace: Mapping[str, Any],
    ) -> Tuple[str, ...]:
        if not isinstance(effects, Mapping):
            raise ValidationError("declared attempt effects are malformed")
        if namespace.get("status") != "resolved":
            exclusive = effects.get("exclusive") or []
            if not isinstance(exclusive, list) or not any(
                item in {"*", "repo:*"} for item in exclusive
            ):
                raise ValidationError(
                    "unresolved resource namespace requires repository-wide exclusive scope"
                )
        allowed = []
        for kind in ("writes", "exclusive"):
            raw = effects.get(kind) or []
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ValidationError("declared attempt %s effects are malformed" % kind)
            for item in raw:
                if item in {"*", "repo:*"}:
                    allowed.append(item)
                elif "://" not in item:
                    allowed.append(self._normalize_path(item, namespace))
        return tuple(sorted(set(allowed)))

    @staticmethod
    def _normalize_path(path: str, namespace: Mapping[str, Any]) -> str:
        value = str(path)
        if not value or "\x00" in value:
            raise ValidationError("attempt output contains an invalid repository path")
        if value.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[/\\]", value):
            raise ValidationError("attempt output contains an absolute repository path")
        normalization = str(namespace.get("unicode_normalization") or "NFC")
        value = unicodedata.normalize(normalization, value).replace("\\", "/")
        if not bool(namespace.get("case_sensitive")):
            value = value.casefold()
        parts = []
        for part in value.split("/"):
            if not part or part == ".":
                continue
            if part == "..":
                raise ValidationError("attempt output contains a parent path segment")
            parts.append(part)
        if not parts:
            raise ValidationError("attempt output contains an empty repository path")
        return "/".join(parts)

    @staticmethod
    def _effect_covers(effect: str, path: str) -> bool:
        return effect in {"*", "repo:*"} or path == effect or path.startswith(
            effect.rstrip("/") + "/"
        )

    @staticmethod
    def _parse_name_status(payload: bytes) -> Tuple[AttemptPathChange, ...]:
        if not payload:
            return ()
        fields = payload.split(b"\x00")
        if fields[-1] != b"":
            raise ValidationError("Git returned an unterminated attempt diff")
        fields.pop()
        changes = []
        index = 0
        while index < len(fields):
            try:
                status = fields[index].decode("ascii", errors="strict")
            except UnicodeDecodeError as exc:
                raise ValidationError("Git returned an invalid attempt diff status") from exc
            index += 1
            if not status or status[0] not in "ACDMRTUXB":
                raise ValidationError("Git returned an unsupported attempt diff status")
            is_pair = status[0] in {"R", "C"}
            needed = 2 if is_pair else 1
            if index + needed > len(fields):
                raise ValidationError("Git returned a truncated attempt diff")
            paths = []
            for raw_path in fields[index : index + needed]:
                try:
                    paths.append(raw_path.decode("utf-8", errors="strict"))
                except UnicodeDecodeError as exc:
                    raise ValidationError(
                        "attempt output contains a non-UTF-8 repository path"
                    ) from exc
            index += needed
            if is_pair:
                changes.append(
                    AttemptPathChange(
                        status=status,
                        source_path=paths[0],
                        path=paths[1],
                    )
                )
            else:
                changes.append(AttemptPathChange(status=status, path=paths[0]))
        return tuple(changes)

    def _run(self, command: Sequence[str]) -> None:
        self._run_bytes(command)

    def _run_bytes(self, command: Sequence[str]) -> bytes:
        return self._run_raw(command).stdout

    def _run_raw(
        self,
        command: Sequence[str],
        *,
        allowed_returncodes: Tuple[int, ...] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "SSH_ASKPASS": "",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("controller attempt observation failed") from exc
        if result.returncode not in allowed_returncodes:
            # Never surface stderr/stdout: credential helpers and authenticated
            # URLs can appear in Git diagnostics.
            raise ValidationError("controller attempt observation failed")
        return result

    @staticmethod
    def _decode(value: bytes) -> str:
        try:
            return value.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValidationError("Git returned a non-ASCII object id") from exc


__all__ = [
    "AttemptOutputObservation",
    "AttemptPathChange",
    "GitAttemptOutputVerifier",
    "WORK_PACKAGE_OUTPUT_VERIFIER_VERSION",
]
