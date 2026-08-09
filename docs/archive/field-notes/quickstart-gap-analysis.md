!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Quickstart Gap Analysis

**Document:** `docs/quickstart-gap-analysis.md`
**Baseline doc reviewed:** `docs/getting-started.md` (read-only; not modified)
**Audited against:** Nine requirements from parent task
`task_dda240df39784e738195329301d5cc98`
("Deliver a verified zero-to-first-coding quickstart for one-node and fleet MAC")
**Baseline test run:** 5254 passed, 21 skipped (scripts/run-contract-tests.sh)
**Date:** 2026-07-06

---

## Requirement 1 — One-node loop-mode topology and availability-aware review fallback

### What the doc says

`docs/getting-started.md` covers:

- "Try MAC On One Computer" (standalone `mac.db` mode, no API server running as
  an agent target)
- "What A Real Agent Does" (loop description, 9 steps, prose only)
- "How Reviewers Learn Repository Access" (fleet_learning memory, reviewer
  routing rules)

### Gaps identified

1. **No explicit one-node loop topology.** The doc never shows a single machine
   running both the MAC API server and a local worker agent in loop mode
   simultaneously.  There is no command sequence of the form:
   start API -> register agent -> start worker loop -> dispatch task -> observe
   claim.  The reader cannot follow a concrete single-machine walkthrough.

2. **No `mac agent register` / `mac agent start` commands.** The quickstart
   shows task creation and manual claim (`mac task claim`, `mac task start`)
   but does not show how an autonomous loop-mode worker is registered or
   launched.

3. **Review fallback path not demonstrated.** The "How Reviewers Learn
   Repository Access" section describes the `fleet_learning:repository_access`
   memory records and cooldown rules in prose, but does not show:
   - How to check a reviewer's current eligibility before assigning review.
   - What command or API call triggers the fallback when the preferred reviewer
     is on cooldown.
   - How the operator verifies which agent will be chosen for a review.

4. **Availability-aware review routing is implicit.** The doc says routing
   "uses the newest matching record" but gives no `mac` command to inspect
   current reviewer eligibility or simulate routing for a given repository
   host.

### Recommended additions

- A "One-node loop-mode" sub-section showing the complete start-API +
  register-agent + start-worker-loop + dispatch sequence on one machine.
- A concrete review-eligibility inspection example using
  `mac --json admin memory search --record-type fleet_learning:repository_access`.
- A note on what happens when zero eligible reviewers exist (task stays in
  `needs_review`; operator must add an eligible reviewer or repair credentials).

---

## Requirement 2 — Setup entry point: scripts/setup-fleet.py not executable, no one-node Make/CLI target

### What the doc says

`docs/getting-started.md` references `scripts/setup-fleet.py` in the "Deploy A
Real Fleet" section:

```
scripts/setup-fleet.py --list-samples
scripts/setup-fleet.py --init-from gke --name my-gke
```

The Makefile `setup` target delegates to `setup.py` (not `setup-fleet.py`).

### Gaps identified

1. **`scripts/setup-fleet.py` is not executable.** The file has mode
   `-rw-r--r--` (644), so running `scripts/setup-fleet.py --list-samples`
   directly fails with "Permission denied".  The user must prefix with
   `python3` or `uv run` — which the doc does not say.

2. **No Make target for one-node quickstart.** `make setup` runs fleet
   deployment (`setup.py`), not local quickstart initialization.  There is no
   `make quickstart` or equivalent that bootstraps a single-machine demo
   (API + worker) in one command.

3. **No `mac` CLI `setup` subcommand for one-node mode.** The CLI has
   `mac admin fleet validate` and `mac admin fleet doctor` for spec validation, but no
   `mac setup` or `mac quickstart` command that guides a user through the
   local first-run sequence (init DB, create secret key, start API, register
   agent).

4. **The doc's quickstart command sequence requires manual shell assembly.**
   Steps to reach a running local system span multiple sections (`mac --db
   mac.db init`, `export MAC_SECRET_KEY`, `uvicorn ...`, manual task creation)
   without a single entry-point command or script.

### Recommended additions

- `chmod +x scripts/setup-fleet.py` (one-line fix; file already exists).
- A `make quickstart` target that runs the full local demo sequence.
- Or document the `python3 scripts/setup-fleet.py` invocation form explicitly
  in the getting-started guide.
- A note clarifying that `make setup` is for fleet deployment, not local demo
  setup.

---

## Requirement 3 — MAC_ROUTER_PROVIDERS credential collection and validation step absent

