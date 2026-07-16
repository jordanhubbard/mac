# Disposition: skill environment-prerequisite finding — smallest repair applied

**Task**: task_46eb6c79305349e48c776893272de871 ("Produce disposition:
smallest repair or documented close"), plan node `disposition`.
**Parent task**: task_886a1a7002b94d7aa796a445a5f25a00
("Investigate low-confidence dream finding: skill").
**Depends on**: task_b6ddd8ec186645c98f83943d882aba19 (`skill_verification`),
whose record is `docs/investigation-task-b6ddd8-skill-env-prereqs.md`.
**Prepared by**: fleet worker (disposition node) in a task-owned worktree.

## Determination: PARTIALLY ACTIONABLE — one cosmetic doc line repaired

The parent "environment prerequisites" finding is **not actionable as a skill
defect**: the upstream verification (task_b6ddd8) refuted it, and the assessment
chain (`docs/dream-finding-6d1b5b.md`,
`docs/investigation-dreamrepair-5404b15-skill.md`,
`docs/closeout-dreamrepair-5404b15-skill.md`, `docs/prereq-task-fd2f34.md`)
independently closed it as low-confidence, single-record, generic label noise.

The verification did, however, confirm **one concrete, low-risk residual**: the
`skills/setup-mac-fleet/SKILL.md` Validation block invoked `uv run pytest …`,
but `uv` is not present in the environment and the repository contract's
bootstrap and contract test use `.venv`/`pip`/`python3` (no `uv` anywhere). Per
this task's acceptance criteria — "if a concrete, low-risk defect was confirmed,
make the smallest appropriate repair" — the single offending line was aligned to
the contract-native form. No skill *behavior*, module, or test was changed.

## Repair applied (smallest change)

In `skills/setup-mac-fleet/SKILL.md`, the Validation snippet line

```
uv run pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py
```

was changed to the form the repository contract already uses:

```
.venv/bin/python -m pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py
```

Rationale: `scripts/bootstrap-project.py` builds `.venv` via `pip install -e
.[dev]` and `scripts/run-contract-tests.sh` resolves its interpreter from
`.venv`; neither uses `uv`, and `uv` is not on PATH. The prior line would fail
`command not found: uv` for a reader who followed it verbatim. This is a
documentation-only correction to a runnable snippet; it introduces no new
dependency and does not alter deploy behavior.

## Verification

- The single `uv run` occurrence under `skills/` was the only one
  (`grep -rn "uv run" skills/`); after the edit there are none.
- The referenced tests exist and pass under the bootstrapped `.venv`
  (`.venv/bin/python -m pytest tests/test_deploy_agent_configs.py
  tests/test_hermes_startup.py`), so the corrected command is accurate.
- The canonical contract suite (`scripts/run-contract-tests.sh`) was run to
  confirm the doc edit does not regress the fleet-generic docs-identity guard
  (`test_docs_carry_no_operator_identity`) or any other check.

## Why the rest of the finding is not actionable (evidence gap)

- **Single, self-referential, low-confidence record.** The finding is a
  `failure_pattern`/`project`-scope classification at the 0.35 floor, backed by
  one evidence record whose only `skill`-area signal is the bare `\bskill[s]?\b`
  token. It names no failing assertion, reproducer, or offending skill asset.
- **Skills are otherwise accurate.** Every other environment prerequisite the
  two skills state is enforced by a repository entrypoint (`setup.sh` Python
  3.11+ gate, `scripts/bootstrap-project.py` required-commands check) or matches
  a present artifact (all referenced deploy scripts, sample fleet YAML, K8s
  manifests, and named tests exist).

## Follow-up plan / reopen criteria

- Treat the parent `skill` finding as closed. Re-file only against a specific
  skill component and only if stronger evidence appears: a failing skill/dream
  suite assertion on the current tree, a reproducer localized to a named
  `SKILL.md`/skill module, or at least two independent (non-self-referential)
  evidence records for the same component.
- If the underlying recurrence is instead the executor/sandbox preflight
  fallback seen upstream (`class=probe_failed`), file it against the
  executor/runtime-availability path with a tool/provider label rather than the
  bare `skill` token.

## Assumptions

- This is a `repo_change` task. The tracked deliverables are the one-line skill
  correction plus this note, recorded so the disposition is auditable from
  repository history.
- Canonical synchronization, final tests/CodeGraph, commits of tracked
  modifications, and publication are owned by the deterministic host finalizer;
  this note is self-contained and unaffected by upstream drift.
