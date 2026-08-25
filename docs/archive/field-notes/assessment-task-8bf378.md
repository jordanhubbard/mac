!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Assessment: task_8bf37845abf445149d99fb4a1e3a41d5

**Task**: Triage the low-confidence dream-cycle repair finding
`dreamrepair:bf68720937810742ef53e6bb00fcffd5` (kind `failure_pattern`, scope
project, confidence low) and determine whether it is actionable.
**Affected label**: `Skills:'skill'` only (no tools, providers, or repo areas).
**Supporting evidence**: single record `mem_e72fd2c610b744a79dae0d4ecca3c751`
(`deployment_learning:mac`) derived from origin
`task_19a56b79684e4a66889c8069c9deca5d`, whose `failure_class` is `environment`
and whose summary is "Repair environment prerequisites: audit skill modules and
tests against contract tests" and which FAILED at the
execution-environment/prerequisite stage.
**Repo areas grounded in**: `src/mac/skill_auto_repair.py`, skill docs under
`src/mac/_hermes/skills/` and `skills/`, and the skill contract tests
`tests/test_skill_auto_repair.py`, `tests/test_fleet_skills.py`,
`tests/test_hermes_skills_dedup.py`.
**Assessment date**: 2026-07-16
**Assessed by**: fleet worker (investigation only; no skill or tool edits, per
task scope).

## Status: NOT ACTIONABLE as a skill defect — this is case (b): an
environment-prerequisite failure of the origin task that produced a spurious
low-confidence `skill` `failure_pattern` with a real evidence gap

The skill surface named by the finding is healthy: the code imports and runs,
and all skill-related contract tests pass. The single supporting evidence record
does not describe any skill code/test defect — it records an `environment`
failure_class from an origin task that stopped at the prerequisite stage. The
`skill` label is an artifact of the origin task's *title* ("audit skill modules
and tests"), not of any observed skill fault. The follow-on task should NOT
patch skill code/tests; the concrete gap is evidentiary.

## Ground Truth Observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.13; `git`/`gh` present; pytest/coverage installed by
`python3 scripts/bootstrap-project.py`). All skill files were read, not
modified.

### 1. The skill module imports and is fully wired

- `src/mac/skill_auto_repair.py:1` — module docstring describes the guarded
  high-confidence staging path (allowlist, evidence gate, secret scan, identity
  scan, auditable summary).
- `src/mac/skill_auto_repair.py:229` — `def stage_skill_patch(...)` public API.
- `src/mac/skill_auto_repair.py:345` — `def stage_skill_patches(...)` batch API.
- Import smoke test succeeds: `import mac.skill_auto_repair` returns schema
  `mac.skill_auto_repair.v1`. No import-time or dependency error.

### 2. All skill-related contract tests pass

Ran the three skill-scoped test modules named by the task:

- `tests/test_skill_auto_repair.py` — 30 tests (schema, allowlist/traversal
  guards, evidence gate, secret + identity scrubbing, write/dry-run, batch).
- `tests/test_fleet_skills.py` — 2 tests (fleet-wide multimodal skill present
  and installed for every agent).
- `tests/test_hermes_skills_dedup.py` — 14 tests (skill dedup/archival).

Result: `46 passed`. Re-run confirmed the same result — no flakiness, no
failure, no revealed defect in the skill surface.

### 3. Skill docs referenced by the affected label are present

- `src/mac/_hermes/skills/**/SKILL.md` — 90 files.
- `skills/**/SKILL.md` — 2 files.
- `deploy/skills/**/SKILL.md` — 1 file.

The affected label `Skills:'skill'` corresponds to real, populated skill assets;
none is missing or malformed per the passing tests above.

### 4. The origin failure is an environment/prerequisite failure, not a skill fault

The supporting evidence record's `failure_class` is `environment`, and its
summary is framed as *repairing environment prerequisites*. The contract-test
harness itself documents exactly this failure mode: the runner
`scripts/run-contract-tests.sh` unsets fleet/provider/token environment and
redirects `HOME`/`XDG` because leaked worker-host environment (live
`GH_TOKEN`/`GITHUB_TOKEN`, `OPENAI_BASE_URL`/API keys, real `~/.hermes` and
`~/.mac` config) has repeatedly caused tests to fail on fleet worker hosts while
passing on tokenless dev machines and hub sandboxes:

- `scripts/run-contract-tests.sh:1` — hermetic-environment preamble that
  `unset`s `GH_TOKEN`/`GITHUB_TOKEN`/`GITEA_TOKEN`, `OPENAI_*`/`ANTHROPIC_*`
  route knobs, and redirects `HOME`/`XDG_CONFIG_HOME` to a throwaway dir.

An origin task that failed at the execution-environment/prerequisite stage
(before or during bootstrap/test setup) would not have exercised the skill code
at all. Its `environment` failure_class was mislabeled downstream as a
project-scope `skill` `failure_pattern` because the origin task's *title*
mentioned skill modules — producing the low-confidence finding under review.

## Actionability Verdict

- **Is the finding actionable as a skill code/test defect?** No.
- **Case (a) genuine skill defect?** Ruled out: the module imports and all 46
  skill contract tests pass; no defect is revealed.
- **Case (b) environment-prerequisite failure with a spurious `skill`
  label + evidence gap?** Confirmed. The lone supporting evidence is an
  `environment` failure_class from an origin task that stopped at the
  prerequisite stage; it contains no evidence of a skill code/test fault.

## Concrete Evidence Gap (for the follow-on task to act on)

The finding lacks any evidence that ties a failure to skill code or tests. To be
actionable it would need at least one of:

1. A reproducing skill-test failure captured under the hermetic harness
   (`scripts/run-contract-tests.sh`) — currently the skill suite is green.
2. An origin failure record whose `failure_class` is a code/test class (not
   `environment`) and whose excerpt points at a specific skill module/test.
3. A specific malformed or missing skill asset under `skills/`,
   `deploy/skills/`, or `src/mac/_hermes/skills/` — none observed.

Recommended disposition for the parent/follow-on task: reclassify the finding as
an origin **environment-prerequisite** failure (not a skill defect) and close the
skill `failure_pattern` as non-actionable / evidence-gap, rather than opening a
skill code or test change.

## Scope Note

Per task instructions, no skill or tool files were modified. The only worktree
change is this new assessment document.