### What the doc says

`docs/getting-started.md` mentions `MAC_SECRET_KEY` (required for DB init) and
`MAC_DEPLOY_GH_TOKEN` (for GitHub HTTPS repos), but does not mention
`MAC_ROUTER_PROVIDERS`.

The "Run The API And Dashboard" section shows:

```console
MAC_SECRET_KEY="$MAC_SECRET_KEY" MAC_DB="$PWD/mac.db" \
  uv run uvicorn mac.api:app --reload --port 8789
```

No LLM provider credential is set.

### Gaps identified

1. **`MAC_ROUTER_PROVIDERS` not mentioned in getting-started.** The in-MAC LLM
   router is required for workflow planning
   (`mac.api` raises `ValidationError: workflow planner requires configured
   MAC_ROUTER_PROVIDERS`) and for agent-driven task work.  A fresh user who
   follows the quickstart will have an API that cannot serve LLM-driven tasks.

2. **No credential collection step.** The doc does not explain:
   - What provider API keys are needed (OpenAI, NVIDIA NIM, Anthropic, etc.).
   - The `name=url,priority,key=secret:keyname` format for `MAC_ROUTER_PROVIDERS`.
   - Where to store provider keys (e.g., `~/.mac/.env`, host-local env).

3. **No validation step.** After setting `MAC_ROUTER_PROVIDERS`, the user has
   no guidance on how to verify the router is operational (e.g.,
   `mac admin diagnostics`, `curl /health`, or a router-status CLI command).

4. **Secret storage path not shown for local dev.** For fleet deployments the
   doc mentions `~/.mac/.env` for `MAC_DEPLOY_GH_TOKEN`; the same pattern
   applies to provider keys for local dev, but is not documented for that
   context.

### Recommended additions

- A "Configure LLM Providers" sub-section before "Run The API" that shows how
  to set `MAC_ROUTER_PROVIDERS` for at least one provider.
- A `mac router status` or equivalent health-check command (or note to use
  `mac admin diagnostics`).
- Example format: `export MAC_ROUTER_PROVIDERS="openai=https://api.openai.com/v1,1,key=MY_OPENAI_KEY"`.
- A note that without a configured router, agent-driven tasks will fail at the
  workflow-planning step.

---

## Requirement 4 — Repository onboarding still references manual /srv path without automation

### What the doc says

The "Tell Agents To Work On A Project" section shows:

```console
uv run mac --db mac.db bridge repository register my-project \
  /srv/repos/my-project --project my-project
```

### Gaps identified

1. **`/srv/repos/` is a bare path assumption.** The path `/srv/repos/my-project`
   is not created by any MAC command shown in the quickstart.  The user must
   manually clone the repository to that exact path before running `register`.
   No git clone or `project onboard` command is shown as the prerequisite.

2. **`project onboard` and `bridge repository register` relationship is
   unclear.** The doc shows both `project onboard` (which creates a
   contract-authoring task) and `bridge repository register` (which reads
   `.mac/project.yaml`) but does not explain the sequencing: onboard first,
   wait for task completion, then register the checkout.

3. **No automation for the checkout path.** There is no `mac bridge repository
   clone` or helper that creates the `/srv/repos/` tree and clones into it.
   The operator must know to do this manually and choose the path themselves.

4. **Path convention is unexplained.** `/srv/repos/` is a Linux FHS convention
   but is arbitrary; the doc does not say what constraints apply to the path
   (absolute, accessible by the MAC process user, etc.).

### Recommended additions

- Replace the `/srv/repos/my-project` example with a user-relative path (e.g.,
  `~/repos/my-project`) or add a `git clone` step before `register`.
- Clarify the onboard -> task completion -> register sequence.
- Note that the path must be readable by the process running the MAC API.
- Consider a helper: `mac admin bridge repository clone <url> <local-path>` that
  clones and registers in one step.

---

## Requirement 5 — No end-to-end first coding task walkthrough with evidence commands

### What the doc says

The doc covers:
- Creating a practice project and task.
- Manually claiming and starting a task.
- "What A Real Agent Does" (prose list of 9 steps).
- Splitting large tasks.

### Gaps identified

1. **No create -> dispatch -> observe -> review -> publish -> close sequence.**
   The quickstart never shows a complete task lifecycle with real evidence.
   Steps 6-9 of the "What A Real Agent Does" loop (record evidence, ask for
   review, publish, complete) are described in prose but never demonstrated
   with commands.

