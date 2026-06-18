# Hermes Integration

Phase 2 provides a thin Hermes-facing adapter around the `mac` HTTP API. The
adapter lives in `mac.hermes_adapter` and is exposed as the `mac-hermes` CLI.

The adapter is intentionally not a worker runtime. It gives Hermes enough API
surface to register itself, create durable tasks from sanitized conversation
context, fetch concise task status for user replies, and prepare completed-task
memory write-back payloads.

## What Is Implemented

- `MacApiClient`: small standard-library HTTP client for the `mac` API.
- `HermesMacAdapter`: high-level helper for Hermes skills or gateway code.
- Idempotent identity registration for tenants, personas, Hermes instances,
  and platform bindings.
- Conversation-to-task creation that stores only sanitized operational context.
- `GET /tasks/{id}/summary` for user-facing status summaries.
- Completed-task memory write-back payloads targeted at the Hermes
  `memory_scope`.
- Optional operational record that a write-back was prepared or sent.

## Registration

Hermes should register its durable identity on startup:

```python
from mac.hermes_adapter import HermesMacAdapter, MacApiClient, PlatformBindingSpec

adapter = HermesMacAdapter(MacApiClient("http://127.0.0.1:8000"))
registration = adapter.register_identity(
    tenant_name="personal",
    persona_name="AssistantOne",
    instance_name="assistant-one",
    soul_ref="hermes://personal/assistant-one/SOUL.md",
    memory_scope="hermes://personal/assistant-one/memory",
    platform_bindings=[
        PlatformBindingSpec("slack", "T123/C456", "#ops"),
    ],
)
```

In production, configure `MAC_API_TOKEN` / `MAC_API_TOKENS` on the API and pass
the matching Hermes token to `MacApiClient(..., token="...")`. Hermes generally
needs `write` to create identity/task records and `read` to fetch summaries; a
worker gateway may instead use narrower `agent` or `dispatch` scopes.

The returned `hermes_instance.id` is the durable identity used for later task
creation.

## Startup State Check

`mac` also performs a metadata-only Hermes startup check when the API process
starts. The check inventories local Hermes-owned state references such as
`SOUL.md`, `USER.md`, `MEMORY.md`, `state.db`, `memory_store.db`,
`kanban.db`, `auth.json`, `.env`, `slack_accounts.json`, Slack channel
mapping files, and selected `~/.acc/data` references. It returns only paths,
existence, sizes, and mtimes; file contents are never returned.

The report is exposed at:

```bash
curl $MAC_URL/startup/hermes
```

By default, missing state produces warnings but does not stop `mac`, which
keeps local development and fresh installs simple. Set
`MAC_REQUIRE_HERMES_STARTUP_READY=1` in production if startup should fail until
the expected Hermes soul, memory, state, and Slack activation contract is
present.

Relevant environment:

- `HERMES_HOME`: Hermes state directory; defaults to `~/.hermes`.
- `ACC_DIR`: legacy ACC data directory for migration references; defaults to
  `~/.acc`.
- `MAC_HERMES_AGENT_DIR` / `HERMES_AGENT_DIR`: upstream Hermes checkout to
  inspect for the Slack account-file activation shim.
- `MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM=0`: disable the startup shim patcher
  when `MAC_HERMES_AGENT_DIR` points at an explicit checkout. It is enabled by
  default only for explicit checkout paths.
- `MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM=0`: disable startup patching of
  upstream Hermes gateway model/runtime overrides.
- `MAC_HERMES_GATEWAY_MODEL`: per-agent model selector. Fleet deploy also
  mirrors it to `HERMES_INFERENCE_MODEL` so gateway conversations and worker
  oneshot execution use the same model on that host.
- `MAC_HERMES_GATEWAY_PROVIDER`: provider selector for the per-agent model.
  Fleets normally use `custom` with TokenHub as the shared OpenAI-compatible
  endpoint.
