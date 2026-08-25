"""Project-repository registration and contract access.

The service depends on explicit ports instead of the ControlPlane facade.  That
keeps repository registration independently testable and prevents the common
"extracted class with a ControlPlane backreference" failure mode.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from mac.models import (
    JsonDict,
    NotFoundError,
    ProjectRepository,
    ValidationError,
    coerce_list,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


ContractLoader = Callable[[Path], JsonDict]
CodeGraphInitializer = Callable[[Path], JsonDict]
CodeGraphValidator = Callable[[JsonDict], None]
LogRecorder = Callable[..., Any]


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip("-._").lower()
    return slug or "repo"


class ProjectRepositoryService:
    """Own persistence and runtime-contract behavior for registered repos."""

    def __init__(
        self,
        store: Any,
        *,
        load_contract: ContractLoader,
        initialize_codegraph: CodeGraphInitializer,
        validate_codegraph: CodeGraphValidator,
        record_log: LogRecorder,
    ) -> None:
        self._store = store
        self._load_contract = load_contract
        self._initialize_codegraph = initialize_codegraph
        self._validate_codegraph = validate_codegraph
        self._record_log = record_log

    def project_repository_url(self, project: Optional[str]) -> Optional[str]:
        if not project:
            return None
        row = self._store.query_one(
            "SELECT metadata FROM projects WHERE name = ? OR id = ?",
            (project, project),
        )
        if row is None:
            return None
        metadata = ensure_json_object(json_loads(row["metadata"], {}))
        value = metadata.get("repository_url")
        return value.strip() if isinstance(value, str) and value.strip() else None

    def repository_for_project(self, project: Optional[str]) -> Optional[ProjectRepository]:
        if not project:
            return None
        row = self._store.query_one(
            """
            SELECT * FROM project_repositories
            WHERE project = ? AND enabled = ?
            ORDER BY name, id
            LIMIT 1
            """,
            (project, 1),
        )
        return self.from_row(row) if row is not None else None

    def register(
        self,
        name: str,
        path: str,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[Dict[str, Any]] = None,
        actor: str = "project-repo",
    ) -> ProjectRepository:
        name = name.strip()
        if not name:
            raise ValidationError("project repository name is required")
        repo_path_obj = Path(path).expanduser()
        repo_path = str(repo_path_obj)
        repo_source = (source or "repo-%s" % _safe_slug(name)).strip()
        if not repo_source:
            raise ValidationError("project repository source is required")
        repo_project = (project or repo_source).strip()
        contract = self._load_contract(repo_path_obj)
        if contract["project"] != repo_project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo_project)
            )
        codegraph_status = self._initialize_codegraph(repo_path_obj)
        self._validate_codegraph(codegraph_status)
        repo_metadata = ensure_json_object(metadata)
        repo_metadata["repository_contract"] = contract
        repo_metadata["codegraph"] = codegraph_status
        now = utcnow()
        row = self._store.query_one("SELECT id FROM project_repositories WHERE name = ?", (name,))
        repo_id = row["id"] if row is not None else new_id("projectrepo")
        self._store.execute(
            """
            INSERT INTO project_repositories (
                id, name, path, source, project, required_capabilities,
                enabled, poll_interval_seconds, metadata, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                path = excluded.path,
                source = excluded.source,
                project = excluded.project,
                required_capabilities = excluded.required_capabilities,
                enabled = excluded.enabled,
                poll_interval_seconds = excluded.poll_interval_seconds,
                metadata = excluded.metadata,
                updated_at = excluded.updated_at
            """,
            (
                repo_id,
                name,
                repo_path,
                repo_source,
                repo_project,
                json_dumps(coerce_list(required_capabilities)),
                1 if enabled else 0,
                max(1, int(poll_interval_seconds)),
                json_dumps(repo_metadata),
                now,
                now,
            ),
        )
        self._record_log(
            "bridge.project_repository.registered",
            layer="control_plane",
            source=actor,
            subject_type="environment",
            subject_id=repo_id,
            detail={
                "name": name,
                "path": repo_path,
                "source": repo_source,
                "project": repo_project,
                "enabled": enabled,
                "repository_contract_schema": contract["schema"],
                "repository_contract_path": contract["contract_path"],
                "codegraph": codegraph_status,
            },
        )
        return self.get(repo_id)

    def get(self, repo_id_or_name: str) -> ProjectRepository:
        row = self._store.query_one(
            "SELECT * FROM project_repositories WHERE id = ? OR name = ?",
            (repo_id_or_name, repo_id_or_name),
        )
        if row is None:
            raise NotFoundError("project repository not found: %s" % repo_id_or_name)
        return self.from_row(row)

    def list(self, enabled: Optional[bool] = None) -> List[ProjectRepository]:
        if enabled is None:
            rows = self._store.query_all("SELECT * FROM project_repositories ORDER BY name, id")
        else:
            rows = self._store.query_all(
                "SELECT * FROM project_repositories WHERE enabled = ? ORDER BY name, id",
                (1 if enabled else 0,),
            )
        return [self.from_row(row) for row in rows]

    def record_merge_capability(
        self, repo_id_or_name: str, capability: Dict[str, Any]
    ) -> ProjectRepository:
        """Store a resolved merge-serialization capability on the repo record.

        Written into ``metadata`` rather than a new column because
        ``schema.sql`` is ``CREATE TABLE IF NOT EXISTS`` with no migration
        framework -- a new column simply would not appear on the live hub.

        The capability's own ``error`` field carries a failed probe rather than
        ``last_error``: ``last_error`` belongs to issue ingest, and letting a
        capability probe overwrite it would make an ingest failure and a
        capability failure indistinguishable, which is the exact confusion this
        record exists to remove.
        """

        repo = self.get(repo_id_or_name)
        metadata = ensure_json_object(repo.metadata)
        metadata["merge_serialization_capability"] = ensure_json_object(capability)
        self._store.execute(
            "UPDATE project_repositories SET metadata = ?, updated_at = ? WHERE id = ?",
            (json_dumps(metadata), utcnow(), repo.id),
        )
        return self.get(repo.id)

    def contract_for(self, repo: ProjectRepository) -> JsonDict:
        contract = self._load_contract(Path(repo.path).expanduser())
        if contract["project"] != repo.project:
            raise ValidationError(
                "repository runtime contract project %s does not match registered project %s"
                % (contract["project"], repo.project)
            )
        return contract

    @staticmethod
    def from_row(row: Any) -> ProjectRepository:
        return ProjectRepository(
            row["id"],
            row["name"],
            row["path"],
            row["source"],
            row["project"],
            json_loads(row["required_capabilities"], []),
            bool(row["enabled"]),
            int(row["poll_interval_seconds"]),
            row["last_polled_at"],
            row["last_imported_at"],
            row["last_error"],
            json_loads(row["metadata"], {}),
            row["created_at"],
            row["updated_at"],
        )