2. **No evidence submission example.** The doc does not show how to attach
   evidence to a task (e.g., `mac task evidence` or the API `POST
   /tasks/{id}/evidence`).

3. **No review request example.** There is no command shown for transitioning a
   task to `needs_review` state or for a reviewer to submit a verdict.

4. **No publication step example.** The doc does not show `mac task publish` or
   equivalent to mark work as accepted and merged.

5. **No task close example.** The doc does not show `mac task close` with a
   reason, even though the AGENTS.md quick reference lists it.

6. **No observe step.** There is no `mac task show` or `mac task history`
   command shown to watch a task's lifecycle as it progresses.

### Recommended additions

- A "First Coding Task End-to-End" section with concrete commands for each
  lifecycle phase: create, dispatch (`mac admin dispatch tick`), observe
  (`mac task show`, `mac task history`), submit evidence, request review,
  approve review, publish, close.
- Example evidence payload for a simple docs task.
- Expected output at each step so the reader knows what success looks like.

---

## Requirement 6 — No explicit readiness checks

### What the doc says

The "Connect A New Client Today" section shows `mac admin diagnostics`, `mac task
stats`, and `mac agent list` as post-login verification.  The "Run The API"
section shows how to start the API but gives no health check.

### Gaps identified

1. **No API health check.** There is no `GET /health` or `mac admin diagnostics`
   example shown immediately after starting the API to confirm it is up.

2. **No worker registration verification.** The doc does not show how to confirm
   a worker agent is registered and its heartbeat is current
   (e.g., `mac agent show agent_...` or `mac agent list --healthy`).

3. **No dispatch eligibility check.** There is no command shown to verify that
   an open task will actually be picked up by the dispatcher on the next tick
   (e.g., `mac task ready --agent agent_...`).

4. **No repository access check.** Before assigning a review, no command is
   shown to verify that the reviewer agent can clone the repository
   (distinct from the `fleet_learning` memory check described elsewhere).

5. **No sandbox/tool contract check.** The doc does not mention how to confirm
   that the agent's sandbox has the required toolchain commands available
   (python3, git, gh) before dispatching repository-backed tasks.

6. **No model route check.** There is no step shown to verify that
   `MAC_ROUTER_PROVIDERS` is correctly resolving a model route before
   dispatching LLM-driven tasks.

7. **No review eligibility/fallback check.** The doc does not show a command
   to list agents currently eligible to review a specific project/repo, or
   to confirm fallback coverage exists.

### Recommended additions

- A "Readiness Checklist" section covering: API health, worker heartbeat,
  dispatch eligibility for a test task, repo access, sandbox toolchain,
  model route, and review eligibility.
- Concrete commands or `mac admin diagnostics` output annotation for each check.
- A note that all seven checks should pass before sending a real coding task
  to the fleet.

---

## Requirement 7 — No separate path for adding more agents and an independent reviewer

### What the doc says

The doc mentions loop-mode agents claiming from `mac task ready` and
`mac task claim`/`mac task start` for manual assignment.  The "How Reviewers
Learn Repository Access" section explains reviewer routing.

### Gaps identified

1. **No "add a second agent" walkthrough.** After the single-machine demo, there
   is no section showing how to add a second agent (on the same or a different
   machine) and register it with the hub.

2. **No independent reviewer setup.** The review routing section assumes
   reviewers already exist; it does not show how to provision a new agent
   specifically as a reviewer, or how `require_distinct_agent=true` is
   satisfied when the fleet has only one or two agents.

3. **No `mac agent register` or equivalent shown.** The onboarding path for a
   new worker agent (register, configure capabilities, verify heartbeat) is
   absent from the quickstart.

4. **No capability assignment shown.** The doc does not show how to assign
   capabilities (e.g., `python`, `docs`, `review`) to an agent so it matches
   `required_capabilities` filters on tasks.

5. **Reviewer vs. worker agent distinction not explained.** The doc describes
   reviewer routing as if all agents are interchangeable; it does not explain
   how the `require_distinct_agent` coordination constraint works in practice
   or how to ensure a separate reviewer is available.

### Recommended additions

- A "Scale Up: Adding a Second Agent" sub-section covering agent registration,
  capability assignment, heartbeat verification, and dispatcher tick to assign
  work.
- A "Reviewer Setup" sub-section showing how to designate a reviewer-capable
  agent and verify `require_distinct_agent` satisfaction.
- Example `mac agent list --capabilities` or equivalent command.

---

## Requirement 8 — No automated documentation or CLI smoke coverage for quickstart command shapes

