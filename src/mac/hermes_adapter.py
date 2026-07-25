from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from mac.api_client import MacApiClient, MacApiError


JsonDict = Dict[str, Any]
Transport = Callable[[str, str, Optional[JsonDict]], Any]
MemorySink = Callable[[JsonDict], None]


SECRET_FIELD_HINTS = ("secret", "token", "password", "private_key", "credential")
SECRET_ARGUMENT_FLAGS = {"--token", "--api-key", "--key", "--secret", "--password"}


def _path_part(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def _query(params: Iterable[tuple[str, Any]]) -> str:
    encoded = []
    for key, value in params:
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        encoded.append((key, value))
    return urllib.parse.urlencode(encoded)


@dataclass
class PlatformBindingSpec:
    platform: str
    external_id: str
    display_name: str = ""
    scopes: JsonDict = field(default_factory=dict)
    metadata: JsonDict = field(default_factory=dict)


@dataclass
class ConversationTaskInput:
    title: str
    summary: str
    user_id: Optional[str] = None
    platform_binding_id: Optional[str] = None
    conversation_ref: Optional[str] = None
    project: Optional[str] = None
    priority: int = 0
    required_capabilities: Sequence[str] = field(default_factory=tuple)
    dependencies: Sequence[str] = field(default_factory=tuple)
    snippets: Sequence[str] = field(default_factory=tuple)
    links: Sequence[str] = field(default_factory=tuple)
    metadata: JsonDict = field(default_factory=dict)
    max_attempts: int = 3

    def description(self) -> str:
        sections = ["Summary:\n%s" % self.summary.strip()]
        snippets = [snippet.strip() for snippet in self.snippets if snippet.strip()]
        if snippets:
            sections.append("Relevant excerpts:\n" + "\n".join("- %s" % item for item in snippets))
        links = [link.strip() for link in self.links if link.strip()]
        if links:
            sections.append("References:\n" + "\n".join("- %s" % item for item in links))
        return "\n\n".join(sections)

    def sanitized_metadata(self) -> JsonDict:
        metadata = _sanitize_json_object(self.metadata)
        metadata.setdefault(
            "sanitized_conversation",
            {
                "summary": self.summary.strip(),
                "snippets": [snippet.strip() for snippet in self.snippets if snippet.strip()],
                "links": [link.strip() for link in self.links if link.strip()],
            },
        )
        return metadata


def _default_web_url() -> str:
    return (
        os.environ.get("FIRECRAWL_API_URL")
        or os.environ.get("FIRECRAWL_GATEWAY_URL")
        or os.environ.get("MAC_WEB_SEARCH_URL")
        or ""
    ).rstrip("/")


class HermesMacAdapter:
    """High-level helper intended to be called from Hermes skills/gateways."""

    def __init__(self, client: MacApiClient, web_client: Optional[MacApiClient] = None) -> None:
        self.client = client
        self.web_client = web_client

    def _web_client(self) -> MacApiClient:
        if self.web_client is not None:
            return self.web_client
        web_url = _default_web_url()
        if not web_url:
            raise MacApiError(
                "web search requires FIRECRAWL_API_URL, FIRECRAWL_GATEWAY_URL, or MAC_WEB_SEARCH_URL"
            )
        self.web_client = MacApiClient(web_url, token=os.environ.get("FIRECRAWL_API_KEY"))
        return self.web_client

    def register_identity(
        self,
        tenant_name: str,
        persona_name: str,
        instance_name: str,
        soul_ref: str,
        memory_scope: str,
        home_ref: str = "",
        platform_bindings: Iterable[PlatformBindingSpec] = (),
    ) -> JsonDict:
        tenant = self.client.post("/tenants", {"name": tenant_name})
        persona = self.client.post(
            "/personas",
            {
                "tenant_id": tenant["id"],
                "name": persona_name,
                "soul_ref": soul_ref,
                "memory_scope": memory_scope,
            },
        )
        instance = self.client.post(
            "/persona-instances",
            {
                "tenant_id": tenant["id"],
                "name": instance_name,
                "persona_id": persona["id"],
                "home_ref": home_ref,
            },
        )
        bindings = []
        for binding in platform_bindings:
            bindings.append(
                self.client.post(
                    "/platform-bindings",
                    {
                        "tenant_id": tenant["id"],
                        "hermes_instance_id": instance["id"],
                        "platform": binding.platform,
                        "external_id": binding.external_id,
                        "display_name": binding.display_name,
                        "scopes": binding.scopes,
                        "metadata": binding.metadata,
                    },
                )
            )
        return {
            "tenant": tenant,
            "persona": persona,
            "hermes_instance": instance,
            "platform_bindings": bindings,
        }

    def create_task_from_conversation(
        self,
        hermes_instance_id: str,
        task_input: ConversationTaskInput,
    ) -> JsonDict:
        if not task_input.title.strip():
            raise MacApiError("conversation task title is required")
        if not task_input.summary.strip():
            raise MacApiError("conversation task summary is required")
        return self.client.post(
            "/persona-instances/%s/tasks" % hermes_instance_id,
            {
                "title": task_input.title,
                "description": task_input.description(),
                "project": task_input.project,
                "priority": task_input.priority,
                "required_capabilities": list(task_input.required_capabilities),
                "dependencies": list(task_input.dependencies),
                "metadata": task_input.sanitized_metadata(),
                "max_attempts": task_input.max_attempts,
                "user_id": task_input.user_id,
                "platform_binding_id": task_input.platform_binding_id,
                "conversation_ref": task_input.conversation_ref,
                "actor": "hermes",
            },
        )

    def create_conversation_task(
        self,
        hermes_instance_id: str,
        task_input: ConversationTaskInput,
    ) -> JsonDict:
        return self.create_task_from_conversation(hermes_instance_id, task_input)

    def task_summary(self, task_id: str) -> JsonDict:
        return self.client.get("/tasks/%s/summary" % _path_part(task_id))

    def task_show(self, task_id: str) -> JsonDict:
        return self.task_summary(task_id)

    def list_tasks(
        self,
        *,
        state: Optional[str] = None,
        tenant_id: Optional[str] = None,
        project: Optional[str] = None,
        limit: Optional[int] = None,
        view: Optional[str] = None,
    ) -> Any:
        query = _query(
            (
                ("state", state),
                ("tenant_id", tenant_id),
                ("view", view),
                ("project", project),
                ("limit", int(limit) if limit is not None else None),
            )
        )
        return self.client.get("/tasks%s" % (("?%s" % query) if query else ""))

    def list_task_summaries(
        self,
        *,
        state: Optional[str] = None,
        tenant_id: Optional[str] = None,
        project: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Any:
        return self.list_tasks(
            state=state,
            tenant_id=tenant_id,
            view="summary",
            project=project,
            limit=limit,
        )

    def task_detail(self, task_id: str) -> JsonDict:
        return self.client.get("/tasks/%s" % _path_part(task_id))

    def add_child_task(
        self,
        task_id: str,
        *,
        title: str,
        description: str = "",
        project: Optional[str] = None,
        priority: Optional[int] = None,
        required_capabilities: Optional[Iterable[str]] = None,
        dependencies: Optional[Iterable[str]] = None,
        metadata: Optional[JsonDict] = None,
        max_attempts: Optional[int] = None,
        actor: str = "hermes",
    ) -> JsonDict:
        child: JsonDict = {
            "title": title,
            "description": description,
            "project": project,
            "priority": priority,
            "metadata": _sanitize_json_object(metadata or {}),
            "max_attempts": max_attempts,
        }
        if required_capabilities is not None:
            child["required_capabilities"] = list(required_capabilities)
        if dependencies is not None:
            child["dependencies"] = list(dependencies)
        return self.client.post(
            "/tasks/%s/children" % _path_part(task_id),
            {
                "actor": actor,
                "children": [{key: value for key, value in child.items() if value is not None}],
            },
        )

    def work_context(
        self,
        hermes_instance_id: str,
        *,
        include_completed: bool = True,
        task_limit: int = 100,
    ) -> JsonDict:
        query = _query(
            (
                ("include_completed", include_completed),
                ("task_limit", int(task_limit)),
            )
        )
        return self.client.get(
            "/persona-instances/%s/work-context?%s" % (_path_part(hermes_instance_id), query)
        )

    def work_context_brief(self, hermes_instance_id: str) -> str:
        context = self.work_context(hermes_instance_id, include_completed=False, task_limit=20)
        projects = context.get("projects") or []
        tasks = context.get("tasks") or []
        agents = context.get("agents") or []
        project_names = ", ".join(str(project.get("project")) for project in projects[:5])
        return (
            "MAC work context: %d active task(s), %d project(s), %d agent(s). "
            "Projects: %s."
            % (
                len(tasks),
                len(projects),
                len(agents),
                project_names or "none",
            )
        )

    def runtime_proof(
        self,
        hermes_instance_id: str,
        *,
        hermes_startup: Optional[JsonDict] = None,
    ) -> JsonDict:
        path = "/persona-instances/%s/runtime-proof" % _path_part(hermes_instance_id)
        if hermes_startup is None:
            return self.client.get(path)
        return self.client.post(path, {"hermes_startup": _sanitize_json_object(hermes_startup)})

    def create_project(
        self,
        name: str,
        description: str = "",
        *,
        metadata: Optional[JsonDict] = None,
        status: str = "active",
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/projects",
            {
                "name": name,
                "description": description,
                "metadata": _sanitize_json_object(metadata or {}),
                "status": status,
                "actor": actor,
            },
        )

    def import_project_item(
        self,
        source: str,
        external_id: str,
        title: str,
        *,
        payload: Optional[JsonDict] = None,
        description: Optional[str] = None,
        project: Optional[str] = None,
        priority: int = 0,
        required_capabilities: Sequence[str] = (),
        dependencies: Sequence[str] = (),
        metadata: Optional[JsonDict] = None,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/bridge/items",
            {
                "source": source,
                "external_id": external_id,
                "title": title,
                "description": description,
                "project": project,
                "priority": int(priority),
                "payload": _sanitize_json_object(payload or {}),
                "required_capabilities": list(required_capabilities),
                "dependencies": list(dependencies),
                "metadata": _sanitize_json_object(metadata or {}),
                "actor": actor,
            },
        )

    def list_project_items(self) -> Any:
        return self.client.get("/bridge/items")

    def list_projects(self) -> Any:
        return self.client.get("/projects")

    def project_detail(self, project: str) -> JsonDict:
        return self.client.get("/projects/%s" % _path_part(project))

    def register_project_repository(
        self,
        name: str,
        path: str,
        *,
        source: Optional[str] = None,
        project: Optional[str] = None,
        required_capabilities: Sequence[str] = (),
        enabled: bool = True,
        poll_interval_seconds: int = 60,
        metadata: Optional[JsonDict] = None,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/bridge/repositories",
            {
                "name": name,
                "path": path,
                "source": source,
                "project": project,
                "required_capabilities": list(required_capabilities),
                "enabled": enabled,
                "poll_interval_seconds": int(poll_interval_seconds),
                "metadata": _sanitize_json_object(metadata or {}),
                "actor": actor,
            },
        )

    def list_project_repositories(self, *, enabled: Optional[bool] = None) -> Any:
        query = _query((("enabled", enabled),))
        path = "/bridge/repositories"
        if query:
            path = "%s?%s" % (path, query)
        return self.client.get(path)

    def list_agents(self) -> Any:
        return self.client.get("/agents")

    def agent_detail(self, agent_id: str) -> JsonDict:
        return self.client.get("/agents/%s" % _path_part(agent_id))

    def agent_identity(self, agent_id: str) -> JsonDict:
        return self.client.get("/agents/%s/identity" % _path_part(agent_id))

    def claim_next_task(
        self,
        agent_id: str,
        *,
        lease_seconds: int = 900,
        allowed_projects: Sequence[str] = (),
        required_metadata: Optional[JsonDict] = None,
        claim_only_canary_tasks: bool = False,
        dry_run: bool = False,
    ) -> Optional[JsonDict]:
        return self.client.post(
            "/agents/%s/claim-next" % _path_part(agent_id),
            {
                "lease_seconds": int(lease_seconds),
                "allowed_projects": list(allowed_projects),
                "required_metadata": _sanitize_json_object(required_metadata or {}),
                "claim_only_canary_tasks": claim_only_canary_tasks,
                "dry_run": dry_run,
            },
        )

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        lease_seconds: int = 900,
    ) -> JsonDict:
        query = _query((("agent_id", agent_id), ("lease_seconds", int(lease_seconds))))
        return self.client.post("/tasks/%s/claim?%s" % (_path_part(task_id), query), {})

    def start_task(
        self,
        task_id: str,
        agent_id: str,
        *,
        lease_id: Optional[str] = None,
    ) -> JsonDict:
        query = _query((("agent_id", agent_id), ("lease_id", lease_id)))
        return self.client.post("/tasks/%s/start?%s" % (_path_part(task_id), query), {})

    def transition_task(
        self,
        task_id: str,
        target_state: str,
        actor: str,
        detail: Optional[JsonDict] = None,
        *,
        lease_id: Optional[str] = None,
    ) -> JsonDict:
        body: JsonDict = {
            "target_state": target_state,
            "actor": actor,
            "detail": _sanitize_json_object(detail or {}),
        }
        if lease_id:
            body["lease_id"] = lease_id
        return self.client.post(
            "/tasks/%s/transition" % _path_part(task_id),
            body,
        )

    def add_evidence(
        self,
        task_id: str,
        kind: str,
        uri: str,
        summary: str,
        created_by: str,
        *,
        checksum: Optional[str] = None,
        lease_id: Optional[str] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        body: JsonDict = {
            "kind": kind,
            "uri": uri,
            "summary": summary,
            "created_by": created_by,
            "checksum": checksum,
            "metadata": _sanitize_json_object(metadata or {}),
        }
        if lease_id:
            body["lease_id"] = lease_id
        return self.client.post(
            "/tasks/%s/evidence" % _path_part(task_id),
            body,
        )

    def submit_for_review(
        self,
        task_id: str,
        agent_id: str,
        *,
        lease_id: Optional[str] = None,
        advance_default_workflow: bool = False,
    ) -> JsonDict:
        query = _query(
            (
                ("agent_id", agent_id),
                ("lease_id", lease_id),
                ("advance_default_workflow", advance_default_workflow),
            )
        )
        return self.client.post(
            "/tasks/%s/submit-for-review?%s" % (_path_part(task_id), query),
            {},
        )

    def request_review(
        self,
        task_id: str,
        reviewer_agent_id: str,
        *,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/tasks/%s/reviews" % _path_part(task_id),
            {"reviewer_agent_id": reviewer_agent_id, "actor": actor},
        )

    def claim_review(
        self,
        review_id: str,
        reviewer_agent_id: str,
        *,
        executor_evidence_id: Optional[str] = None,
        actor: str = "hermes",
    ) -> JsonDict:
        return self.client.post(
            "/reviews/%s/claim" % _path_part(review_id),
            {
                "reviewer_agent_id": reviewer_agent_id,
                "executor_evidence_id": executor_evidence_id,
                "actor": actor,
            },
        )

    def submit_review(
        self,
        review_id: str,
        status: str,
        reviewer_agent_id: str,
        *,
        reason: Optional[str] = None,
        evidence_id: Optional[str] = None,
    ) -> JsonDict:
        return self.client.post(
            "/reviews/%s/decision" % _path_part(review_id),
            {
                "status": status,
                "reviewer_agent_id": reviewer_agent_id,
                "reason": reason,
                "evidence_id": evidence_id,
            },
        )

    def publish_task(
        self,
        task_id: str,
        target: str,
        created_by: str,
        *,
        evidence_id: Optional[str] = None,
    ) -> JsonDict:
        return self.client.post(
            "/publications",
            {
                "task_id": task_id,
                "target": target,
                "created_by": created_by,
                "evidence_id": evidence_id,
            },
        )

    def record_command_audit(
        self,
        agent_id: str,
        *,
        phase: str,
        argv: Sequence[str],
        cwd: str,
        command_id: Optional[str] = None,
        task_id: Optional[str] = None,
        lease_id: Optional[str] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        duration_ms: Optional[float] = None,
        returncode: Optional[int] = None,
        stdout_sha256: Optional[str] = None,
        stderr_sha256: Optional[str] = None,
        stdout_bytes: Optional[int] = None,
        stderr_bytes: Optional[int] = None,
        metadata: Optional[JsonDict] = None,
    ) -> JsonDict:
        return self.client.post(
            "/agents/%s/command-audit" % _path_part(agent_id),
            {
                "command_id": command_id,
                "phase": phase,
                "argv": _sanitize_command_argv(argv),
                "cwd": cwd,
                "task_id": task_id,
                "lease_id": lease_id,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_ms": duration_ms,
                "returncode": returncode,
                "stdout_sha256": stdout_sha256,
                "stderr_sha256": stderr_sha256,
                "stdout_bytes": stdout_bytes,
                "stderr_bytes": stderr_bytes,
                "metadata": _sanitize_json_object(metadata or {}),
            },
        )

    def list_command_audit(
        self,
        *,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        command_id: Optional[str] = None,
        phase: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 200,
    ) -> Any:
        query = _query(
            (
                ("agent_id", agent_id),
                ("task_id", task_id),
                ("command_id", command_id),
                ("phase", phase),
                ("since", since),
                ("until", until),
                ("limit", int(limit)),
            )
        )
        return self.client.get("/command-audit?%s" % query)

    def web_search(self, query: str, *, limit: int = 5) -> JsonDict:
        if not str(query).strip():
            raise MacApiError("web search query is required")
        return self._web_client().post(
            "/v2/search",
            {"query": str(query).strip(), "limit": int(limit)},
        )

    def web_scrape(self, url: str, *, formats: Sequence[str] = ("markdown",)) -> JsonDict:
        if not str(url).strip():
            raise MacApiError("web scrape url is required")
        selected_formats = [str(item).strip() for item in formats if str(item).strip()]
        return self._web_client().post(
            "/v2/scrape",
            {"url": str(url).strip(), "formats": selected_formats or ["markdown"]},
        )

    def web_crawl(
        self,
        url: str,
        *,
        limit: int = 1,
        formats: Sequence[str] = ("markdown",),
    ) -> JsonDict:
        if not str(url).strip():
            raise MacApiError("web crawl url is required")
        selected_formats = [str(item).strip() for item in formats if str(item).strip()]
        return self._web_client().post(
            "/v2/crawl",
            {
                "url": str(url).strip(),
                "limit": int(limit),
                "scrapeOptions": {"formats": selected_formats or ["markdown"]},
            },
        )

    def web_crawl_status(self, job_id: str) -> JsonDict:
        if not str(job_id).strip():
            raise MacApiError("web crawl job id is required")
        return self._web_client().get("/v2/crawl/%s" % _path_part(str(job_id).strip()))

    def user_reply_for_task(self, task_id: str) -> str:
        summary = self.task_summary(task_id)
        if summary["state"] == "completed":
            publications = summary.get("publications") or []
            if publications:
                return "%s is complete and published to %s." % (
                    summary["title"],
                    publications[-1]["target"],
                )
            return "%s is complete." % summary["title"]
        if summary["state"] in {"failed", "cancelled"}:
            return "%s is %s." % (summary["title"], summary["state"])
        return "%s is currently %s." % (summary["title"], summary["state"])

    def memory_writeback_payload(self, hermes_instance_id: str, task_id: str) -> JsonDict:
        context = self.client.get("/persona-instances/%s/context" % hermes_instance_id)
        summary = self.task_summary(task_id)
        if summary["state"] != "completed":
            raise MacApiError("only completed tasks should be written back to Hermes memory")
        memory_contract = context["memory_contract"]
        memory_scope = memory_contract.get("memory_scope")
        if not memory_scope:
            raise MacApiError("Hermes instance has no memory_scope")
        return {
            "memory_scope": memory_scope,
            "content": self.user_reply_for_task(task_id),
            "metadata": {
                "source": "mac",
                "task_id": task_id,
                "hermes_instance_id": hermes_instance_id,
                "persona_id": context["hermes_instance"].get("persona_id"),
                "origin": summary.get("origin"),
            },
        }

    def record_memory_writeback(
        self,
        hermes_instance_id: str,
        task_id: str,
        payload: Optional[JsonDict] = None,
        created_by: str = "hermes",
    ) -> JsonDict:
        payload = payload or self.memory_writeback_payload(hermes_instance_id, task_id)
        return self.client.post(
            "/memory",
            {
                "task_id": task_id,
                "subject_type": "hermes_memory",
                "subject_id": hermes_instance_id,
                "record_type": "task_result_writeback",
                "content": payload["content"],
                "created_by": created_by,
            },
        )

    def search_memory(
        self,
        *,
        task_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[str] = None,
        record_type_prefix: Optional[str] = None,
        created_by: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
        order: str = "asc",
        content_contains: Optional[str] = None,
    ) -> Any:
        query = _query(
            (
                ("task_id", task_id),
                ("subject_type", subject_type),
                ("subject_id", subject_id),
                ("record_type", record_type),
                ("record_type_prefix", record_type_prefix),
                ("created_by", created_by),
                ("since", since),
                ("until", until),
                ("limit", int(limit) if limit is not None else None),
                ("order", order),
                ("content_contains", content_contains),
            )
        )
        return self.client.get("/memory%s" % (("?%s" % query) if query else ""))

    def write_completed_task_to_memory(
        self,
        hermes_instance_id: str,
        task_id: str,
        sink: Optional[MemorySink] = None,
        created_by: str = "hermes",
    ) -> JsonDict:
        payload = self.memory_writeback_payload(hermes_instance_id, task_id)
        if sink is not None:
            sink(payload)
        record = self.record_memory_writeback(
            hermes_instance_id,
            task_id,
            payload=payload,
            created_by=created_by,
        )
        return {"payload": payload, "record": record}


def _sanitize_json_object(value: JsonDict) -> JsonDict:
    return _sanitize_json(value, ())


def _sanitize_json(value: Any, path: Sequence[str]) -> Any:
    if isinstance(value, dict):
        sanitized: JsonDict = {}
        for key, nested in value.items():
            key_lower = str(key).lower()
            if any(hint in key_lower for hint in SECRET_FIELD_HINTS):
                continue
            if key_lower in {"raw_messages", "memory", "private_memory", "user_memory"}:
                continue
            sanitized[str(key)] = _sanitize_json(nested, path + (str(key),))
        return sanitized
    if isinstance(value, list):
        return [_sanitize_json(item, path + (str(index),)) for index, item in enumerate(value)]
    return value


def _sanitize_command_argv(argv: Sequence[str]) -> List[str]:
    sanitized: List[str] = []
    redact_next = False
    for raw in argv:
        arg = str(raw)
        lowered = arg.lower()
        if redact_next:
            sanitized.append("<redacted>")
            redact_next = False
            continue
        if lowered in SECRET_ARGUMENT_FLAGS:
            sanitized.append(arg)
            redact_next = True
            continue
        if any(
            marker in lowered
            for marker in ("bearer ", "token=", "api_key=", "apikey=", "password=", "secret=")
        ):
            sanitized.append("<redacted>")
            continue
        if len(arg) > 512:
            sanitized.append("<truncated:chars=%d>" % len(arg))
            continue
        sanitized.append(arg)
    return sanitized


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_arg(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _json_list_arg(value: Optional[str]) -> List[Any]:
    parsed = _json_arg(value, [])
    if not isinstance(parsed, list):
        raise MacApiError("expected JSON array")
    return parsed


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _adapter(args: argparse.Namespace) -> HermesMacAdapter:
    client = MacApiClient(args.url, token=args.token)
    web_url = (args.web_url or "").rstrip()
    web_client = MacApiClient(web_url, token=args.web_token) if web_url else None
    return HermesMacAdapter(client, web_client=web_client)


def _cmd_register(args: argparse.Namespace) -> None:
    bindings = []
    for raw in args.binding or []:
        parts = raw.split(":", 2)
        if len(parts) < 2:
            raise MacApiError("--binding must be platform:external_id[:display_name]")
        bindings.append(
            PlatformBindingSpec(
                parts[0],
                parts[1],
                parts[2] if len(parts) == 3 else "",
            )
        )
    _print(
        _adapter(args).register_identity(
            args.tenant,
            args.persona,
            args.instance,
            args.soul_ref,
            args.memory_scope,
            home_ref=args.home_ref or "",
            platform_bindings=bindings,
        )
    )


def _cmd_task(args: argparse.Namespace) -> None:
    task_input = ConversationTaskInput(
        title=args.title,
        summary=args.summary,
        platform_binding_id=args.platform_binding_id,
        conversation_ref=args.conversation_ref,
        project=args.project,
        priority=args.priority,
        required_capabilities=_csv(args.required_capabilities),
        snippets=args.snippet or (),
        links=args.link or (),
    )
    _print(_adapter(args).create_task_from_conversation(args.hermes_instance_id, task_input))


def _cmd_summary(args: argparse.Namespace) -> None:
    _print(_adapter(args).task_summary(args.task_id))


def _cmd_tasks(args: argparse.Namespace) -> None:
    view = "summary" if args.summary else args.view
    _print(
        _adapter(args).list_tasks(
            state=args.state,
            tenant_id=args.tenant_id,
            view=view,
            project=args.project,
            limit=args.limit,
        )
    )


def _cmd_task_detail(args: argparse.Namespace) -> None:
    _print(_adapter(args).task_detail(args.task_id))


def _cmd_work_context(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).work_context(
            args.hermes_instance_id,
            include_completed=not args.active_only,
            task_limit=args.task_limit,
        )
    )


def _cmd_work_brief(args: argparse.Namespace) -> None:
    print(_adapter(args).work_context_brief(args.hermes_instance_id))


def _cmd_runtime_proof(args: argparse.Namespace) -> None:
    startup = None
    if not args.skip_local_startup:
        from mac.hermes_startup import build_hermes_startup_report

        startup = build_hermes_startup_report()
    _print(_adapter(args).runtime_proof(args.hermes_instance_id, hermes_startup=startup))


def _cmd_import_project_item(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).import_project_item(
            args.source,
            args.external_id,
            args.title,
            payload=_json_arg(args.payload, {}),
            description=args.description,
            project=args.project,
            priority=args.priority,
            required_capabilities=_csv(args.required_capabilities),
            dependencies=_csv(args.dependencies),
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def _cmd_project_items(args: argparse.Namespace) -> None:
    _print(_adapter(args).list_project_items())


def _cmd_create_project(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).create_project(
            args.name,
            args.description or "",
            metadata=_json_arg(args.metadata, {}),
            status=args.status,
            actor=args.actor,
        )
    )


def _cmd_projects(args: argparse.Namespace) -> None:
    _print(_adapter(args).list_projects())


def _cmd_project_detail(args: argparse.Namespace) -> None:
    _print(_adapter(args).project_detail(args.project))


def _cmd_register_project_repository(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).register_project_repository(
            args.name,
            args.path,
            source=args.source,
            project=args.project,
            required_capabilities=_csv(args.required_capabilities),
            enabled=not args.disabled,
            poll_interval_seconds=args.poll_interval_seconds,
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def _cmd_project_repositories(args: argparse.Namespace) -> None:
    _print(_adapter(args).list_project_repositories(enabled=args.enabled))


def _cmd_agents(args: argparse.Namespace) -> None:
    _print(_adapter(args).list_agents())


def _cmd_agent_detail(args: argparse.Namespace) -> None:
    _print(_adapter(args).agent_detail(args.agent_id))


def _cmd_agent_identity(args: argparse.Namespace) -> None:
    _print(_adapter(args).agent_identity(args.agent_id))


def _cmd_claim_next(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).claim_next_task(
            args.agent_id,
            lease_seconds=args.lease_seconds,
            allowed_projects=args.allowed_project,
            required_metadata=_json_arg(args.required_metadata, {}),
            claim_only_canary_tasks=args.claim_only_canary_tasks,
            dry_run=args.dry_run,
        )
    )


def _cmd_claim(args: argparse.Namespace) -> None:
    _print(_adapter(args).claim_task(args.task_id, args.agent_id, lease_seconds=args.lease_seconds))


def _cmd_start(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).start_task(
            args.task_id,
            args.agent_id,
            lease_id=args.lease_id,
        )
    )


def _cmd_transition(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).transition_task(
            args.task_id,
            args.target_state,
            args.actor,
            _json_arg(args.detail, {}),
            lease_id=args.lease_id,
        )
    )


