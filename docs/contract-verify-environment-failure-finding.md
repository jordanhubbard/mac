# Contract-verify environment failure investigation finding

This note records the investigation outcome for the repair incident that opened
against the `mac` repository contract-verify gate. It is the durable,
product-tracked artifact for that investigation; the per-run executor/worker
diagnostics live in the task workspace and are intentionally kept out of git
(see `src/mac/investigation_artifacts.py`).

## Scope

A contract-verify run is expected to reproduce the declared repository contract
on the assigned agent: bootstrap the hermetic environment, then run the
canonical test gate. The reproduction commands are:

```text
python3 scripts/bootstrap-project.py
scripts/run-contract-tests.sh
```

`scripts/bootstrap-project.py` builds the self-healing hermetic `.venv`
(`pip install -e .[dev]`, producing `.venv/bin/python`, `.venv/bin/pytest`, and
`.venv/bin/coverage`), and `scripts/run-contract-tests.sh` is the canonical
verification gate declared in `.mac/project.yaml`. This investigation examined
whether the observed contract-verify failure maps to an actionable defect in
the repository/source or is scoped to the host/agent that ran it.

## Observed failure

The contract-verify attempt did not reach or complete the repository test gate.
The failure surfaced during the sandbox preflight stage, before the repository
bootstrap and test contract could be exercised on their own terms:

- Failure class: `environment` (host/agent-scoped), not `source`.
- Trigger: the coding-agent `claude` sandbox preflight probe failed on the
  assigned agent. The probe is a host/agent-scoped readiness check that runs
  ahead of the repository contract; its non-zero exit aborts the attempt before
  the declared bootstrap/test commands can establish their own result.
- Because the abort happens in host preflight, the observed non-zero exit is a
  host-preflight exit and is not attributable to
  `scripts/bootstrap-project.py` or `scripts/run-contract-tests.sh`.

## Confirmed root cause

The confirmed root cause is a **host/agent-scoped coding-agent `claude` sandbox
preflight probe failure** on the single affected agent. It is a **host defect**
(`failure_class=environment`), not a repository or source defect. The probe
failure is local to that agent's sandbox/coding-CLI provisioning and does not
reproduce from the repository contract itself.

## Repository contract is intact

The declared repository contract is intact and was confirmed by re-reading it
against the current tree:

- `.mac/project.yaml` declares the bootstrap command
  (`python3 scripts/bootstrap-project.py`) and the canonical test command
  (`scripts/run-contract-tests.sh`).
- `scripts/bootstrap-project.py` remains a self-healing hermetic bootstrap: it
  rebuilds `.venv` when the expected interpreter is missing and installs the
  project with its dev extras, producing the declared `.venv/bin/python`,
  `.venv/bin/pytest`, and `.venv/bin/coverage` outputs.
- `scripts/run-contract-tests.sh` remains the canonical, hermetic gate and
  passes on an unaffected agent (see the verification recorded with this
  finding's task evidence).

No repository/source change is warranted by this incident; the defect is
external to the tree.

## Excluded agent

The affected agent's coding-agent `claude` sandbox preflight is the failing
component. That originating agent is excluded from re-verification of this
contract; the incident is scoped to that host and must not gate the repository.
Personal usernames, hosts, and tokens are intentionally kept out of this note;
the agent is referenced by its fleet-generic role/exclusion only.

## Recommended remediation

- Re-verify the contract on an unaffected/distinct agent whose coding-agent
  `claude` sandbox preflight is healthy; the repository contract itself needs no
  change.
- Enforce `require_distinct_agent=true` for the re-verification so the excluded,
  preflight-failing agent cannot re-run the same host-scoped defect.
- If the same host-preflight failure recurs on additional agents, escalate the
  coding-CLI/sandbox provisioning defect through host remediation rather than
  reopening a repository repair.

## Disposition

- Classification: `environment` (host/agent-scoped) preflight failure.
- Code change: none in the repository/source tree.
- Follow-up: verify on a distinct, unaffected agent with
  `require_distinct_agent=true`; remediate the affected agent's coding-agent
  `claude` sandbox preflight at the host level.

## References

- `.mac/project.yaml` — repository contract: bootstrap and canonical test
  commands.
- `scripts/bootstrap-project.py` — self-healing hermetic `.venv` bootstrap.
- `scripts/run-contract-tests.sh` — canonical contract-test gate.
- `docs/crash-diagnosis-and-repair.md` — evidence lifecycle and failure-class
  handling conventions.
- `docs/crash-incident-finding.md` — companion investigation-finding note whose
  structure this note follows.
- `src/mac/investigation_artifacts.py` — why per-run investigation diagnostics
  are not committed to the tree.