### What the doc says

The Makefile has a `cli-coverage` target (`scripts/cli-coverage.py`) and a
`test-cli` target.  The doc does not reference these.

### Gaps identified

1. **No quickstart smoke test.** There is no test or script that runs the
   command shapes shown in `docs/getting-started.md` and verifies they exit
   successfully (with a test DB).

2. **`scripts/cli-coverage.py` not documented.** The CLI coverage script
   exists but is not referenced in getting-started or in a testing guide.

3. **Documentation is not contract-tested.** Commands in `docs/getting-started.md`
   can drift from the CLI implementation without any automated detection.
   For example, if a subcommand is renamed, the doc would silently become
   wrong.

4. **No `--help` output verification.** There is no test asserting that every
   command shape shown in the quickstart is present in `mac --help` output.

5. **`make cli-coverage` output not shown to users.** The CI/developer
   workflow does not include a step that prints CLI coverage gaps to the
   operator so they know which commands are exercised by tests.

### Recommended additions

- A `tests/test_quickstart_commands.py` or equivalent that runs each command
  shape from the getting-started guide against a temp DB and asserts exit
  code 0.
- A CI step or `make` target that runs `scripts/cli-coverage.py` and fails if
  any command shown in getting-started.md is absent from the CLI.
- A note in `docs/getting-started.md` pointing to `make cli-coverage` for
  operators who want to verify CLI shape.

---

## Requirement 9 — No verified evidence of reaching a completed first code task

### What the doc says

The "What A Real Agent Does" section lists the 9-step loop in prose.  The
"Split A Large Task" section shows how to add child tasks via the API.

### Gaps identified

1. **No worked example with real output.** The quickstart contains no
   transcript, screenshot reference, or evidence payload showing a task that
   was actually completed by an agent with passing tests, a review verdict,
   and a publication record.

2. **No `mac task show` output example.** The doc does not show what a
   completed task looks like in `mac task show` output (state, evidence,
   completed_at, review verdict, publication ref).

3. **No evidence format shown.** The doc does not show what a
   `mac.worker_evidence.v1` or `repo_change` evidence payload looks like
   so operators can verify an agent produced correct evidence.

4. **No review verdict example.** There is no sample `review_verdict` evidence
   shown to indicate what a passing or failing review looks like.

5. **Completion state not demonstrated.** The doc does not show `mac task close`
   or the transition from `needs_review` -> `review_passed` ->
   `published` -> `closed` with real command output.

6. **No "this is what done looks like" anchor.** Without a worked example, a
   new operator cannot distinguish a correctly completed task from a stalled
   or incorrectly closed one.

### Recommended additions

- A "Verified Completion" section or appendix with annotated `mac task show`
  output for a completed repository-backed task, including:
  - Worker evidence (tests passed, files changed, pushed ref).
  - Review verdict (reviewer agent, outcome, timestamp).
  - Publication record (merged ref or operator confirmation).
  - Final task state (`closed`, `completed_at`).
- A reference to `mac task stats` output that shows one completed task as
  the "success signal" for the quickstart.

---

## Summary Table

| # | Requirement | Current State | Primary Gap |
|---|-------------|---------------|-------------|
| 1 | One-node loop-mode + review fallback | Prose only | No runnable loop-mode sequence; no eligibility commands |
| 2 | Setup entry point | `setup-fleet.py` not +x; no quickstart Make/CLI target | Permission denied on direct invocation; no single entry point |
| 3 | MAC_ROUTER_PROVIDERS credential collection | Not mentioned | No provider setup step; API starts without LLM routes |
| 4 | Repository onboarding / /srv assumption | Manual /srv path shown | No automation; sequencing of onboard->register unclear |
| 5 | End-to-end first coding task | Manual claim only | No evidence, review, publish, or close commands shown |
| 6 | Readiness checks | Partial (diagnostics post-login) | No pre-dispatch checklist for 7 readiness dimensions |
| 7 | Adding more agents + independent reviewer | Routing rules described | No agent registration, capability, or reviewer setup walkthrough |
| 8 | Automated doc/CLI smoke coverage | cli-coverage.py exists but undocumented | No quickstart command shape tests; docs can drift silently |
| 9 | Verified evidence of completed first task | Prose loop description only | No worked example with real output, evidence, or completion state |

All nine requirements have identified gaps. None are blocking bugs in the
existing code; all are documentation and workflow coverage deficits that
prevent a new operator from confidently reaching a completed first coding task
on a one-node or small fleet deployment.