def _cmd_add_child_task(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).add_child_task(
            args.task_id,
            title=args.title,
            description=args.description or "",
            project=args.project,
            priority=args.priority,
            required_capabilities=(
                _csv(args.required_capabilities)
                if args.required_capabilities is not None
                else None
            ),
            dependencies=_csv(args.dependencies) if args.dependencies is not None else None,
            metadata=_json_arg(args.metadata, {}),
            max_attempts=args.max_attempts,
            actor=args.actor,
        )
    )


def _cmd_evidence(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).add_evidence(
            args.task_id,
            args.kind,
            args.uri,
            args.summary,
            args.created_by,
            checksum=args.checksum,
            lease_id=args.lease_id,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def _cmd_submit_review(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).submit_for_review(
            args.task_id,
            args.agent_id,
            lease_id=args.lease_id,
            advance_default_workflow=args.advance_default_workflow,
        )
    )


def _cmd_request_review(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).request_review(
            args.task_id,
            args.reviewer_agent_id,
            actor=args.actor,
        )
    )


def _cmd_claim_review(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).claim_review(
            args.review_id,
            args.reviewer_agent_id,
            executor_evidence_id=args.executor_evidence_id,
            actor=args.actor,
        )
    )


def _cmd_review_decision(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).submit_review(
            args.review_id,
            args.status,
            args.reviewer_agent_id,
            reason=args.reason,
            evidence_id=args.evidence_id,
        )
    )