- `MAC_HERMES_GATEWAY_BASE_URL`: optional explicit base URL. Usually omitted
  when `TOKENHUB_URL` is available because mac derives `${TOKENHUB_URL}/v1`.
- `MAC_HERMES_STARTUP_CHECK=0`: disable the check.
- `MAC_REQUIRE_HERMES_STARTUP_READY=1`: fail startup when warnings are present.
- `MAC_HERMES_SLACK_HOME_CHANNEL_NAME`: Slack home-channel name, without `#`,
  used by fleet deploy to materialize `slack_home_channels.json`.
- `MAC_HERMES_SYNC_SLACK_HOME_CHANNELS=0`: preserve existing home-channel files
  without discovery.

One current deployment caveat matters for an upstream-Hermes-based install:
upstream `NousResearch/hermes-agent` enables Slack from `SLACK_BOT_TOKEN` or
explicit Slack config. If a deployed agent relies only on
`slack_accounts.json`, `mac` applies the account-file activation shim from
ACC's `deploy/setup-hermes-venv.sh` when `MAC_HERMES_AGENT_DIR` explicitly
points at the Hermes checkout. Without an explicit checkout path, `mac` only
reports that the shim is missing and marks startup unready.

The same explicit-checkout rule applies to the gateway runtime shim. When a
per-agent model/provider/base URL is configured, `mac` patches upstream Hermes
`gateway/run.py` so the gateway resolves runtime credentials from TokenHub or
host-local secrets while preserving the agent-specific model. This is how mac
keeps configured agents on different model families for review diversity
without forking Hermes or storing provider secrets in Git.

## Creating Tasks

Hermes should summarize the user request and pass sanitized context:

```python
from mac.hermes_adapter import ConversationTaskInput

task = adapter.create_task_from_conversation(
    registration["hermes_instance"]["id"],
    ConversationTaskInput(
        title="Investigate failed deploy",
        summary="Deploy failed after the package publish step.",
        platform_binding_id=registration["platform_bindings"][0]["id"],
        conversation_ref="slack://T123/C456/1712345678.000100",
        required_capabilities=["ops"],
        snippets=["User-visible error: publish returned 500"],
    ),
)
```

Do not pass raw transcripts, private memory, provider tokens, or full
`MEMORY.md` contents. The adapter drops obvious secret and private-memory
metadata keys, but Hermes should still choose a minimal task brief.

## Work Context

Hermes can load MAC's authoritative task, project, and agent projection for an
instance:

```python
context = adapter.work_context(registration["hermes_instance"]["id"])
open_tasks = adapter.list_tasks(state="open")
```

The payload is `mac.hermes_work_context.v1`. It keeps task, project, and agent
authority in MAC while preserving Hermes as the authority for personality and
user memory. It includes visible tasks, project frontier summaries, agent
assignments, task dependencies, Hermes task origins, and stable API/CLI
operation hints. Hermes should treat those operations as affordances over MAC's
durable task objects, not as local state to copy.

The same contract is available from:

```bash
mac hermes work-context <hermes_instance_id>
mac-hermes work-context <hermes_instance_id>
mac-hermes tasks --state open
```

Hermes can also use the same project bridge operators use. This keeps
Beads-backed project registration, issue import, and repository polling as MAC
state instead of hidden local Hermes state:

```python
adapter.list_projects()
adapter.project_detail("repo-beads-nanolang")
adapter.list_project_items()
adapter.import_project_item(
    "repo-beads-nanolang",
    "nanolang-42",
    "Update parser dependency",
    project="repo-beads-nanolang",
    priority=10,
    dependencies=["task_parent"],
)
adapter.register_beads_repository("nanolang", "/Users/jordanh/Src/nanolang", project="repo-beads-nanolang")
adapter.list_beads_repositories()
adapter.poll_beads_repositories(repository="nanolang", force=True)
```

