# Home-Directory Consolidation: Analysis & Plan

Status: **plan** (approved target = single authoritative root under `$HOME`).
Scope: first-party MAC only (`src/mac/` excluding the vendored `src/mac/_hermes/`
snapshot, `deploy/`, `scripts/`, `Makefile`). Every claim below is grounded in
`file:line` evidence gathered by a three-way exploration of the `.mac`,
`.hermes`, and `.openclaw`/`.nemoclaw` namespaces.

## Motivation

An agent's "personal data" and the control-plane's operational state are
currently spread across multiple sibling directories under `$HOME`. This leaves
the system open to **metadata duplication**, **abandoned metadata**, and a
**POLA violation**: anyone inspecting an agent's data finds it scattered in a
confusing way, with the same logical datum (secrets, identity/soul) living in
several places. Example that surfaced this: an agent described its dream logs as
living in `~/.hermes/dream_logs/` — a location MAC does not own — even though we
have migrated the gateway off Hermes onto OpenClaw.

## 1. Actual topology — four homes, not three

| Home | Role | Location knob | Owns (authoritative) |
|---|---|---|---|
| `~/.mac` | Control plane / hub | `MAC_HOME` (**leaky**, §3) | `mac.db` ledger, `fleets.yaml`, `mac.env` (hub secrets), `client-principals.json`, `qdrant/` (L2 memory), installed `src`/`venv`/`hermes-agent`, `bin/`, OpenShell policies |
| `~/.hermes` | Gateway / agent-personal | `HERMES_HOME` (default pinned by **one line**) | `SOUL.md`/`USER.md`/`MEMORY.md`/`memories/`, mood, `config.yaml`, `.env` (chat+Slack secrets), `auth.json`, `state.db`, `sessions/`, `skills/`, `plugins/`, `cron/`, logs, dream logs |
| `~/.hermes-nemoclaw` | NemoClaw gateway | `NEMOCLAW_HERMES_HOME` | A *second full Hermes home* (a clone), used only by the nemoclaw pilot/compose deployment |
| `~/.openclaw` | — | — | **Not a host home.** OpenClaw's real home is `~/.mac/openclaw/` (already under `.mac`). `~/.openclaw` exists only as a *container* symlink → `/sandbox/state`. `.nemoclaw` as a dotdir does not exist — it is only an env-var/label prefix. |

**Key structural insight:** consolidation is already half-done. The newer
components — OpenClaw home (`$MAC_HOME/openclaw`,
`deploy/openclaw/install-openclaw-gateway.sh:16`), OpenShell policy
(`src/mac/executor_sandbox.py:667`), installed source, venv — all live under
`~/.mac`. The **only** real holdout is the legacy Hermes home at the `$HOME/.hermes`
sibling (plus its `.hermes-nemoclaw` clone). This is not "merge three peers";
it is "pull the last legacy home under the root everything else already uses."

## 2. Split-brain instances (the concrete duplication / POLA violation)

1. **Secrets are tri-located.** The hub `MAC_API_TOKEN` and Slack tokens live in
   `.mac/mac.env` *and* `.hermes/.env` *and* (client-scoped) `.mac/.env`. The hub
   deliberately reads **both** `mac.env` and `.hermes/.env` as one merged pool
   (`src/mac/api.py:2755-2768`), and deploy actively **copies** secrets from
   `mac.env` → `.hermes/.env` (`deploy/fleet-node-install.sh:8623-8700`). A
   `scrub_spoke_provider_secrets` routine (`fleet-node-install.sh:10523-10560`)
   exists *only* to clean provider keys that leak into `.hermes/.env`.
2. **Identity/soul is triplicated.** `SOUL.md`/`USER.md`/`MEMORY.md` are
   authoritative in `.hermes` (`src/mac/soul_snapshot.py:33-34`,
   `journal.py:29`), byte-copied into `.mac/journal/<date>/` (backup,
   `journal.py:88-98`), and migrated into `.mac/openclaw/workspace/`
   (`deploy/openclaw/migrate-hermes-continuity.py:203-215`). NemoClaw adds a
   fourth copy in `.hermes-nemoclaw`.
3. **Reverse leakage:** MAC writes its *own* artifacts —
   `mac-runtime-context.json/.md`, `mac-memory-topology.json` — **into the
   gateway's `.hermes` home** (`src/mac/deploy_env.py:437-443`). Control-plane
   data misfiled in the agent-personal home.
4. **`slack_home_channels.json`** lives in `.hermes` and is copied to
   `.mac/openclaw` (`install-openclaw-gateway.sh:767-815`).

None of these are *live two-writer conflicts* today (copies are one-way,
source-authoritative). The risk is **drift, staleness, and "where does my data
actually live?"** — precisely POLA.

## 3. Why relocation is currently unsafe — leaky resolvers

Neither root has a single honored resolver, which is the root cause:

