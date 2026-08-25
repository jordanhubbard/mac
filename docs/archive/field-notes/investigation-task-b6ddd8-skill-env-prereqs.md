!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: skills environment-prerequisite behavior vs. the finding

**Task**: Concretely verify whether the mac skills
(`skills/setup-mac-fleet/SKILL.md`, `skills/mac-agent-terminal-timeout/SKILL.md`,
and related environment-prerequisite/bootstrap guidance) actually exhibit the
"environment prerequisites" failure pattern the parent finding suggests.
Cross-check the two skills against the repository-contract bootstrap
(`scripts/bootstrap-project.py`) and contract tests
(`scripts/run-contract-tests.sh`). Investigation only — no speculative edits.

**Investigated by**: fleet worker (read-only skill/contract inspection plus
targeted test runs in a task-owned worktree).

## Status: LARGELY REFUTED — no concrete environment-prerequisite defect

Both skills state their environment prerequisites accurately and consistently
with the repository contract. Every environment prerequisite the two skills name
is either enforced by a repository entrypoint or matched by a real, present
artifact. The only concrete residual is a minor documentation inconsistency in
one Validation snippet (a `uv run pytest` line), which does not match the
finding's "missing/incorrect environment prerequisites" pattern and is recorded
below without editing per the no-speculative-edit constraint.

## Ground truth observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.13; `git` and `gh` present; pytest 8.4.2 / coverage 7.15.2 /
project deps installed). All skill and contract sources were read, not modified.

### Per-skill pass/fail against the finding

| Skill | Environment-prerequisite claim | Verified against | Result |
| --- | --- | --- | --- |
| `skills/mac-agent-terminal-timeout/SKILL.md` | Long operations (`scripts/run-contract-tests.sh`, `python3 scripts/bootstrap-project.py`) need an explicit high `timeout`; default is 180s | Names real repo entrypoints; timeout table is advisory guidance, not a false prerequisite | PASS (accurate) |
| `skills/mac-agent-terminal-timeout/SKILL.md` | Cargo lives outside `MAC_SANDBOX_BASE_PATH`; declare `cargo`/`rustc`/`rustup` in `toolchain.required_commands` so `mac_sandbox_toolchain_setup` symlinks it | Describes toolchain provisioning; consistent with contract's `toolchain.required_commands` mechanism (this repo declares only `python3`, `git`, `gh`, so no Rust step is expected) | PASS (accurate) |
| `skills/mac-agent-terminal-timeout/SKILL.md` | `test_docs_carry_no_operator_identity` enforces generic docs/skills | `tests/test_docs_no_operator_identity.py` defines `test_docs_carry_no_operator_identity` and passes | PASS (accurate) |
| `skills/setup-mac-fleet/SKILL.md` | Deploy prerequisite: run `setup.sh` wizard; Python 3.11+ implied | `setup.sh` `find_python()` enforces `sys.version_info >= (3, 11)` and errors "Python 3.11+ is required" | PASS (enforced) |
| `skills/setup-mac-fleet/SKILL.md` | Validation: `bash -n` the four deploy scripts | `deploy/deploy-mac-fleet.sh`, `deploy/install-qdrant-service.sh`, `deploy/install-tailscale.sh`, `deploy/install-headscale.sh` all present and pass `bash -n` | PASS (verified) |
| `skills/setup-mac-fleet/SKILL.md` | Validation: `uv run pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py` | Both test files exist and pass; but `uv` is NOT on PATH and neither the contract test nor bootstrap use `uv` (they use `.venv`/`pip`/`python3`) | PARTIAL — doc-only inconsistency, not an environment-prerequisite defect |

### Repository-contract cross-check

- `.mac/project.yaml` (`mac.repository_contract.v1`): `toolchain.required_commands`
  = `python3`, `git`, `gh`; `bootstrap.command` = `python3 scripts/bootstrap-project.py`;
  `test.command` = `scripts/run-contract-tests.sh`.
- `scripts/bootstrap-project.py` explicitly enforces the environment
  prerequisites the skills rely on: it hard-fails when `python3` is below 3.11
  ("Python 3.11+ is required to bootstrap mac"), checks `REQUIRED_COMMANDS`
  (`python3`, `git`, `gh`; `--venv-only` relaxes to just `python3`), then builds
  `.venv` with `pip install -e .[dev]`. No `uv` usage anywhere.
- `scripts/run-contract-tests.sh` resolves an interpreter from `.venv`,
  `/opt/mac-venv`, or PATH `python3`; if the interpreter cannot run the suite and
  `.venv` is absent, it self-bootstraps via
  `scripts/bootstrap-project.py --venv-only`. It clears leaking `MAC_/HERMES_/…`
  and git-forge/provider env vars for hermetic runs. No `uv` usage anywhere.

### Skill-referenced tests: all green

Run via `.venv/bin/python -m pytest`:

| Test file | Result |
| --- | --- |
| `tests/test_deploy_agent_configs.py` | passed |
| `tests/test_hermes_startup.py` | passed |
| `tests/test_docs_no_operator_identity.py` | passed |
| **Total** | **120 passed, 0 failed** |

## Concrete conclusions tying each skill to the finding

1. `skills/mac-agent-terminal-timeout/SKILL.md` — REFUTED. Its environment
   prerequisites (timeout budgets, Rust/`cargo` provisioning, the docs-identity
   guard) are accurate and map to real repository behavior and passing tests.
   No missing or incorrect environment prerequisite.
2. `skills/setup-mac-fleet/SKILL.md` — REFUTED as an environment-prerequisite
   defect. Its core prerequisite (Python 3.11+, run the wizard, then the deploy
   commands) is enforced by `setup.sh` and `scripts/bootstrap-project.py`, and
   its deploy-script `bash -n` validation checks pass.
3. Residual (doc inconsistency, NOT the finding's pattern): the setup skill's
   Validation block uses `uv run pytest …`, but the repository contract's
   bootstrap and contract test use `.venv`/`pip`/`python3` and `uv` is absent
   from the environment. This is a tool-name mismatch in a validation snippet,
   not a genuine unstated/incorrect environment prerequisite. Recorded without
   editing per the no-speculative-edit constraint; if remediated later, aligning
   that line to `.venv/bin/python -m pytest …` (as the contract already does)
   would resolve it.

**Overall**: The alleged "environment prerequisites" failure pattern is not
present as a real skill defect. The finding is refuted; the only actionable item
is an optional cosmetic doc fix to one Validation command.