```bash
mac project list
mac project show repo-beads-nanolang
mac-hermes projects
mac-hermes project-detail repo-beads-nanolang
mac-hermes project-items
mac-hermes import-project-item repo-beads-nanolang nanolang-42 "Update parser dependency" --project repo-beads-nanolang --priority 10 --dependencies task_parent
mac-hermes beads-repositories
mac-hermes register-beads-repository nanolang /Users/jordanh/Src/nanolang --project repo-beads-nanolang
mac-hermes poll-beads-repositories --repository nanolang --force
```

Agent state is also MAC-owned. Hermes can inspect the same agent records and
composed identity that operators see:

```python
adapter.list_agents()
adapter.agent_detail(agent_id)
adapter.agent_identity(agent_id)
```

```bash
mac-hermes agents
mac-hermes agent-detail <agent_id>
mac-hermes agent-identity <agent_id>
hgmac agents list
hgmac agents identity <agent_id>
```

Operators and Hermes agents can also request an auditable readiness proof for
the bridge:

```python
proof = adapter.runtime_proof(registration["hermes_instance"]["id"])
```

```bash
mac hermes runtime-proof <hermes_instance_id>
mac-hermes runtime-proof <hermes_instance_id>
```

The proof payload is `mac.hermes_runtime_proof.v1`. It checks that the API work
context, MAC and Hermes CLI affordances, dashboard projection, deployed runtime
context, prompt bridge, and bound agent identity all agree on the same
task/project/agent authority model. Its evidence includes a first-class object
matrix for tasks, projects, and agents across API operations, MAC CLI commands,
Hermes-facing commands, dashboard projection fields, bookmarkable dashboard
URLs, and runtime session capabilities. It also includes a live alignment section that compares current
MAC task, project, and agent records against the Hermes work-context projection
so a ready proof means Hermes is seeing the same operational objects MAC sees.
The dashboard proof covers `/ui` views and URL state parameters such as
`view`, `project`, `task_state`, `selected`, and agent table filters, so a
task, project, agent, or Hermes runtime proof can be shared as a direct URL
rather than reconstructed from local UI state.

When `mac-hermes runtime-proof` runs inside a Hermes agent, it sends that
agent's local `build_hermes_startup_report()` to the hub proof endpoint. That
lets MAC validate the caller's actual runtime context, first-class object
model, prompt bridge, and direct-session capabilities instead of relying only
on the hub process environment. Use `mac-hermes runtime-proof
<hermes_instance_id> --skip-local-startup` only when a hub-only proof is
intended.

Fleet deployment now also writes a Hermes-visible runtime bootstrap contract:

- `~/.hermes/mac-runtime-context.json` (`mac.hermes.runtime_context.v1`)
- `~/.hermes/mac-runtime-context.md`
- `MAC_HERMES_INSTANCE_ID`, `MAC_WORKER_HERMES_INSTANCE_ID`, `MAC_AGENT_ID`,
  `MAC_HERMES_WORKSPACE`, `MAC_PROJECT_CONTRACT_FILE`, and `MAC_URL` in the
  Hermes environment

That contract is the runtime reminder that MAC owns tasks, projects,
dependencies, agent assignments, reviews, and publications while Hermes owns
soul, personality, private memory, and conversation state. Deployed workers are
registered against the same deterministic Hermes instance id, so
`mac-hermes work-context $MAC_HERMES_INSTANCE_ID` gives the agent the same
task/project graph the MAC API, CLI, and dashboard show.

The JSON context also carries a `first_class_objects` contract for `tasks`,
`projects`, and `agents`. Each object records MAC authority, the source of
truth, identity fields, API paths, MAC CLI and `mac-hermes` commands, dashboard
state keys, dashboard deep-link templates, and the runtime rule Hermes should
follow. Startup health fails a required runtime context when that object model
is missing or incomplete, so a deployed Hermes agent cannot silently regress to
treating tasks, projects, or agents as informal prompt text.