def _cmd_publish(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).publish_task(
            args.task_id,
            args.target,
            args.created_by,
            evidence_id=args.evidence_id,
        )
    )


def _cmd_command_audit_record(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).record_command_audit(
            args.agent_id,
            phase=args.phase,
            argv=_json_list_arg(args.argv_json),
            cwd=args.cwd,
            command_id=args.command_id,
            task_id=args.task_id,
            lease_id=args.lease_id,
            started_at=args.started_at,
            completed_at=args.completed_at,
            duration_ms=args.duration_ms,
            returncode=args.returncode,
            stdout_sha256=args.stdout_sha256,
            stderr_sha256=args.stderr_sha256,
            stdout_bytes=args.stdout_bytes,
            stderr_bytes=args.stderr_bytes,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def _cmd_command_audit_list(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).list_command_audit(
            agent_id=args.agent_id,
            task_id=args.task_id,
            command_id=args.command_id,
            phase=args.phase,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    )


def _cmd_web_search(args: argparse.Namespace) -> None:
    _print(_adapter(args).web_search(args.query, limit=args.limit))


def _cmd_web_scrape(args: argparse.Namespace) -> None:
    _print(_adapter(args).web_scrape(args.url, formats=args.format or ("markdown",)))


def _cmd_web_crawl(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).web_crawl(
            args.url,
            limit=args.limit,
            formats=args.format or ("markdown",),
        )
    )


