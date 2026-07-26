
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REQUIRED_ENV = ["MAC_URL", "MAC_WORKER_TOKEN", "MAC_PERSONA_INSTANCE_ID"]
PLUGIN_NAME = "mac"
TOOLSET = "mac"


@dataclass(frozen=True)
class ToolSpec:

    name: str
    method: str
    path: str
    description: str
    toolset: str = TOOLSET
    timeout: Optional[float] = None
    retry: bool = True


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="mac_create_task",
        method="POST",
        path="/persona-instances/{persona_instance_id}/tasks",
        description=(
            "Create a durable task in mac for this persona instance. Use when "
            "the user asks for work that should be tracked, executed, and "
            "audited (e.g. 'refactor the auth module', 'investigate the deploy "
            "failure', 'create a daily report'). Pass `title` (short imperative "
            "summary), `summary` (longer brief — target system/repository, "
            "scope, acceptance criteria, and sanitized evidence), plus routing "
            "metadata whenever the work should be executed: set `project` when "
            "the target project is known. For MAC control-plane/API/UI/runner/plugin "
            "work use `project: mac`; MAC will apply the project's `task_defaults.role`. "
            "Do not send language/domain labels like `typescript`, `frontend`, or "
            "`design` as `required_capabilities` for normal project work. Only set "
            "`required_capabilities` for hard runtime needs that the runner must "
            "physically provide, such as `ops` for Kubernetes/deploy/infra access. "
            "Put language/domain labels like `typescript`, `frontend`, or `ui` in "
            "`tags` instead. If you cannot determine the project, ask a "
            "clarifying question or call `mac_work_brief` before creating the task "
            "instead of creating an unroutable task. Optional `priority` integer, "
            "optional `platform_binding_id` to tie the task back to a Telegram/Slack "
            "thread, optional `conversation_ref` URI, optional `snippets` list. "
            "Do not include private user memory or transcripts in the brief — only "
            "sanitized operational context."
        ),
    ),
    ToolSpec(
        name="mac_list_tasks",
        method="GET",
        path="/tasks",
        description=(
            "List tasks in mac. Use when the user asks 'what's going on?', "
            "'show me my tasks', or wants a quick status of pending work. "
            "Optional `state` filter (open/claimed/running/needs_review/"
            "reviewing/completed/failed/cancelled). Optional `tenant_id`. "
            "Optional `project` to scope to one project (server-side filter). "
            "Optional `limit` integer to cap the result set."
        ),
    ),
    ToolSpec(
        name="mac_get_task",
        method="GET",
        path="/tasks/{task_id}",
        description=(
            "Fetch the full record for one task by id. Use when the user "
            "references a specific task — its evidence, lease state, history. "
            "For a concise user-facing status string, prefer mac_task_summary."
        ),
    ),
    ToolSpec(
        name="mac_task_summary",
        method="GET",
        path="/tasks/{task_id}/summary",
        description=(
            "Fetch the durable user-facing summary for one task by id. Use "
            "when the user asks for the status of a specific task in a way "
            "you can speak back to them in natural language."
        ),
    ),
    ToolSpec(
        name="mac_cancel_task",
        method="POST",
        path="/tasks/{task_id}/transition",
        description=(
            "Cancel a task by transitioning it to the cancelled state. Use "
            "ONLY when the user explicitly asks to cancel/stop/abort a "
            "specific task they have referenced. Body should pass `actor` "
            "(use the bound agent id) and `detail` with `reason`. The plugin "
            "fills `target_state=cancelled` automatically — do not include "
            "target_state in the args."
        ),
    ),
    ToolSpec(
        name="mac_work_brief",
        method="GET",
        path="/persona-instances/{persona_instance_id}/work-context",
        description=(
            "Get the mac authoritative work projection for this persona "
            "instance — what tasks are open/in-flight, what projects they "
            "roll up to, which agents are assigned. Use when the user asks "
            "an open-ended 'what are you (or the fleet) working on?' "
            "question. Optional `include_completed` boolean (default true), "
            "optional `task_limit` integer (default 100)."
        ),
    ),
    ToolSpec(
        name="mac_pending_notifications",
        method="GET",
        path="/notifications",
        description=(
            "List undelivered task notifications (status=pending, "
            "subject_type=task) that this persona instance still needs to "
            "announce. mac's database is the source of truth for what has "
            "already been delivered, so call this to learn which task "
            "updates are new instead of tracking state locally. Returns a "
            "list of notification records; deliver each, then call "
            "mac_ack_notification with its id so it is not returned again. "
            "Optional `limit` integer (default 100)."
        ),
    ),
    ToolSpec(
        name="mac_ack_notification",
        method="POST",
        path="/notifications/{notification_id}/delivered",
        description=(
            "Mark one task notification as delivered after you have "
            "successfully announced it. Idempotent — safe to call more than "
            "once. Pass `notification_id`. This flips the notification's "
            "status from pending to delivered in mac so it stops coming back "
            "from mac_pending_notifications."
        ),
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}