The same runtime context now carries a direct-session capability contract. A
Hermes session sees the MAC source workspace, the repository Beads contract,
the `bd prime` workflow, the `mac`, `mac-hermes`, and `hgmac` CLIs, Git status
and quality-gate commands, shell execution, writable workspace access, the
`mac-hermes-task-executor` oneshot worker path, and the hub Firecrawl web-search
affordance. Startup health and runtime proof reports treat those declarations
as part of the MAC/Hermes bridge rather than as tribal knowledge from an
operator shell. Startup proof also verifies the declared commands, workspace,
project contract, quality gate, Hermes oneshot executor, workspace file access,
and web-search environment are available in the Hermes runtime.

Deployment also patches Hermes' prompt builder to load
`mac-runtime-context.md` as a normal context source. That means gateway, CLI,
and oneshot Hermes sessions see the MAC task/project contract in their system
prompt, alongside `SOUL.md`, `AGENTS.md`, and other context files. Startup
health exposes this as `task_project_runtime.prompt_bridge` and
`task_project_runtime.markdown_contract`. It reports degraded readiness when
fleet deploy requires the contract but the files are missing, invalid, missing
the first-class task/project/agent prompt snippets, or not wired into the
Hermes prompt builder.

Hermes can then perform lifecycle operations through the adapter or CLI:

```python
adapter.list_tasks(state="open")
adapter.claim_task(task_id, agent_id)
adapter.claim_next_task(agent_id, dry_run=True)
adapter.start_task(task_id, agent_id)
adapter.add_evidence(task_id, "test", "artifact://pytest", "tests passed", agent_id)
adapter.record_command_audit(agent_id, phase="completed", argv=["git", "status"], cwd="/workspace/mac", task_id=task_id)
adapter.web_search("current project dependency release notes", limit=5)
adapter.web_scrape("https://example.com", formats=["markdown"])
adapter.submit_for_review(task_id, agent_id)
adapter.request_review(task_id, reviewer_agent_id)
adapter.publish_task(task_id, "git://main", reviewer_agent_id, evidence_id=evidence_id)
```

```bash
mac-hermes tasks --state open
mac-hermes claim <task_id> <agent_id>
mac-hermes claim-next <agent_id> --dry-run
mac-hermes start <task_id> <agent_id>
mac-hermes evidence <task_id> --kind test --uri artifact://pytest --summary "tests passed" --created-by <agent_id>
mac-hermes command-audit record <agent_id> --phase completed --argv-json '["git","status"]' --cwd /workspace/mac --task-id <task_id>
mac-hermes command-audit list --agent-id <agent_id> --task-id <task_id>
mac-hermes web-search "current project dependency release notes" --limit 5
mac-hermes web-scrape https://example.com --format markdown
mac-hermes web-crawl https://example.com --limit 1
mac-hermes submit-review <task_id> <agent_id>
mac-hermes request-review <task_id> <reviewer_agent_id>
mac-hermes publish <task_id> git://main <reviewer_agent_id> --evidence-id <evidence_id>
mac-agent --loop --executor ~/.mac/bin/mac-hermes-task-executor
```

## User Replies

Hermes can poll a durable task summary:

```python
reply = adapter.user_reply_for_task(task["id"])
```

The reply is intentionally concise so Hermes can render it in the personality
and tone of the active agent.

## Memory Write-Back

When a task completes, Hermes may write a result into its own memory:

```python
result = adapter.write_completed_task_to_memory(
    registration["hermes_instance"]["id"],
    task["id"],
    sink=lambda payload: hermes_memory_write(payload["memory_scope"], payload["content"]),
)
```

The `sink` is supplied by Hermes because Hermes owns memory storage. `mac`
records only an operational memory record that the write-back was prepared.

## Contract

The adapter must call `mac` APIs. It must not import `ControlPlane` or mutate
SQLite directly in production gateway code. Tests may use an in-process FastAPI
transport to verify the same API contract without opening a port.