- **`.hermes`** — the *read* path is clean: everything resolves through
  `$HERMES_HOME` (`src/mac/journal.py:39-40` et al.), pinned by **one line**
  (`src/mac/deploy_env.py:429`) plus three derived paths (`:437,438,443`).
  Genuinely hard-wired to the literal `.hermes` (ignore the env): the SSH soul
  transport (`src/mac/soul_snapshot.py:81`), the SSH migrate tar/verify
  (`src/mac/agent_migrate.py:46-58,211,407,504,512`), and a host probe
  (`deploy/fleet-node-install.sh:10341`).
- **`.mac`** — `MAC_HOME` is a **leaky knob**. The canonical `mac_home()`
  (`src/mac/client_principals.py:62`) honors it, but dozens of hot-path modules
  re-implement the path and hard-code `~/.mac` *ignoring* `MAC_HOME`: the ledger
  (`src/mac/dispatch.py:2811`), fleets registry (`src/mac/fleet_creds.py:121`),
  journal, most of `src/mac/cli.py`, OpenShell. Setting `MAC_HOME` today would
  relocate *some* data and orphan the rest — a data-loss trap.

**Conclusion:** the prerequisite for any relocation is a single, reliably-honored
resolver per root. That is Phase 0 and must land first.

## 4. Target shape (approved: single authoritative root)

```
$MAC_HOME                (default ~/.mac; XDG-aware)   ← the ONE authoritative root
├── ledger/    mac.db, backups, archive
├── secrets/   mac.env, .env, client-principals.json     ← single secret source
├── fleet/     fleets.yaml, specs
├── runtime/   mac-runtime-context.*, memory-topology, journal/
├── gateway/   (← today's ~/.hermes: SOUL, memory, sessions, skills, cron, dream logs)
│   └── openclaw/     (already here as .mac/openclaw)
└── toolchain/ src, venv, bin, hermes-agent
```

The control-plane vs agent-personal **trust boundary is preserved by subdir +
`0700` perms**, not by separate top-level directories. `~/.hermes` and
`~/.hermes-nemoclaw` become compatibility **symlinks** into `$MAC_HOME/gateway*`
so nothing external breaks during the transition.

## 5. Phased plan (each phase independently shippable and reversible)

### Phase 0 — One resolver, no data moves (prerequisite)
Introduce `src/mac/mac_paths.py` as the *only* sanctioned resolver
(`mac_home()`, `gateway_home()`, `secrets_dir()`, `ledger_path()`, …). Route
every hard-coded `~/.mac` and `~/.hermes` site through it (see §3 for the site
lists). Add a test/lint that fails on any new literal
`Path.home() / ".mac" | ".hermes"`. **Moves zero data**; closes the leaky-knob
trap and makes relocation possible. Highest value / lowest risk.

### Phase 1 — De-duplicate before relocating
Fix the split-brain *writes* so we do not relocate duplicated data:
- Make one secret file authoritative; render `.hermes/.env` as a
  projection/symlink of `mac.env` and delete the copy-into-both
  (`fleet-node-install.sh:8623-8700`).
- Move MAC's `mac-runtime-context.*` / `mac-memory-topology.json` out of
  `.hermes` into `$MAC_HOME/runtime/` (`deploy_env.py:437-443`); update readers.
- Keep `.mac/journal/` (a legitimate dated backup).

### Phase 2 — Relocate the gateway home under the root
Flip the one pin (`deploy_env.py:429`) and the three hard-wired literals
(`soul_snapshot.py:81`, `agent_migrate.py`, `fleet-node-install.sh:10341`) to
`gateway_home() = $MAC_HOME/gateway`. Migrate on-disk `~/.hermes` →
`$MAC_HOME/gateway` with an **idempotent, checksum-verified,
permission-preserving** move, leaving `~/.hermes` as a compat symlink. Fold
`.hermes-nemoclaw` the same way.

### Phase 3 — Retire `.hermes` (post-migration-off-Hermes)
Once OpenClaw is the sole gateway (its live data already under `.mac/openclaw`),
drop the legacy subtree entirely and remove the compat symlinks.

### Cross-cutting — accuracy & safety
- Extend `src/mac/hermes_home_audit.py` into a `mac_home_audit` that asserts the
  canonical unified layout and flags orphans/drift (built-in abandoned-metadata
  detection). `audit_hermes_home()` already hard-codes a 66-entry canonical
  allow-list; generalize it to the new root.
- Every move idempotent + dated-backup (`.mac/backups`) + reversible.
- Secrets never printed; `0600`/`0700` perms preserved across moves.
- Roll out as a deploy epoch with Phase 0 landing first in a backward-compatible
  **read-old / write-new / symlink-bridge** mode so no worker breaks mid-fleet.

## 6. Risks

- Fleet-wide blast radius: home resolution runs in every worker + the hub +
  deploy. Phase 0 must be backward-compatible and land as a coordinated epoch.
- Vendored gateway code (`src/mac/_hermes/`, OpenClaw upstream) expects its
  internal home layout; we relocate the *root* via `HERMES_HOME`, never rename
  the gateway's internal structure.
- Secrets migration is the highest-care step — verify perms and that no secret
  is duplicated or dropped; do it with checksums and a reversible backup.
