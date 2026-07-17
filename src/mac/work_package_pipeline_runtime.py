"""Runtime adapters for the bounded work-package assembly pipeline.

The controller in :mod:`mac.work_package_pipeline` is deliberately expressed
in ports.  This module binds two security-sensitive ports to the authoritative
store without giving the external certifier repository or landing authority:

* downstream release is enabled only when one registered repository has a
  validated certification contract and an explicitly enabled landing service;
* the certifier receives a regular, credential-free Git bundle containing the
  exact assembled candidate SHA, never a mutable checkout or credentialed URL.

Bundle files are a rebuildable content cache, not lifecycle authority.  The
database's exact batch/job identities remain authoritative and every cache hit
is revalidated before use.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from mac.gitops import validate_git_ref
from mac.landing_service import LandingService, LandingServiceConfig, RepositoryEndpoint
from mac.models import ValidationError, json_loads, new_id
from mac.openshell_certifier import OpenShellCertificationRunner
from mac.repository_hygiene import redact_repository_hygiene_text
from mac.repository_contract import (
    resolve_repository_canonical_remote,
    validate_secret_free_git_remote,
)
from mac.store import Store
from mac.work_package_certification_service import WorkPackageCertificationService
from mac.work_package_pipeline import (
    PipelineReleaseGate,
    PipelineSnapshot,
    ServicePipelineInventory,
    WorkPackagePipelineConfig,
    WorkPackagePipelineController,
    control_plane_pipeline_observer,
)
from mac.work_package_publication_finalizer import WorkPackagePublicationFinalizer


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _parse_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_CONTROLLER_SSH_ENV_NAMES = frozenset({"SSH_AUTH_SOCK"})

CredentialEnvironment = Callable[[str, Mapping[str, Any]], Mapping[str, str]]
CertificationContractValidator = Callable[..., Any]


@dataclass(frozen=True)
class WorkPackagePipelineRuntimeConfig:
    """Validated process-local bindings around the durable pipeline state."""

    pipeline: WorkPackagePipelineConfig
    landing: LandingServiceConfig
    bundle_dir: Optional[Path]
    bundle_retention_seconds: int = 7 * 24 * 60 * 60
    configuration_error: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.pipeline.enabled and not self.configuration_error)

    @classmethod
    def from_env(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "WorkPackagePipelineRuntimeConfig":
        source = os.environ if environ is None else environ
        errors: list[str] = []

        def boolean(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            errors.append("%s must be a boolean" % name)
            return default

        def integer(name: str, default: int, minimum: int, maximum: int) -> int:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = int(raw)
            except ValueError:
                errors.append("%s must be an integer" % name)
                return default
            if not minimum <= value <= maximum:
                errors.append("%s must be between %d and %d" % (name, minimum, maximum))
                return default
            return value

        def number(name: str, default: float, minimum: float, maximum: float) -> float:
            raw = source.get(name)
            if raw is None or not raw.strip():
                return default
            try:
                value = float(raw)
            except ValueError:
                errors.append("%s must be numeric" % name)
                return default
            if not minimum <= value <= maximum:
                errors.append("%s must be between %s and %s" % (name, minimum, maximum))
                return default
            return value

        # WorkPackagePipelineConfig already owns scheduler bounds.  Supply the
        # selected mapping temporarily so tests and embedded construction do not
        # need to mutate process environment.
        max_actions = integer("MAC_WORK_PACKAGE_PIPELINE_MAX_ACTIONS", 8, 1, 1_000)
        max_items = integer("MAC_WORK_PACKAGE_PIPELINE_MAX_ITEMS", 128, 1, 10_000)
        if max_items < max_actions:
            errors.append(
                "MAC_WORK_PACKAGE_PIPELINE_MAX_ITEMS must be at least MAX_ACTIONS"
            )
            max_items = max_actions
        actor = str(
            source.get("MAC_WORK_PACKAGE_PIPELINE_ACTOR")
            or "work-package-pipeline-controller"
        ).strip()
        if not actor:
            errors.append("MAC_WORK_PACKAGE_PIPELINE_ACTOR must not be empty")
            actor = "work-package-pipeline-controller"
        pipeline = WorkPackagePipelineConfig(
            enabled=boolean("MAC_WORK_PACKAGE_PIPELINE_ENABLED", False),
            interval_seconds=number(
                "MAC_WORK_PACKAGE_PIPELINE_INTERVAL_SECONDS", 10.0, 0.05, 86_400
            ),
            initial_delay_seconds=number(
                "MAC_WORK_PACKAGE_PIPELINE_INITIAL_DELAY_SECONDS", 5.0, 0.0, 86_400
            ),
            max_actions_per_run=max_actions,
            max_items_per_run=max_items,
            max_error_chars=integer(
                "MAC_WORK_PACKAGE_PIPELINE_MAX_ERROR_CHARS", 500, 64, 2_000
            ),
            actor=actor,
        )
        landing_values = {
            "enabled": boolean("MAC_WORK_PACKAGE_LANDING_ENABLED", False),
            "lease_seconds": integer(
                "MAC_WORK_PACKAGE_LANDING_LEASE_SECONDS", 120, 5, 86_400
            ),
            "git_timeout_seconds": integer(
                "MAC_WORK_PACKAGE_GIT_TIMEOUT_SECONDS", 300, 1, 86_400
            ),
            "candidate_namespace": str(
                source.get("MAC_WORK_PACKAGE_CANDIDATE_NAMESPACE")
                or "refs/mac/candidates"
            ).strip(),
        }
        try:
            landing = LandingServiceConfig(**landing_values)
        except (TypeError, ValueError) as exc:
            errors.append("MAC work-package landing configuration is invalid: %s" % exc)
            landing = LandingServiceConfig(
                enabled=False,
                lease_seconds=int(landing_values["lease_seconds"]),
                git_timeout_seconds=int(landing_values["git_timeout_seconds"]),
            )
        raw_bundle_dir = str(source.get("MAC_WORK_PACKAGE_BUNDLE_DIR") or "").strip()
        bundle_dir = Path(raw_bundle_dir).expanduser() if raw_bundle_dir else None
        bundle_retention_seconds = int(
            number(
                "MAC_WORK_PACKAGE_BUNDLE_RETENTION_DAYS",
                7.0,
                0.0,
                3650.0,
            )
            * 24
            * 60
            * 60
        )
        if pipeline.enabled and bundle_dir is None:
            errors.append(
                "MAC_WORK_PACKAGE_BUNDLE_DIR is required when the pipeline is enabled"
            )
        if pipeline.enabled and not landing.enabled:
            errors.append(
                "MAC_WORK_PACKAGE_LANDING_ENABLED must be true when the pipeline is enabled"
            )
        return cls(
            pipeline=(replace(pipeline, enabled=False) if errors else pipeline),
            landing=landing,
            bundle_dir=bundle_dir,
            bundle_retention_seconds=bundle_retention_seconds,
            configuration_error="; ".join(errors),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configuration_error": self.configuration_error,
            "pipeline": self.pipeline.to_dict(),
            "landing": {
                "enabled": self.landing.enabled,
                "lease_seconds": self.landing.lease_seconds,
                "git_timeout_seconds": self.landing.git_timeout_seconds,
                "candidate_namespace": self.landing.candidate_namespace,
            },
            "bundle_dir_configured": self.bundle_dir is not None,
            "bundle_retention_seconds": self.bundle_retention_seconds,
        }


def controller_git_credential_environment(
    _operation: str,
    repository: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Mapping[str, str]:
    """Construct a nonpersistent credential environment for one exact remote.

    HTTPS publication is supported only for a validated ``github.com`` remote.
    The package-owned askpass executable receives ``GH_TOKEN`` in the child
    environment, so neither the token nor an authenticated URL enters argv or
    durable repository metadata.  SSH remotes retain the narrow agent/command
    variables used by existing fleet deployments.  Ambient askpass and Git
    configuration variables are never forwarded.
    """

    source = os.environ if environ is None else environ
    if isinstance(repository, Mapping):
        remote_value = repository.get("source") or repository.get("remote_url")
    else:
        remote_value = getattr(repository, "remote_url", None)
    try:
        remote = validate_secret_free_git_remote(remote_value)
    except ValueError as exc:
        raise ValidationError("controller Git remote is invalid") from exc

    if remote.startswith("/") or remote.startswith("file://"):
        return {}
    if "://" not in remote:
        return _controller_ssh_environment(source)

    parsed = urlsplit(remote)
    scheme = parsed.scheme.lower()
    if scheme == "ssh":
        return _controller_ssh_environment(source)
    if scheme != "https" or (parsed.hostname or "").lower() != "github.com":
        raise ValidationError(
            "controller Git HTTPS credentials support only github.com"
        )
    if parsed.port not in {None, 443}:
        raise ValidationError("controller GitHub HTTPS remote uses an invalid port")

    token = source.get("GH_TOKEN", "")
    if (
        not token
        or token != token.strip()
        or any(character in token for character in ("\x00", "\r", "\n"))
    ):
        raise ValidationError("controller GitHub HTTPS credential is unavailable")
    askpass = Path(sys.executable).with_name("mac-git-askpass")
    if not askpass.is_file() or not os.access(askpass, os.X_OK):
        raise ValidationError("controller GitHub HTTPS askpass helper is unavailable")
    return {
        "GH_TOKEN": token,
        "GIT_ASKPASS": str(askpass),
    }


def _controller_ssh_environment(source: Mapping[str, str]) -> Mapping[str, str]:
    result: dict[str, str] = {}
    for name in sorted(_CONTROLLER_SSH_ENV_NAMES):
        value = source.get(name)
        if value is None or value == "":
            continue
        if "\x00" in value:
            raise ValidationError("controller Git credential environment is invalid")
        result[name] = value
    return result


class RepositoryPipelineReleaseGateResolver:
    """Resolve one secret-free downstream pull signal from locked metadata."""

    def __init__(
        self,
        store: Store,
        *,
        validate_certification_contract: CertificationContractValidator,
        landing_config: LandingServiceConfig,
    ) -> None:
        self.store = store
        self.validate_certification_contract = validate_certification_contract
        self.landing_config = landing_config

    def resolve(self, snapshot: PipelineSnapshot) -> PipelineReleaseGate:
        return self.resolve_package(
            snapshot.package_id,
            plan_version=snapshot.plan_version,
            epoch=snapshot.epoch,
        )

    def resolve_package(
        self,
        package_id: str,
        *,
        plan_version: int,
        epoch: int,
        source: Optional[Any] = None,
    ) -> PipelineReleaseGate:
        """Resolve from either the store or a caller-held transaction.

        Claim admission supplies the transaction that already holds the package
        and repository row locks.  All repository reads and contract validation
        then share that snapshot instead of reopening a pooled connection.
        """

        query = (
            "SELECT repository.id, repository.source, repository.metadata, "
            "repository.enabled FROM work_packages AS package "
            "JOIN project_repositories AS repository "
            "ON repository.id = package.repository_id "
            "WHERE package.id = ? AND package.current_plan_version = ? "
            "AND package.current_epoch = ?"
        )
        params = (package_id, int(plan_version), int(epoch))
        row = (
            self.store.query_one(query, params)
            if source is None
            else source.execute(query, params).fetchone()
        )
        if row is None or int(row["enabled"]) != 1:
            return PipelineReleaseGate(
                False,
                bool(self.landing_config.enabled),
                reason="registered repository is unavailable",
            )
        repository_id = str(row["id"])
        try:
            if source is None:
                self.validate_certification_contract(repository_id)
            else:
                self.validate_certification_contract(repository_id, source=source)
        except Exception:  # validation details stay in controller logs, not the gate.
            return PipelineReleaseGate(
                False,
                bool(self.landing_config.enabled),
                reason="repository certification contract is unavailable or invalid",
            )

        try:
            canonical = resolve_repository_canonical_remote(dict(row))
            endpoint: Optional[RepositoryEndpoint] = RepositoryEndpoint(
                repository_id,
                canonical.url,
                display_name="registered canonical repository",
            )
        except (TypeError, ValueError):
            endpoint = None

        return PipelineReleaseGate(
            True,
            bool(self.landing_config.enabled),
            endpoint=endpoint,
            reason=(
                "canonical landing endpoint is unavailable" if endpoint is None else ""
            ),
        )


class ExactCandidateBundleProvider:
    """Build and revalidate a credential-free bundle for one exact batch."""

    def __init__(
        self,
        store: Store,
        *,
        cache_dir: Path,
        credential_environment: Optional[CredentialEnvironment] = None,
        git_timeout_seconds: int = 300,
        retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        if int(git_timeout_seconds) < 1:
            raise ValueError("bundle Git timeout must be positive")
        self.store = store
        self.cache_dir = Path(cache_dir)
        self.credential_environment = (
            credential_environment or controller_git_credential_environment
        )
        self.git_timeout_seconds = int(git_timeout_seconds)
        self.retention_seconds = max(0, int(retention_seconds))

    def ensure_bundle(self, snapshot: PipelineSnapshot) -> Path:
        if not snapshot.batch_id:
            raise ValidationError("certification bundle requires an exact batch")
        batch = self.store.query_one(
            "SELECT * FROM work_package_integration_batches WHERE id = ?",
            (snapshot.batch_id,),
        )
        if batch is None:
            raise ValidationError("certification batch is unavailable")
        candidate_sha = self._sha(batch["candidate_sha"], "candidate SHA")
        candidate_ref = validate_git_ref(str(batch["candidate_ref"] or ""))
        candidate_tree_digest = str(batch["candidate_tree_digest"] or "")
        expected = (
            snapshot.package_id,
            int(snapshot.plan_version),
            int(snapshot.epoch),
            snapshot.integration_task_id,
            candidate_sha,
        )
        observed = (
            str(batch["package_id"]),
            int(batch["plan_version"]),
            int(batch["epoch"]),
            str(batch["integration_task_id"]),
            candidate_sha,
        )
        if observed != expected:
            raise ValidationError("certification batch identity changed")
        if str(batch["state"]) not in {"verifying", "certified", "published"}:
            raise ValidationError("certification bundle requires an assembled batch")
        if not candidate_tree_digest.startswith("git-tree:"):
            raise ValidationError("assembled candidate tree identity is invalid")

        repository = self.store.query_one(
            "SELECT id, source, metadata, enabled FROM project_repositories WHERE id = ?",
            (batch["repository_id"],),
        )
        if repository is None or int(repository["enabled"]) != 1:
            raise ValidationError("certification repository is unavailable")
        try:
            source = resolve_repository_canonical_remote(dict(repository)).url
        except ValueError as exc:
            raise ValidationError("certification canonical remote is invalid") from exc

        cache = self._cache_path(
            repository_id=str(repository["id"]),
            batch_id=str(batch["id"]),
            candidate_sha=candidate_sha,
            candidate_tree_digest=candidate_tree_digest,
        )
        expected_digest = self._prepared_bundle_digest(str(batch["id"]))
        self._ensure_cache_dir()
        if cache.exists():
            self._validate_regular_bundle(
                cache,
                candidate_sha=candidate_sha,
                expected_digest=expected_digest,
            )
            return cache
        if cache.is_symlink():
            raise ValidationError("certification bundle cache path is unsafe")

        with tempfile.TemporaryDirectory(
            prefix="mac-certification-bundle-",
            dir=str(self.cache_dir),
        ) as raw:
            root = Path(raw)
            repository_dir = root / "repository.git"
            bundle = root / "candidate.bundle"
            environment = self._git_environment(repository, root, source)
            self._git(
                ["init", "--bare", str(repository_dir)], cwd=None, env=environment
            )
            self._git(
                [
                    "fetch",
                    "--no-tags",
                    source,
                    candidate_ref,
                ],
                cwd=repository_dir,
                env=environment,
            )
            fetched = self._git(
                ["rev-parse", "--verify", "FETCH_HEAD^{commit}"],
                cwd=repository_dir,
                env=environment,
            ).stdout.strip()
            if fetched != candidate_sha:
                raise ValidationError(
                    "protected candidate ref no longer names the exact assembled SHA"
                )
            tree = self._git(
                ["rev-parse", "--verify", "%s^{tree}" % candidate_sha],
                cwd=repository_dir,
                env=environment,
            ).stdout.strip()
            if "git-tree:%s" % tree != candidate_tree_digest:
                raise ValidationError("assembled candidate tree identity changed")

            bundle_ref = (
                "refs/mac/certification/%s"
                % hashlib.sha256(
                    (str(batch["id"]) + "\0" + candidate_sha).encode("utf-8")
                ).hexdigest()[:32]
            )
            self._git(
                ["update-ref", bundle_ref, candidate_sha],
                cwd=repository_dir,
                env=environment,
            )
            self._git(
                ["bundle", "create", str(bundle), bundle_ref],
                cwd=repository_dir,
                env=environment,
            )
            self._validate_regular_bundle(
                bundle,
                candidate_sha=candidate_sha,
                expected_digest=expected_digest,
            )
            bundle.chmod(0o400)
            with bundle.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(bundle, cache)
            directory = os.open(self.cache_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self._validate_regular_bundle(
            cache,
            candidate_sha=candidate_sha,
            expected_digest=expected_digest,
        )
        return cache

    def prune(self) -> int:
        """Delete only finalized-product cache entries after retention.

        Unknown files, active/rejected/stale/cancelled products, symlinks, and
        any batch with a runnable certification job are retained.  Published
        products remain evidenced by the canonical landing/finalization chain
        and can be rebuilt from the canonical repository if needed.
        """

        if not self.cache_dir.exists():
            return 0
        self._ensure_cache_dir()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.retention_seconds)
        rows = self.store.query_all(
            "SELECT batch.*, finalization.finalized_at "
            "FROM work_package_integration_batches AS batch "
            "JOIN work_package_publication_finalizations AS finalization "
            "ON finalization.batch_id = batch.id "
            "WHERE batch.state = 'published' AND batch.candidate_sha IS NOT NULL "
            "AND batch.candidate_tree_digest IS NOT NULL ORDER BY batch.id"
        )
        removed = 0
        for row in rows:
            finalized = _parse_timestamp(row["finalized_at"])
            if finalized is None or finalized > cutoff:
                continue
            running = self.store.query_one(
                "SELECT 1 FROM work_package_certification_jobs "
                "WHERE batch_id = ? AND state IN ('queued', 'running') LIMIT 1",
                (row["id"],),
            )
            if running is not None:
                continue
            path = self._cache_path(
                repository_id=str(row["repository_id"]),
                batch_id=str(row["id"]),
                candidate_sha=str(row["candidate_sha"]),
                candidate_tree_digest=str(row["candidate_tree_digest"]),
            )
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                continue
            modified = datetime.fromtimestamp(metadata.st_mtime, tz=timezone.utc)
            if modified > cutoff:
                continue
            path.unlink()
            removed += 1
        return removed

    def _ensure_cache_dir(self) -> None:
        if self.cache_dir.exists():
            mode = self.cache_dir.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValidationError("certification bundle cache directory is unsafe")
        else:
            self.cache_dir.mkdir(parents=True, mode=0o700)
        self.cache_dir.chmod(0o700)

    def _cache_path(
        self,
        *,
        repository_id: str,
        batch_id: str,
        candidate_sha: str,
        candidate_tree_digest: str,
    ) -> Path:
        identity = "\0".join(
            (repository_id, batch_id, candidate_sha, candidate_tree_digest)
        )
        return self.cache_dir / (
            "candidate-%s.bundle" % hashlib.sha256(identity.encode("utf-8")).hexdigest()
        )

    def _prepared_bundle_digest(self, batch_id: str) -> str:
        rows = self.store.query_all(
            "SELECT bundle_digest FROM work_package_certification_jobs WHERE batch_id = ?",
            (batch_id,),
        )
        if len(rows) > 1:
            raise ValidationError(
                "multiple certification jobs exist for one exact batch"
            )
        return str(rows[0]["bundle_digest"] or "") if rows else ""

    def _validate_regular_bundle(
        self,
        path: Path,
        *,
        candidate_sha: str,
        expected_digest: str,
    ) -> None:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValidationError("certification Git bundle is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("certification bundle cache entry is unsafe")
        if metadata.st_size < 16 or metadata.st_size > 2 * 1024 * 1024 * 1024:
            raise ValidationError("certification Git bundle size is invalid")
        digest = self._digest(path)
        if expected_digest and digest != expected_digest:
            raise ValidationError("certification bundle differs from the prepared job")
        result = self._git(
            ["bundle", "list-heads", str(path)],
            cwd=None,
            env=self._base_git_environment(Path(tempfile.gettempdir())),
        )
        heads = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if not any(len(fields) == 2 and fields[0] == candidate_sha for fields in heads):
            raise ValidationError("certification bundle omits the exact candidate SHA")

    def _git_environment(
        self,
        repository: Mapping[str, Any],
        root: Path,
        canonical_remote_url: str,
    ) -> dict[str, str]:
        environment = self._base_git_environment(root)
        credentials = self.credential_environment(
            "read",
            {
                "id": str(repository["id"]),
                "source": canonical_remote_url,
                "metadata": json_loads(repository["metadata"], {}) or {},
            },
        )
        for key, value in credentials.items():
            name = str(key)
            item = str(value)
            if (
                not name
                or "\x00" in name
                or "\x00" in item
                or name in environment
                or name.startswith("GIT_CONFIG_")
            ):
                raise ValidationError("certification credential environment is invalid")
            environment[name] = item
        return environment

    @staticmethod
    def _base_git_environment(root: Path) -> dict[str, str]:
        home = Path(root) / "home"
        home.mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }

    def _git(
        self,
        args: Sequence[str],
        *,
        cwd: Optional[Path],
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.git_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            detail = redact_repository_hygiene_text(
                result.stderr or result.stdout or ""
            ).strip()
            raise ValidationError(
                "certification Git operation failed%s"
                % (": %s" % detail[:500] if detail else "")
            )
        return result

    @staticmethod
    def _sha(value: Any, label: str) -> str:
        result = str(value or "").strip().lower()
        if not _SHA_RE.fullmatch(result):
            raise ValidationError("%s is invalid" % label)
        return result

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return "sha256:%s" % digest.hexdigest()


class WorkPackagePipelineRuntime:
    """Process lifecycle wrapper with configuration diagnostics."""

    def __init__(
        self,
        *,
        controller: WorkPackagePipelineController,
        config: WorkPackagePipelineRuntimeConfig,
        certification: WorkPackageCertificationService,
        landing: LandingService,
        finalization: WorkPackagePublicationFinalizer,
    ) -> None:
        self.controller = controller
        self.config = config
        self.certification = certification
        self.landing = landing
        self.finalization = finalization

    def start(self) -> bool:
        return self.controller.start()

    def stop(self, timeout: float = 5.0) -> bool:
        return self.controller.stop(timeout=timeout)

    def trigger(self) -> bool:
        return self.controller.trigger()

    def status(self) -> dict[str, Any]:
        return {
            **self.controller.status(),
            "runtime": self.config.to_dict(),
        }


def build_work_package_pipeline_runtime(
    control_plane: Any,
    *,
    environ: Optional[Mapping[str, str]] = None,
    certifier_runner: Optional[OpenShellCertificationRunner] = None,
) -> WorkPackagePipelineRuntime:
    """Compose one default-off assembly line around a ControlPlane instance."""

    config = WorkPackagePipelineRuntimeConfig.from_env(environ)
    # The live hub's scheduled and operator-driven paths must share the same
    # durable station objects and configuration.  Explicitly injected
    # environments/runners are test and embedding seams, so they receive
    # isolated adapters without mutating the ControlPlane's public services.
    if environ is None and certifier_runner is None:
        certification = control_plane.work_package_certifications
        landing = control_plane.work_package_landing
    else:
        certification = WorkPackageCertificationService(
            control_plane.store,
            owner=new_id("work-package-pipeline-certifier"),
            runner=certifier_runner,
        )
        landing = LandingService(
            control_plane.store,
            owner=new_id("work-package-pipeline-landing"),
            config=config.landing,
            credential_environment=controller_git_credential_environment,
        )
    finalization = control_plane.work_package_publication_finalizer
    inventory = ServicePipelineInventory(
        list_packages=control_plane.list_work_packages,
        describe_package=control_plane.describe_work_package,
        list_certification_jobs=certification.list,
        paged_catalog=True,
    )
    release_gates = RepositoryPipelineReleaseGateResolver(
        control_plane.store,
        # The service method returns a normalized, validated contract and does
        # not expose it through the gate.  It is intentionally invoked before
        # integration freezes WIP; prepare() locks and rechecks it later.
        validate_certification_contract=certification.validate_repository_contract,
        landing_config=config.landing,
    )
    bundle_dir = config.bundle_dir or Path(tempfile.gettempdir()) / (
        "mac-disabled-certification-bundles"
    )
    bundles = ExactCandidateBundleProvider(
        control_plane.store,
        cache_dir=bundle_dir,
        git_timeout_seconds=config.landing.git_timeout_seconds,
        retention_seconds=config.bundle_retention_seconds,
    )
    controller = WorkPackagePipelineController(
        inventory=inventory,
        release_gates=release_gates,
        bundles=bundles,
        integration=control_plane.work_package_integrations,
        certification=certification,
        landing=landing,
        finalization=finalization,
        rejection=certification,
        config=config.pipeline,
        observer=control_plane_pipeline_observer(control_plane),
        # This identity is part of the durable certification fence.  A stable
        # process-wide label would let two hub replicas both observe themselves
        # as the live owner and execute the same external certifier job.
        owner=new_id("work-package-pipeline"),
    )
    return WorkPackagePipelineRuntime(
        controller=controller,
        config=config,
        certification=certification,
        landing=landing,
        finalization=finalization,
    )


__all__ = [
    "build_work_package_pipeline_runtime",
    "controller_git_credential_environment",
    "ExactCandidateBundleProvider",
    "RepositoryPipelineReleaseGateResolver",
    "WorkPackagePipelineRuntime",
    "WorkPackagePipelineRuntimeConfig",
]
