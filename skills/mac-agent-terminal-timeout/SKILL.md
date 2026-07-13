---
name: mac-agent-terminal-timeout
description: >
  Use when a MAC fleet agent task fails with a terminal:timeout tool_error, or
  when running long-lived MAC-repo operations (contract tests, bootstrap,
  codegraph init/sync, large git operations) to avoid timing out mid-task.
version: 1.0.0
platforms: [linux, macos, wsl2]
metadata:
  hermes:
    tags: [mac, fleet, timeout, terminal, contract-tests, reliability]
    related_skills: [setup-mac-fleet]
---

# MAC Agent Terminal Timeout

## When to use

Apply this skill when:

- A task report shows `tool_error` with signature `terminal:timeout`.
- You are about to call `terminal()` for any of the following in a MAC
  repo worktree:
  - `scripts/run-contract-tests.sh` — the full suite can take 4-5 minutes.
  - `python3 scripts/bootstrap-project.py` — downloads and installs deps.
  - `codegraph init` or `codegraph sync` — indexes the full call graph.
  - `git clone` of a large repository.
  - Any `pip install`, `uv sync`, or heavy build step.

## Root cause

The Hermes `terminal()` tool defaults to a 180-second timeout. MAC contract
tests consistently run for 4-5 minutes (260+ seconds observed on fleet
workers). With the default timeout, the terminal call is cancelled mid-suite,
leaving no test results and causing the task to be blocked with a
`terminal:timeout` error.

## Fix: always pass explicit `timeout` for long operations

Set `timeout` high enough for the slowest plausible run. Conservative values
for MAC repo work:

| Operation                       | Recommended timeout (seconds) |
|---------------------------------|-------------------------------|
| `scripts/run-contract-tests.sh` | 600                           |
| `python3 scripts/bootstrap-project.py` | 300                  |
| `codegraph init .`              | 300                           |
| `codegraph sync`                | 300                           |
| `git clone --depth 1 <repo>`    | 240                           |
| Any `pip install` / `uv sync`   | 300                           |

Example — running the contract test suite:

```python
terminal(command="scripts/run-contract-tests.sh", timeout=600, workdir=worktree)
```

Never use the default timeout for any of the operations above.

## Recovery when a timeout has already occurred

1. Re-run the failing operation with the explicit timeout above.
2. If the suite still exceeds the budget, decompose the task: post child tasks
   via the MAC API (`POST /tasks/<id>/children`) so each child covers one
   deliverable and can be verified independently.
3. Record the explicit timeout used under `verification.environment_delta` in
   `mac-evidence.json` so reviewers can confirm it was intentional.

## Checked-in docs / skills identity rule

When adding or updating skills in the `skills/` directory, docs in `docs/`,
or deploy markdown, use only generic role names and placeholders:

- Roles: `hub`, `worker-1`, `worker-2`, `gpu-worker`
- Placeholders: `<user>`, `<host>`, `<mesh-ip>`, `<fleet-name>`

Never embed real agent names, usernames, hostnames, or IP addresses.
The test `test_docs_carry_no_operator_identity` enforces this and will fail
the contract suite if any fleet-specific identity token is found.

## Diagnostic checklist

- [ ] Does the task evidence show `"timed out"` or `exit_code: 124`?
- [ ] Was `terminal()` called without an explicit `timeout` parameter?
- [ ] Is the operation in the long-lived category above?
- [ ] Did the previous attempt add a skill/doc with operator identity tokens?
      Check with: `grep -rn "agent_\|worker[0-9]" skills/ docs/`
- [ ] Does the failure mention `CARGO_HOME`, `cargo/bin`, `rustup`, or `rust-toolchain`?
      These indicate a Rust toolchain provisioning gap — `cargo` lives in `~/.cargo/bin`,
      which is outside `MAC_SANDBOX_BASE_PATH`. The task executor symlinks it into
      `MAC_TOOLCHAIN_BIN` on demand; if that step failed or was skipped, `cargo`
      will not be found even on agents with Rust installed.
      Verify with: `echo $CARGO_HOME; ls ${CARGO_HOME:-$HOME/.cargo}/bin/cargo 2>/dev/null`
- [ ] Did the task declare `cargo`, `rustc`, or `rustup` in `toolchain.required_commands`?
      If yes and the toolchain setup shell did not run, the symlink into `MAC_TOOLCHAIN_BIN`
      was never created. Re-run bootstrap or inspect `mac_sandbox_toolchain_setup` output.

## Pitfall: cargo not found even when Rust is installed

**Symptom:** A task with `cargo`, `rustc`, or `rustup` in
`toolchain.required_commands` fails with `command not found: cargo` (or similar)
even though Rust is installed on the worker.

**Root cause:** Cargo's binaries live in `~/.cargo/bin` (or `$CARGO_HOME/bin`).
That directory is intentionally outside `MAC_SANDBOX_BASE_PATH`, so cargo is not
visible inside the task sandbox by default.

**How it is handled automatically:** `mac_sandbox_toolchain_setup` (injected into
every task sandbox before agent or test work runs) iterates over
`toolchain.required_commands`. When it encounters `cargo`, `rustc`, or `rustup`, it
creates symlinks from `$CARGO_HOME/bin/<binary>` (defaulting to `~/.cargo/bin`)
into `MAC_TOOLCHAIN_BIN`, then calls `mac_refresh_sandbox_path` so the updated
`PATH` takes effect. If no local Rust installation exists, it runs the upstream
`rustup` installer and links the freshly installed binaries into `MAC_TOOLCHAIN_BIN`.

**What can go wrong:**

- `mac_sandbox_toolchain_setup` was never called (e.g., the sandbox shell script
  was skipped or the task used a raw `terminal()` call without sourcing the setup).
- `cargo` is not listed in `toolchain.required_commands`, so the symlink step is
  never triggered.
- `CARGO_HOME` points to an unexpected location and `~/.cargo/bin/cargo` does not
  exist; the installer then has no network access to fetch Rust.

**Fix checklist:**

1. Declare `cargo` (or `rustc`/`rustup`) in `toolchain.required_commands` inside
   the repository's `.mac/project.yaml`. `mac_sandbox_toolchain_setup` runs
   automatically for every task that uses the repository contract and will symlink
   the Cargo binaries into `MAC_TOOLCHAIN_BIN`.
2. If you are running a `terminal()` call manually (outside the normal sandbox
   boot), source the toolchain setup first or prepend
   `mac_sandbox_toolchain_setup &&` to the command.
3. Verify the symlink is present before the failing command:
   `ls -la $MAC_TOOLCHAIN_BIN/cargo`.
4. If the binary is still missing, inspect `$MAC_TASK_WORKSPACE/mac-toolchain.log`
   for errors from the install step.