def _cmd_web_crawl_status(args: argparse.Namespace) -> None:
    _print(_adapter(args).web_crawl_status(args.job_id))


def _cmd_reply(args: argparse.Namespace) -> None:
    print(_adapter(args).user_reply_for_task(args.task_id))


def _cmd_writeback(args: argparse.Namespace) -> None:
    _print(_adapter(args).write_completed_task_to_memory(args.hermes_instance_id, args.task_id))


def _cmd_memory_search(args: argparse.Namespace) -> None:
    _print(
        _adapter(args).search_memory(
            task_id=args.task_id,
            subject_type=args.subject_type,
            subject_id=args.subject_id,
            record_type=args.record_type,
            record_type_prefix=args.record_type_prefix,
            created_by=args.created_by,
            since=args.since,
            until=args.until,
            limit=args.limit,
            order=args.order,
            content_contains=args.content_contains,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mac-hermes",
        description="Hermes-side adapter for the mac control plane",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("MAC_URL") or os.environ.get("MAC_HUB_URL") or "http://127.0.0.1:8000",
    )
    from mac.fleet_env import resolve_first as _resolve_token

    parser.add_argument(
        "--fleet",
        default=os.environ.get("MAC_FLEET"),
        help="fleet name used to scope env var lookup (MAC_API_TOKEN__<FLEET>)",
    )
    parser.add_argument(
        "--token",
        default=_resolve_token(["MAC_TOKEN", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"]),
    )
    parser.add_argument("--web-url", default=_default_web_url())
    parser.add_argument("--web-token", default=os.environ.get("FIRECRAWL_API_KEY"))
    sub = parser.add_subparsers(dest="command", required=True)

    register = sub.add_parser("register", help="register tenant, persona, Hermes instance, and optional platform bindings")
    register.add_argument("--tenant", required=True)
    register.add_argument("--persona", required=True)
    register.add_argument("--instance", required=True)
    register.add_argument("--soul-ref", required=True)
    register.add_argument("--memory-scope", required=True)
    register.add_argument("--home-ref")
    register.add_argument("--binding", action="append", help="platform:external_id[:display_name]")
    register.set_defaults(func=_cmd_register)

    task = sub.add_parser("task", help="create a durable task from sanitized conversation context")
    task.add_argument("hermes_instance_id")
    task.add_argument("title")
    task.add_argument("--summary", required=True)
    task.add_argument("--platform-binding-id")
    task.add_argument("--conversation-ref")
    task.add_argument("--project")
    task.add_argument("--priority", type=int, default=0)
    task.add_argument("--required-capabilities")
    task.add_argument("--snippet", action="append")
    task.add_argument("--link", action="append")
    task.set_defaults(func=_cmd_task)

    summary = sub.add_parser("summary", help="fetch a task summary for a user-facing response")
    summary.add_argument("task_id")
    summary.set_defaults(func=_cmd_summary)

    task_show = sub.add_parser("task-show", help="fetch a task using MAC's task summary projection")
    task_show.add_argument("task_id")
    task_show.set_defaults(func=_cmd_summary)

    task_detail = sub.add_parser("task-detail", help="fetch MAC's full task detail")
    task_detail.add_argument("task_id")
    task_detail.set_defaults(func=_cmd_task_detail)

    tasks = sub.add_parser("tasks", help="list MAC task records")
    tasks.add_argument("--state")
    tasks.add_argument("--tenant-id")
    tasks.add_argument("--project")
    tasks.add_argument("--limit", type=int)
    tasks.add_argument("--view", choices=("summary",))
    tasks.add_argument("--summary", action="store_true", help="request the lightweight task summary projection")
    tasks.set_defaults(func=_cmd_tasks)

    work_context = sub.add_parser("work-context", help="fetch MAC's task/project/agent context for this Hermes instance")
    work_context.add_argument("hermes_instance_id")
    work_context.add_argument("--active-only", action="store_true")
    work_context.add_argument("--task-limit", type=int, default=100)
    work_context.set_defaults(func=_cmd_work_context)

    work_brief = sub.add_parser("work-brief", help="render a concise MAC work-context status line")
    work_brief.add_argument("hermes_instance_id")
    work_brief.set_defaults(func=_cmd_work_brief)

    runtime_proof = sub.add_parser("runtime-proof", help="prove MAC/Hermes task-project bridge readiness")
    runtime_proof.add_argument("hermes_instance_id")
    runtime_proof.add_argument(
        "--skip-local-startup",
        action="store_true",
        help="fetch hub-only proof without sending this Hermes runtime's local startup report",
    )
    runtime_proof.set_defaults(func=_cmd_runtime_proof)

    import_project_item = sub.add_parser("import-project-item", help="import an external project item into MAC")
    import_project_item.add_argument("source")
    import_project_item.add_argument("external_id")
    import_project_item.add_argument("title")
    import_project_item.add_argument("--payload", default="{}")
    import_project_item.add_argument("--description")
    import_project_item.add_argument("--project")
    import_project_item.add_argument("--priority", type=int, default=0)
    import_project_item.add_argument("--required-capabilities")
    import_project_item.add_argument("--dependencies")
    import_project_item.add_argument("--metadata", default="{}")
    import_project_item.add_argument("--actor", default="hermes")
    import_project_item.set_defaults(func=_cmd_import_project_item)

    project_items = sub.add_parser("project-items", help="list MAC bridge project items")
    project_items.set_defaults(func=_cmd_project_items)

    create_project = sub.add_parser("create-project", help="create a first-class MAC project")
    create_project.add_argument("name")
    create_project.add_argument("--description", default="")
    create_project.add_argument("--metadata", default="{}")
    create_project.add_argument("--status", default="active")
    create_project.add_argument("--actor", default="hermes")
    create_project.set_defaults(func=_cmd_create_project)

    projects = sub.add_parser("projects", help="list MAC project summaries")
    projects.set_defaults(func=_cmd_projects)

    project_detail = sub.add_parser("project-detail", help="fetch one MAC project summary and records")
    project_detail.add_argument("project")
    project_detail.set_defaults(func=_cmd_project_detail)

    project_repositories = sub.add_parser("project-repositories", help="list registered project repositories")
    project_repositories.add_argument("--enabled", action="store_true", default=None)
    project_repositories.set_defaults(func=_cmd_project_repositories)

    register_project_repository = sub.add_parser("register-project-repository", help="register a mac-task-backed project repository")
    register_project_repository.add_argument("name")
    register_project_repository.add_argument("path")
    register_project_repository.add_argument("--source")
    register_project_repository.add_argument("--project")
    register_project_repository.add_argument("--required-capabilities")
    register_project_repository.add_argument("--poll-interval-seconds", type=int, default=60)
    register_project_repository.add_argument("--metadata", default="{}")
    register_project_repository.add_argument("--disabled", action="store_true")
    register_project_repository.add_argument("--actor", default="hermes")
    register_project_repository.set_defaults(func=_cmd_register_project_repository)

    agents = sub.add_parser("agents", help="list MAC agents visible to Hermes")
    agents.set_defaults(func=_cmd_agents)

    agent_detail = sub.add_parser("agent-detail", help="fetch one MAC agent record")
    agent_detail.add_argument("agent_id")
    agent_detail.set_defaults(func=_cmd_agent_detail)

    agent_identity = sub.add_parser("agent-identity", help="fetch composed MAC/Hermes agent identity")
    agent_identity.add_argument("agent_id")
    agent_identity.set_defaults(func=_cmd_agent_identity)

    claim_next = sub.add_parser("claim-next", help="claim or dry-run the next eligible MAC task for an agent")
    claim_next.add_argument("agent_id")
    claim_next.add_argument("--lease-seconds", type=int, default=900)
    claim_next.add_argument("--allowed-project", action="append", default=[])
    claim_next.add_argument("--required-metadata", default="{}")
    claim_next.add_argument("--claim-only-canary-tasks", action="store_true")
    claim_next.add_argument("--dry-run", action="store_true")
    claim_next.set_defaults(func=_cmd_claim_next)

    claim = sub.add_parser("claim", help="claim a MAC task for an agent")
    claim.add_argument("task_id")
    claim.add_argument("agent_id")
    claim.add_argument("--lease-seconds", type=int, default=900)
    claim.set_defaults(func=_cmd_claim)

    start = sub.add_parser("start", help="start a claimed MAC task")
    start.add_argument("task_id")
    start.add_argument("agent_id")
    start.add_argument("--lease-id")
    start.set_defaults(func=_cmd_start)

    transition = sub.add_parser("transition", help="transition a MAC task")
    transition.add_argument("task_id")
    transition.add_argument("target_state")
    transition.add_argument("--actor", required=True)
    transition.add_argument("--detail", default="{}")
    transition.add_argument("--lease-id")
    transition.set_defaults(func=_cmd_transition)

    add_child_task = sub.add_parser("add-child-task", help="create a child task that blocks its parent")
    add_child_task.add_argument("task_id")
    add_child_task.add_argument("title")
    add_child_task.add_argument("--description", default="")
    add_child_task.add_argument("--project")
    add_child_task.add_argument("--priority", type=int)
    add_child_task.add_argument("--required-capabilities")
    add_child_task.add_argument("--dependencies")
    add_child_task.add_argument("--metadata", default="{}")
    add_child_task.add_argument("--max-attempts", type=int)
    add_child_task.add_argument("--actor", default="hermes")
    add_child_task.set_defaults(func=_cmd_add_child_task)

    evidence = sub.add_parser("evidence", help="add MAC task evidence")
    evidence.add_argument("task_id")
    evidence.add_argument("--kind", required=True)
    evidence.add_argument("--uri", required=True)
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--created-by", required=True)
    evidence.add_argument("--checksum")
    evidence.add_argument("--lease-id")
    evidence.add_argument("--metadata", default="{}")
    evidence.set_defaults(func=_cmd_evidence)

    submit_review = sub.add_parser("submit-review", help="submit a task for review")
    submit_review.add_argument("task_id")
    submit_review.add_argument("agent_id")
    submit_review.add_argument("--lease-id")
    submit_review.add_argument("--advance-default-workflow", action="store_true")
    submit_review.set_defaults(func=_cmd_submit_review)

    request_review = sub.add_parser("request-review", help="request a review for a task")
    request_review.add_argument("task_id")
    request_review.add_argument("reviewer_agent_id")
    request_review.add_argument("--actor", default="hermes")
    request_review.set_defaults(func=_cmd_request_review)

    claim_review = sub.add_parser("claim-review", help="claim a review with optional executor evidence context")
    claim_review.add_argument("review_id")
    claim_review.add_argument("reviewer_agent_id")
    claim_review.add_argument("--executor-evidence-id")
    claim_review.add_argument("--actor", default="hermes")
    claim_review.set_defaults(func=_cmd_claim_review)

    review_decision = sub.add_parser("review-decision", help="record a MAC review decision")
    review_decision.add_argument("review_id")
    review_decision.add_argument("status")
    review_decision.add_argument("reviewer_agent_id")
    review_decision.add_argument("--reason")
    review_decision.add_argument("--evidence-id")
    review_decision.set_defaults(func=_cmd_review_decision)

    publish = sub.add_parser("publish", help="publish an approved MAC task")
    publish.add_argument("task_id")
    publish.add_argument("target")
    publish.add_argument("created_by")
    publish.add_argument("--evidence-id")
    publish.set_defaults(func=_cmd_publish)

    command_audit = sub.add_parser("command-audit", help="record or list MAC command audit entries")
    command_audit_sub = command_audit.add_subparsers(dest="command_audit_action", required=True)
    command_audit_record = command_audit_sub.add_parser("record", help="record an audited command phase")
    command_audit_record.add_argument("agent_id")
    command_audit_record.add_argument("--phase", required=True)
    command_audit_record.add_argument("--argv-json", required=True)
    command_audit_record.add_argument("--cwd", required=True)
    command_audit_record.add_argument("--command-id")
    command_audit_record.add_argument("--task-id")
    command_audit_record.add_argument("--lease-id")
    command_audit_record.add_argument("--started-at")
    command_audit_record.add_argument("--completed-at")
    command_audit_record.add_argument("--duration-ms", type=float)
    command_audit_record.add_argument("--returncode", type=int)
    command_audit_record.add_argument("--stdout-sha256")
    command_audit_record.add_argument("--stderr-sha256")
    command_audit_record.add_argument("--stdout-bytes", type=int)
    command_audit_record.add_argument("--stderr-bytes", type=int)
    command_audit_record.add_argument("--metadata", default="{}")
    command_audit_record.set_defaults(func=_cmd_command_audit_record)

    command_audit_list = command_audit_sub.add_parser("list", help="list MAC command audit entries")
    command_audit_list.add_argument("--agent-id")
    command_audit_list.add_argument("--task-id")
    command_audit_list.add_argument("--command-id")
    command_audit_list.add_argument("--phase")
    command_audit_list.add_argument("--since")
    command_audit_list.add_argument("--until")
    command_audit_list.add_argument("--limit", type=int, default=200)
    command_audit_list.set_defaults(func=_cmd_command_audit_list)

    web_search = sub.add_parser("web-search", help="search through the hub Firecrawl-compatible gateway")
    web_search.add_argument("query")
    web_search.add_argument("--limit", type=int, default=5)
    web_search.set_defaults(func=_cmd_web_search)

    web_scrape = sub.add_parser("web-scrape", help="extract one page through the hub Firecrawl-compatible gateway")
    web_scrape.add_argument("url")
    web_scrape.add_argument("--format", action="append", choices=("markdown", "html"), default=None)
    web_scrape.set_defaults(func=_cmd_web_scrape)

    web_crawl = sub.add_parser("web-crawl", help="start a bounded crawl through the hub Firecrawl-compatible gateway")
    web_crawl.add_argument("url")
    web_crawl.add_argument("--limit", type=int, default=1)
    web_crawl.add_argument("--format", action="append", choices=("markdown", "html"), default=None)
    web_crawl.set_defaults(func=_cmd_web_crawl)

    web_crawl_status = sub.add_parser("web-crawl-status", help="fetch a Firecrawl-compatible crawl status")
    web_crawl_status.add_argument("job_id")
    web_crawl_status.set_defaults(func=_cmd_web_crawl_status)

    reply = sub.add_parser("reply", help="render a concise user-facing task status")
    reply.add_argument("task_id")
    reply.set_defaults(func=_cmd_reply)

    writeback = sub.add_parser("writeback", help="prepare and record Hermes memory write-back for a completed task")
    writeback.add_argument("hermes_instance_id")
    writeback.add_argument("task_id")
    writeback.set_defaults(func=_cmd_writeback)

    memory_search = sub.add_parser("memory-search", help="search MAC memory records")
    memory_search.add_argument("--task-id")
    memory_search.add_argument("--subject-type")
    memory_search.add_argument("--subject-id")
    memory_search.add_argument("--record-type")
    memory_search.add_argument("--record-type-prefix")
    memory_search.add_argument("--created-by")
    memory_search.add_argument("--since")
    memory_search.add_argument("--until")
    memory_search.add_argument("--limit", type=int)
    memory_search.add_argument("--order", choices=("asc", "desc"), default="asc")
    memory_search.add_argument("--content-contains")
    memory_search.set_defaults(func=_cmd_memory_search)
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "token", None) is None and getattr(args, "fleet", None):
        from mac.fleet_env import resolve_first as _rt

        args.token = _rt(["MAC_TOKEN", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"], fleet=args.fleet)
    try:
        args.func(args)
    except MacApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
