"""Workflow definition service.

A workflow is a DAG of nodes (typed tasks with a required role) and
edges (transition rules with a matching condition + priority). Workflow
definitions are versioned and immutable per version — updating a
workflow's definition bumps the version so in-flight runs stay
deterministic.

This service owns CRUD + validation + seeding. The runtime that
actually walks a workflow definition lives in
``workflow_runtime.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from mac.models import (
    AgentRole,
    JsonDict,
    NotFoundError,
    Tenant,
    ValidationError,
    Workflow,
    WorkflowDraft,
    ensure_json_object,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)
from mac.observability_service import ObservabilityService
from mac.workflow_models import WorkflowDefinition

SEED_DIR = Path(__file__).resolve().parent / "data" / "workflows"


# ---------------------------------------------------------------------------
# Draft question handling (wf-01)
# ---------------------------------------------------------------------------
#
# Each question on a draft has this normalized shape:
#
#   {
#     "id":              "<unique-within-draft>",   # required; auto-assigned
#     "text":            "<human-readable prompt>",  # required
#     "required":        true | false,               # default false
#     "binds_to_node":   "<node_key>" | None,        # which node consumes the
#                                                    # answer at compile time
#     "binds_to_param":  "instructions" | "metadata.<k>" | None,
#                                                    # default: append the
#                                                    # answer to the node's
#                                                    # instructions as a
#                                                    # "## Pre-supplied
#                                                    # answer:" block.
#   }
#
# Answers are stored as `{question_id: <value>}` on the draft. At
# `approve_draft` time, `_validate_answers` refuses if any required
# question has no answer. `definition_from_draft` then injects each
# answered question's value into the matching node.


def _slug_from_text(text: str, fallback: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:40] or fallback


def _normalize_questions(raw: List[JsonDict]) -> List[JsonDict]:
    """Coerce a list of questions to the canonical shape (assigns ids,
    fills defaults). Idempotent — calling twice yields the same list."""
    seen: set = set()
    out: List[JsonDict] = []
    for index, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("prompt") or item.get("question") or "").strip()
        qid = str(item.get("id") or "").strip()
        if not qid:
            qid = _slug_from_text(text, fallback="q_%d" % (index + 1))
        # Disambiguate collisions when normalizing legacy drafts.
        base = qid
        bump = 2
        while qid in seen:
            qid = "%s_%d" % (base, bump)
            bump += 1
        seen.add(qid)
        question: JsonDict = {
            "id": qid,
            "text": text,
            "required": bool(item.get("required", False)),
        }
        if item.get("binds_to_node"):
            question["binds_to_node"] = str(item["binds_to_node"]).strip()
        if item.get("binds_to_param"):
            question["binds_to_param"] = str(item["binds_to_param"]).strip()
        # Preserve any extra annotation fields the caller added.
        for key, value in item.items():
            if key in {"id", "text", "prompt", "question", "required",
                      "binds_to_node", "binds_to_param"}:
                continue
            question[key] = value
        out.append(question)
    return out


def _migrate_answers_to_ids(
    questions: List[JsonDict],
    answers: JsonDict,
) -> JsonDict:
    """Map legacy text-keyed answers onto the normalized question ids.

    Best-effort: if an answer's key matches a question's `text` we move
    it under that question's `id`. Untouched keys pass through (so
    callers that already key by id keep working).
    """
    if not answers:
        return {}
    by_text = {q["text"]: q["id"] for q in questions if q.get("text")}
    migrated: JsonDict = {}
    for key, value in dict(answers).items():
        if key in {q["id"] for q in questions}:
            migrated[key] = value
        elif key in by_text:
            migrated[by_text[key]] = value
        else:
            # No matching question — carry the entry as-is so we don't
            # silently drop user data, but it won't satisfy any required
            # question check.
            migrated[key] = value
    return migrated


def _validate_answers(
    questions: List[JsonDict],
    answers: JsonDict,
    *,
    node_keys: Optional[Iterable[str]] = None,
) -> None:
    """Raise ValidationError if any required question lacks an answer.

    Also validates that any `binds_to_node` references a real node_key
    (when ``node_keys`` is supplied — it isn't at draft-edit time, only
    at compile time).
    """
    answer_ids = set(answers or {})
    missing = [
        q for q in questions
        if q.get("required") and q["id"] not in answer_ids
    ]
    if missing:
        labels = ", ".join("%s (%s)" % (q["id"], q.get("text") or "?") for q in missing)
        raise ValidationError(
            "workflow draft cannot be approved: required questions unanswered: %s" % labels
        )
    if node_keys is not None:
        valid = set(node_keys)
        bad_bindings = [
            q for q in questions
            if q.get("binds_to_node") and q["binds_to_node"] not in valid
        ]
        if bad_bindings:
            labels = ", ".join(
                "%s -> %s" % (q["id"], q.get("binds_to_node")) for q in bad_bindings
            )
            raise ValidationError(
                "workflow draft questions reference unknown node_keys: %s" % labels
            )


def _apply_question_bindings(
    node: JsonDict,
    questions: List[JsonDict],
    answers: JsonDict,
) -> JsonDict:
    """Stamp question answers onto a compiled node payload.

    For each question with `binds_to_node == node["node_key"]`:
      * stash the answer in ``node.metadata["answers"][question_id]``
        (always — useful for programmatic consumers)
      * unless ``binds_to_param`` says otherwise, also append a
        ``## Pre-supplied answer: <text>\\n<answer>`` block to the
        node's ``instructions`` so an LLM-driven executor sees it
        without needing to read metadata.
    """
    node_key = node.get("node_key")
    relevant = [q for q in questions if q.get("binds_to_node") == node_key]
    if not relevant:
        return node
    metadata = dict(node.get("metadata") or {})
    answers_bucket = dict(metadata.get("answers") or {})
    instructions_blocks: List[str] = []
    for q in relevant:
        if q["id"] not in (answers or {}):
            continue  # required-ness already enforced; optional + absent = skip
        answer = answers[q["id"]]
        answers_bucket[q["id"]] = answer
        param = q.get("binds_to_param") or ""
        if param.startswith("metadata."):
            path = param.split(".", 1)[1]
            metadata[path] = answer
        elif param == "instructions":
            # Replace instructions entirely (template-style use).
            node["instructions"] = str(answer)
        else:
            # Default: append a readable answer block to instructions.
            instructions_blocks.append(
                "## Pre-supplied answer: %s\n%s" % (q.get("text") or q["id"], answer)
            )
    if instructions_blocks:
        existing = str(node.get("instructions") or "").rstrip()
        node["instructions"] = (existing + "\n\n" if existing else "") + "\n\n".join(instructions_blocks)
    metadata["answers"] = answers_bucket
    node["metadata"] = metadata
    return node


class WorkflowService:
    def __init__(
        self,
        store: Any,
        observability: ObservabilityService,
        *,
        get_role: Callable[..., AgentRole],
        get_tenant: Callable[[str], Tenant],
    ) -> None:
        self.store = store
        self.observability = observability
        self._get_role = get_role
        self._get_tenant = get_tenant

    # CRUD ---------------------------------------------------------------

    def create_workflow(
        self,
        slug: str,
        name: str,
        description: str,
        workflow_type: str,
        definition: Dict[str, Any],
        created_by: str,
        *,
        tenant_id: Optional[str] = None,
        is_default: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        workflow_id: Optional[str] = None,
    ) -> Workflow:
        slug_value = (slug or "").strip()
        if not slug_value:
            raise ValidationError("workflow slug is required")
        name_value = (name or "").strip()
        if not name_value:
            raise ValidationError("workflow name is required")
        workflow_type_value = (workflow_type or "").strip()
        if not workflow_type_value:
            raise ValidationError("workflow_type is required")
        if tenant_id is not None:
            self._get_tenant(tenant_id)
        parsed_definition = self._validate_definition(definition, tenant_id=tenant_id)
        normalized_definition = parsed_definition.to_dict()

        existing = self.store.query_all(
            """
            SELECT * FROM workflows
            WHERE slug = ? AND (tenant_id IS ? OR tenant_id = ?)
            ORDER BY version DESC
            LIMIT 1
            """,
            (slug_value, tenant_id, tenant_id),
        )
        existing_row = existing[0] if existing else None
        version = 1
        if existing_row is not None:
            existing_definition = json_loads(existing_row["definition"], {})
            if existing_definition == normalized_definition:
                # Same definition: keep the existing row, just update
                # mutable metadata fields.
                self.store.execute(
                    """
                    UPDATE workflows
                    SET name = ?, description = ?, workflow_type = ?,
                        is_default = ?, metadata = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name_value,
                        description,
                        workflow_type_value,
                        1 if is_default else 0,
                        json_dumps(ensure_json_object(metadata)),
                        utcnow(),
                        existing_row["id"],
                    ),
                )
                return self.get_workflow(existing_row["id"])
            version = int(existing_row["version"]) + 1

        wid = workflow_id or new_id("workflow")
        now = utcnow()
        self.store.execute(
            """
            INSERT INTO workflows (
                id, slug, name, description, workflow_type, is_default,
                version, definition, tenant_id, enabled, metadata,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                wid,
                slug_value,
                name_value,
                description,
                workflow_type_value,
                1 if is_default else 0,
                version,
                json_dumps(normalized_definition),
                tenant_id,
                json_dumps(ensure_json_object(metadata)),
                created_by,
                now,
                now,
            ),
        )
        return self.get_workflow(wid)

    def update_workflow(self, workflow_id: str, **patch: Any) -> Workflow:
        wf = self.get_workflow(workflow_id)
        if "definition" in patch and patch["definition"] is not None:
            return self.create_workflow(
                slug=patch.get("slug") or wf.slug,
                name=patch.get("name") or wf.name,
                description=patch.get("description", wf.description),
                workflow_type=patch.get("workflow_type") or wf.workflow_type,
                definition=patch["definition"],
                created_by=patch.get("created_by") or wf.created_by,
                tenant_id=patch.get("tenant_id", wf.tenant_id),
                is_default=patch.get("is_default", wf.is_default),
                metadata=patch.get("metadata", wf.metadata),
            )
        # Pure metadata-only update.
        updates: Dict[str, Any] = {}
        if "name" in patch and patch["name"] is not None:
            updates["name"] = patch["name"]
        if "description" in patch and patch["description"] is not None:
            updates["description"] = patch["description"]
        if "workflow_type" in patch and patch["workflow_type"] is not None:
            updates["workflow_type"] = patch["workflow_type"]
        if "is_default" in patch and patch["is_default"] is not None:
            updates["is_default"] = 1 if patch["is_default"] else 0
        if "metadata" in patch and patch["metadata"] is not None:
            updates["metadata"] = json_dumps(ensure_json_object(patch["metadata"]))
        if "enabled" in patch and patch["enabled"] is not None:
            updates["enabled"] = 1 if patch["enabled"] else 0
        if not updates:
            return wf
        set_clause = ", ".join("%s = ?" % key for key in updates)
        params = list(updates.values()) + [utcnow(), wf.id]
        self.store.execute(
            "UPDATE workflows SET %s, updated_at = ? WHERE id = ?" % set_clause,
            params,
        )
        return self.get_workflow(wf.id)

    def get_workflow(
        self,
        workflow_id_or_slug: str,
        *,
        tenant_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Workflow:
        # By id first (UUID-shaped). Fallback to slug+tenant.
        row = self.store.query_one(
            "SELECT * FROM workflows WHERE id = ?", (workflow_id_or_slug,)
        )
        if row is None:
            clauses = ["slug = ?"]
            params: List[Any] = [workflow_id_or_slug]
            if tenant_id is not None:
                clauses.append("tenant_id = ?")
                params.append(tenant_id)
            else:
                clauses.append("tenant_id IS NULL")
            if version is not None:
                clauses.append("version = ?")
                params.append(int(version))
            sql = (
                "SELECT * FROM workflows WHERE "
                + " AND ".join(clauses)
                + " ORDER BY version DESC LIMIT 1"
            )
            row = self.store.query_one(sql, tuple(params))
        if row is None:
            raise NotFoundError("workflow not found: %s" % workflow_id_or_slug)
        return self._from_row(row)

    def list_workflows(
        self,
        *,
        tenant_id: Optional[str] = None,
        workflow_type: Optional[str] = None,
        enabled: Optional[bool] = True,
    ) -> List[Workflow]:
        clauses: List[str] = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("(tenant_id = ? OR tenant_id IS NULL)")
            params.append(tenant_id)
        if workflow_type is not None:
            clauses.append("workflow_type = ?")
            params.append(workflow_type)
        if enabled is not None:
            clauses.append("enabled = ?")
            params.append(1 if enabled else 0)
        sql = "SELECT * FROM workflows"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY slug, version DESC"
        return [self._from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def enable_workflow(self, workflow_id: str) -> Workflow:
        return self.update_workflow(workflow_id, enabled=True)

    def disable_workflow(self, workflow_id: str) -> Workflow:
        return self.update_workflow(workflow_id, enabled=False)

    def delete_workflow(self, workflow_id: str) -> None:
        wf = self.get_workflow(workflow_id)
        active = self.store.query_one(
            """
            SELECT id FROM workflow_runs
            WHERE workflow_id = ? AND state NOT IN ('completed','failed','cancelled')
            LIMIT 1
            """,
            (wf.id,),
        )
        if active is not None:
            raise ValidationError(
                "workflow %s cannot be deleted while runs are in flight" % wf.slug
            )
        self.store.execute("DELETE FROM workflows WHERE id = ?", (wf.id,))

    # Draft planning + dry-run preview ------------------------------------

    def create_draft(
        self,
        goal: str,
        *,
        created_by: str = "human",
        tenant_id: Optional[str] = None,
        proposed_steps: Optional[List[Dict[str, Any]]] = None,
        questions: Optional[List[Dict[str, Any]]] = None,
        answers: Optional[Dict[str, Any]] = None,
        draft_id: Optional[str] = None,
    ) -> WorkflowDraft:
        goal_value = str(goal or "").strip()
        if not goal_value:
            raise ValidationError("workflow draft goal is required")
        if tenant_id is not None:
            self._get_tenant(tenant_id)
        now = utcnow()
        did = draft_id or new_id("wfdraft")
        normalized_questions = _normalize_questions(list(questions or []))
        normalized_answers = _migrate_answers_to_ids(
            normalized_questions,
            ensure_json_object(answers),
        )
        self.store.execute(
            """
            INSERT INTO workflow_drafts (
                id, tenant_id, goal, status, proposed_steps, questions, answers,
                edit_history, compiled_workflow_id, created_by, created_at,
                updated_at, approved_at
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, '[]', NULL, ?, ?, ?, NULL)
            """,
            (
                did,
                tenant_id,
                goal_value,
                json_dumps(list(proposed_steps or [])),
                json_dumps(normalized_questions),
                json_dumps(normalized_answers),
                created_by,
                now,
                now,
            ),
        )
        return self.get_draft(did)

    def update_draft(
        self,
        draft_id: str,
        *,
        goal: Optional[str] = None,
        proposed_steps: Optional[List[Dict[str, Any]]] = None,
        questions: Optional[List[Dict[str, Any]]] = None,
        answers: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        actor: str = "human",
    ) -> WorkflowDraft:
        draft = self.get_draft(draft_id)
        status_value = (status or draft.status).strip().lower()
        if status_value not in {"draft", "questions", "approved", "compiled", "cancelled"}:
            raise ValidationError("unsupported workflow draft status: %s" % status)
        now = utcnow()
        history = list(draft.edit_history)
        patch: JsonDict = {}
        if goal is not None:
            patch["goal"] = str(goal).strip()
        if proposed_steps is not None:
            patch["proposed_steps"] = list(proposed_steps)
        if questions is not None:
            patch["questions"] = _normalize_questions(list(questions))
        if answers is not None:
            # Migrate answers against the *new* question set if the caller
            # is updating both, otherwise the existing draft's questions.
            effective_questions = (
                patch.get("questions")
                if "questions" in patch
                else list(draft.questions)
            )
            patch["answers"] = _migrate_answers_to_ids(
                effective_questions,
                ensure_json_object(answers),
            )
        if status is not None:
            patch["status"] = status_value
        history.append({"actor": actor, "at": now, "patch": patch})
        self.store.execute(
            """
            UPDATE workflow_drafts
            SET goal = ?, status = ?, proposed_steps = ?, questions = ?,
                answers = ?, edit_history = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                patch.get("goal", draft.goal),
                status_value,
                json_dumps(patch.get("proposed_steps", draft.proposed_steps)),
                json_dumps(patch.get("questions", draft.questions)),
                json_dumps(patch.get("answers", draft.answers)),
                json_dumps(history),
                now,
                draft.id,
            ),
        )
        return self.get_draft(draft.id)

    def get_draft(self, draft_id: str) -> WorkflowDraft:
        row = self.store.query_one("SELECT * FROM workflow_drafts WHERE id = ?", (draft_id,))
        if row is None:
            raise NotFoundError("workflow draft not found: %s" % draft_id)
        return self._draft_from_row(row)

    def list_drafts(
        self,
        *,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[WorkflowDraft]:
        clauses: List[str] = []
        params: List[Any] = []
        if tenant_id is not None:
            clauses.append("(tenant_id = ? OR tenant_id IS NULL)")
            params.append(tenant_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        sql = "SELECT * FROM workflow_drafts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(min(max(1, int(limit)), 1000))
        return [self._draft_from_row(row) for row in self.store.query_all(sql, tuple(params))]

    def preview_definition(
        self,
        definition: Dict[str, Any],
        *,
        tenant_id: Optional[str] = None,
        input: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        parsed = self._validate_definition(definition, tenant_id=tenant_id)
        return parsed.task_preview(input)

    def preview_workflow(
        self,
        workflow_id_or_slug: str,
        *,
        tenant_id: Optional[str] = None,
        input: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        workflow = self.get_workflow(workflow_id_or_slug, tenant_id=tenant_id)
        preview = self.preview_definition(workflow.definition, tenant_id=workflow.tenant_id, input=input)
        preview["workflow_id"] = workflow.id
        preview["workflow_version"] = workflow.version
        preview["snapshot_sha256"] = self._definition_fingerprint(workflow.definition)
        return preview

    def preview_draft(
        self,
        draft_id: str,
        *,
        workflow_type: str = "custom",
        input: Optional[Dict[str, Any]] = None,
    ) -> JsonDict:
        draft = self.get_draft(draft_id)
        definition = self.definition_from_draft(draft, workflow_type=workflow_type)
        preview = self.preview_definition(definition, tenant_id=draft.tenant_id, input=input)
        preview["draft_id"] = draft.id
        return preview

    def approve_draft(
        self,
        draft_id: str,
        *,
        slug: str,
        name: str,
        workflow_type: str = "custom",
        approved_by: str = "human",
        is_default: bool = False,
    ) -> Workflow:
        draft = self.get_draft(draft_id)
        node_keys = [
            str(step.get("node_key") or step.get("key") or "step_%d" % (i + 1)).strip()
            for i, step in enumerate(draft.proposed_steps or [])
        ]
        _validate_answers(draft.questions, draft.answers, node_keys=node_keys)
        definition = self.definition_from_draft(draft, workflow_type=workflow_type)
        workflow = self.create_workflow(
            slug=slug,
            name=name,
            description=draft.goal,
            workflow_type=workflow_type,
            definition=definition,
            created_by=approved_by,
            tenant_id=draft.tenant_id,
            is_default=is_default,
            metadata={"draft_id": draft.id, "answers": draft.answers},
        )
        now = utcnow()
        history = list(draft.edit_history)
        history.append(
            {
                "actor": approved_by,
                "at": now,
                "patch": {"status": "compiled", "compiled_workflow_id": workflow.id},
            }
        )
        self.store.execute(
            """
            UPDATE workflow_drafts
            SET status = 'compiled', compiled_workflow_id = ?, edit_history = ?,
                approved_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (workflow.id, json_dumps(history), now, now, draft.id),
        )
        return workflow

    def definition_from_draft(
        self,
        draft: WorkflowDraft,
        *,
        workflow_type: str = "custom",
    ) -> JsonDict:
        if not draft.proposed_steps:
            raise ValidationError("workflow draft has no proposed_steps")
        nodes: List[JsonDict] = []
        edges: List[JsonDict] = []
        previous = ""
        for index, step in enumerate(draft.proposed_steps):
            key = str(step.get("node_key") or step.get("key") or "step_%d" % (index + 1)).strip()
            role = str(step.get("role_required") or step.get("role") or "").strip()
            if not role:
                raise ValidationError("workflow draft step %s missing role_required" % key)
            node = {
                "node_key": key,
                "node_type": str(step.get("node_type") or "task").strip().lower(),
                "role_required": role,
                "instructions": str(step.get("instructions") or step.get("title") or "").strip(),
                "max_attempts": int(step.get("max_attempts") or 1),
                "timeout_minutes": int(step.get("timeout_minutes") or 0),
                "metadata": {
                    "draft_id": draft.id,
                    "draft_goal": draft.goal,
                    "draft_answers": draft.answers,
                    **ensure_json_object(step.get("metadata")),
                },
            }
            if step.get("required_capabilities"):
                node["required_capabilities"] = list(step.get("required_capabilities") or [])
            node = _apply_question_bindings(node, draft.questions, draft.answers)
            nodes.append(node)
            edges.append(
                {
                    "from_node_key": previous,
                    "to_node_key": key,
                    "condition": "success",
                    "priority": 100 - index,
                }
            )
            previous = key
        edges.append(
            {
                "from_node_key": previous,
                "to_node_key": "",
                "condition": "success",
                "priority": 1,
            }
        )
        return {"metadata": {"draft_id": draft.id, "workflow_type": workflow_type}, "nodes": nodes, "edges": edges}

    def _definition_fingerprint(self, definition: Dict[str, Any]) -> str:
        import hashlib

        return "sha256:" + hashlib.sha256(json_dumps(definition).encode("utf-8")).hexdigest()

    # YAML import + seed -----------------------------------------------

    # 256 KiB is well above any legitimate workflow YAML we've seen and
    # below the threshold where yaml.safe_load alias expansion becomes
    # an attractive DoS vector (mac-i044).
    YAML_IMPORT_MAX_BYTES = 256 * 1024

    def import_yaml(
        self,
        yaml_text: str,
        *,
        created_by: str,
        tenant_id: Optional[str] = None,
        is_default: bool = False,
    ) -> Workflow:
        import yaml as _yaml

        if len(yaml_text.encode("utf-8")) > self.YAML_IMPORT_MAX_BYTES:
            raise ValidationError(
                "workflow YAML exceeds %d byte limit" % self.YAML_IMPORT_MAX_BYTES
            )
        raw = _yaml.safe_load(yaml_text)
        if not isinstance(raw, dict):
            raise ValidationError("workflow YAML must be a mapping")
        definition = self._yaml_to_definition(raw)
        return self.create_workflow(
            slug=raw.get("id") or raw.get("workflow_type") or "",
            name=raw.get("name", ""),
            description=raw.get("description", ""),
            workflow_type=raw.get("workflow_type") or "",
            definition=definition,
            created_by=created_by,
            tenant_id=tenant_id,
            is_default=is_default,
        )

    def seed_defaults(self, *, source: Optional[Path] = None) -> List[Workflow]:
        directory = source or SEED_DIR
        if not directory.exists():
            raise NotFoundError("workflow seed directory missing: %s" % directory)
        seeded: List[Workflow] = []
        for path in sorted(directory.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            seeded.append(
                self.create_workflow(
                    slug=payload["slug"],
                    name=payload["name"],
                    description=payload.get("description", ""),
                    workflow_type=payload["workflow_type"],
                    definition=payload["definition"],
                    created_by="seed",
                    is_default=True,
                )
            )
        return seeded

    # Validation -------------------------------------------------------

    def _validate_definition(
        self, definition: Any, *, tenant_id: Optional[str] = None
    ) -> WorkflowDefinition:
        parsed = WorkflowDefinition.parse(definition)
        for node in parsed.nodes:
            try:
                self._get_role(node.role_required, tenant_id=tenant_id)
            except TypeError:
                self._get_role(node.role_required)
            except NotFoundError:
                raise ValidationError(
                    "workflow node %s references unknown role: %s"
                    % (node.node_key, node.role_required)
                )
        return parsed

    def _yaml_to_definition(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        nodes: List[Dict[str, Any]] = []
        for node in raw.get("nodes") or []:
            slug = self._slug_from_node(node)
            nodes.append(
                {
                    "node_key": node.get("node_key"),
                    "node_type": (node.get("node_type") or "task").lower(),
                    "role_required": slug,
                    "persona_hint": node.get("persona_hint"),
                    "max_attempts": int(node.get("max_attempts") or 1),
                    "timeout_minutes": int(node.get("timeout_minutes") or 0),
                    "instructions": (node.get("instructions") or "").strip(),
                }
            )
        edges: List[Dict[str, Any]] = []
        for edge in raw.get("edges") or []:
            edges.append(
                {
                    "from_node_key": edge.get("from_node_key") or "",
                    "to_node_key": edge.get("to_node_key") or "",
                    "condition": (edge.get("condition") or "success").lower(),
                    "priority": int(edge.get("priority") or 100),
                }
            )
        return {"nodes": nodes, "edges": edges}

    def _slug_from_node(self, node: Dict[str, Any]) -> str:
        hint = node.get("persona_hint")
        if isinstance(hint, str) and "/" in hint:
            return hint.split("/", 1)[1].strip()
        label = node.get("role_required")
        if isinstance(label, str):
            return label.lower().replace(" ", "-")
        raise ValidationError("workflow node missing role hint")

    # Row hydration ----------------------------------------------------

    def _from_row(self, row: Any) -> Workflow:
        return Workflow(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            workflow_type=row["workflow_type"],
            is_default=bool(row["is_default"]),
            version=int(row["version"]),
            definition=json_loads(row["definition"], {}),
            tenant_id=row["tenant_id"],
            enabled=bool(row["enabled"]),
            metadata=json_loads(row["metadata"], {}),
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _draft_from_row(self, row: Any) -> WorkflowDraft:
        # Normalize on read so older drafts (which may have free-form
        # questions without ids) present the canonical shape to callers.
        raw_questions = json_loads(row["questions"], [])
        raw_answers = json_loads(row["answers"], {})
        normalized_questions = _normalize_questions(raw_questions)
        normalized_answers = _migrate_answers_to_ids(normalized_questions, raw_answers)
        return WorkflowDraft(
            id=row["id"],
            tenant_id=row["tenant_id"],
            goal=row["goal"],
            status=row["status"],
            proposed_steps=json_loads(row["proposed_steps"], []),
            questions=normalized_questions,
            answers=normalized_answers,
            edit_history=json_loads(row["edit_history"], []),
            compiled_workflow_id=row["compiled_workflow_id"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approved_at=row["approved_at"],
        )
