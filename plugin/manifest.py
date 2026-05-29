
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

REQUIRED_ENV = ["MAC_URL", "MAC_WORKER_TOKEN", "MAC_HERMES_INSTANCE_ID"]
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
        path="/hermes-instances/{hermes_instance_id}/tasks",
        description=(
            "Create a durable task in mac for this Hermes instance. Use when "
            "the user asks for work that should be tracked, executed, and "
            "audited (e.g. 'refactor the auth module', 'investigate the deploy "
            "failure', 'create a daily report'). Pass `title` (short imperative "
            "summary), `summary` (longer brief — what the work is, scope, "
            "success criteria), optional `required_capabilities` (e.g. "
            "['ops','python']), optional `priority` integer, optional "
            "`platform_binding_id` to tie the task back to a Telegram/Slack "
            "thread, optional `conversation_ref` URI, optional `snippets` "
            "list. Do not include private user memory or transcripts in the "
            "brief — only sanitized operational context."
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
            "reviewing/completed/failed/cancelled). Optional `tenant_id`."
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
        path="/hermes-instances/{hermes_instance_id}/work-context",
        description=(
            "Get the mac authoritative work projection for this Hermes "
            "instance — what tasks are open/in-flight, what projects they "
            "roll up to, which agents are assigned. Use when the user asks "
            "an open-ended 'what are you (or the fleet) working on?' "
            "question. Optional `include_completed` boolean (default true), "
            "optional `task_limit` integer (default 100)."
        ),
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}
