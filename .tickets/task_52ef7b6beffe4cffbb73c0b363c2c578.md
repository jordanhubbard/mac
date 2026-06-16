---
id: task_52ef7b6beffe4cffbb73c0b363c2c578
status: open
deps: []
links: []
created: 2026-06-14T07:48:39.293390+00:00
type: task
priority: 0
mac-task-id: task_52ef7b6beffe4cffbb73c0b363c2c578
---
# OpenShell: worktree upload/download sync for sandboxed task execution

Sandboxed task execution needs a worktree sync layer.

Discovered validating OpenShell enforcement on rocky: include_workdir is a
Landlock grant only, not a file copy. OpenShell sandboxes are containers with
no bind-mount, so the one-shot `openshell sandbox create -- <argv>` model used
by task_executor._maybe_wrap_openshell leaves the sandbox cwd (/sandbox) EMPTY
(the task git worktree is never uploaded) and any edits the agent makes never
sync back to the host. A real coding task therefore cannot work sandboxed: the
agent has no repo to operate on, and its output (including
$MAC_TASK_WORKSPACE/mac-evidence.json) is trapped in the sandbox and lost on
teardown.

Proven working already (PR #154 + rocky validation): sandbox confinement
(FS read/write + deny-by-default egress), the in-image hermes runtime, gateway
reachability+auth, and a full LLM round-trip (READY_SANDBOX_OK). Only the
worktree I/O round-trip is missing.

Fix: rework the sandbox wrap from one-shot `create -- argv` to an orchestrated
lifecycle:
  1. openshell sandbox create --name <task> --from <image> --policy <P> <env>  (keep alive)
  2. openshell sandbox upload <task> <host-worktree> /sandbox
  3. openshell sandbox upload <task> <rewritten-hermes-config> /tmp/.hermes/config.yaml
  4. openshell sandbox exec <task> -- /opt/mac-venv/bin/python -m hermes_cli.main chat ...
  5. openshell sandbox download <task> /sandbox <host-worktree>   (results + mac-evidence.json)
  6. openshell sandbox delete <task>                               (teardown, also on failure)
Needs: robust teardown on error, timeout handling, only-download-changed,
and tests. Then re-validate a real coding task on rocky before fail-closing,
then GPU hosts (also validate --gpu passthrough).
