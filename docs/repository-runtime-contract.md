# Repository Runtime Contract

Any repository registered with mac must declare how an agent prepares and
verifies that repository on a fresh host. The contract is intentionally
project-owned: mac should not guess that every worker has the same shell state,
package manager, Linux distribution, macOS setup, or WSL2 image.

The contract file lives at `.mac/project.yaml` in the repository root.

```yaml
schema: mac.repository_contract.v1
project: mac
platforms:
  - darwin
  - linux
  - wsl2
toolchain:
  required_commands:
    - python3
    - git
    - gh
bootstrap:
  command: python3 scripts/bootstrap-project.py
  creates:
    - .venv/bin/python
test:
  command: PATH=.venv/bin:$PATH .venv/bin/python -m pytest
evidence:
  required:
    - repo.head_sha
    - repo.pushed
    - repo.dirty
    - repo.files_changed
    - tests
```

## Required Fields

- `schema`: must be `mac.repository_contract.v1`.
- `project`: must match the mac project name used when the repository is
  registered.
- `platforms`: explicit supported host families. Use broad families such as
  `darwin`, `linux`, and `wsl2`; document narrower distro assumptions inside
  the bootstrap script instead of assuming Ubuntu.
- `toolchain.required_commands`: commands that must exist before bootstrap can
  run. Keep this list small and portable. mac fleet deploy installs baseline
  worker tools such as `gh` and `codegraph`; project bootstrap scripts should
  fail loudly when a required command is still missing.
- `bootstrap.command`: an idempotent command run from the repository root to
  create the local build/test environment.
- `bootstrap.creates`: relative paths expected after bootstrap. These are used
  as a quick signal that a host has already been prepared.
- `test.command`: the canonical verification command for default task work.
- `evidence.required`: manifest fields a worker must include before mac can
  consider repo work publishable.

## Enforcement

The project repository registry validates this file during repository
registration. Registration fails if the contract is missing, malformed, or names
a different `project` than the registered mac project. Repository onboarding is
the pre-registration task that produces the first draft of this file.

When the registering host has CodeGraph installed, registration also runs
`codegraph init` in the registered checkout. Before doing so, mac writes
`.codegraph/` to that checkout's local `.git/info/exclude`, so CodeGraph's
generated index does not become visible repo state or a commit candidate.

CodeGraph is a legitimate baseline runtime assumption for deployed agent
analysis. Fleet deploy installs `codegraph`, runs `codegraph install`, and fails
the deploy if CodeGraph cannot be prepared; the OpenShell agent image does the
same at image build time. Agents may use it to understand repository APIs, code
behavior, call relationships, and skills that benefit from code structure. It is
analysis support: repository contracts, source files, tests, and recorded
evidence remain authoritative.

For source, build, dependency, or runtime config changes, CodeGraph is also an
enforced evidence gate. Worker-owned pushes and approved review verdicts must
carry a passing `mac.codegraph_audit.v1` object produced from `codegraph init`
or `codegraph sync` plus `codegraph affected` for the changed files. The worker
and control-plane validators reject source/build change evidence that lacks the
audit or records a failed audit. Pure documentation/media/text-only changes may
skip the audit with `reason=non_code_change`.

Repository-backed tasks carry the normalized contract in
`task.metadata.origin.repository_contract`. The Hermes executor prompt surfaces
the contract and tells workers to bootstrap from the local checkout before
running the declared test command.

Direct tasks created through the task CRUD API are normalized too. If their
`project` matches an enabled registered repository, mac attaches the same
repository contract to `task.metadata.origin.repository_contract` and records a
strong `task.metadata.execution_contract`. If a project record advertises
`metadata.repository_url` but no enabled repository contract is registered,
normal task creation fails closed; only the `origin.onboarding=true` contract
authoring task is allowed through. If no repository applies and the project does
not advertise a repository URL, mac records a weak `operator_directive`
execution contract and emits `task.execution_contract.weak` telemetry so
under-specified non-repository work is visible before an agent tries to execute
it.

## Worker Checkout Rules

Repository work is not performed directly in the registered source checkout.
The worker prepares a task-owned git worktree and passes its path through
`MAC_TASK_REPO_WORKTREE` and `metadata.runtime.repository_worktree`. Agents must
commit, test, and publish from that task worktree, then report the pushed ref or
PR URL in `mac.worker_evidence.v1` evidence.

The registered repository path remains the durable project source used to derive
worktrees and to identify the runtime contract. It should stay clean in normal
operation. If mac detects dirty registered source state that affects worktree
preparation, it creates a source-remediation task for the agent that owns that
environment; ordinary feature or bug tasks should not edit the registered
checkout directly.

## mac As First Adopter

mac declares its own contract in `.mac/project.yaml`. Its bootstrap command is:

```console
python3 scripts/bootstrap-project.py
```

That script first verifies `python3`, `git`, and `gh`, then creates
`.venv` and installs the dev extra so a fresh macOS, Linux, or WSL2 agent can run:

```console
PATH=.venv/bin:$PATH .venv/bin/python -m pytest
```
