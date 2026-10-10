# Updating the fleet with `fleet-update`

`scripts/fleet-update` is the human-run way to move the hub and the workers to
a new commit. You run it on the hub, as the operator account. It
updates one host at a time, checks each host before it moves on, and stops at
the first failure.

It replaces hub self-upgrade, release epochs and source convergence, which
have been deleted: in the 90 days before it was written, release epochs aborted
62% of the time and hub self-upgrade never succeeded. The older deploy script
(`deploy/deploy-mac-fleet.sh`) and its node installer have been deleted too.

## Usage

```console
scripts/fleet-update [--dry-run] [--hermes] [--yes] \
    [--runtime-image REF --runtime-input-sha256 SHA] <hub|HOST|all> <sha>
```

| Argument | Meaning |
| --- | --- |
| `hub` | Update the hub's control plane, then restart the hub's own worker. |
| `HOST` | Update one Linux worker, named as in `~/.mac/fleet-hosts`. |
| `all` | The hub first, then each worker in turn. Stops at the first failure. |
| `<sha>` | Any commit on an `origin` branch. A short sha is fine; it is resolved to the full one. |
| `--dry-run` | Print the plan, including any migrations, and change nothing. The only write is `git fetch`, which updates remote-tracking refs. |
| `--hermes` | Also restart the Hermes gateway (`ai.hermes.gateway` on the hub, `hermes-gateway` user unit on Linux). |
| `--yes` | Don't ask for confirmation before each host. |
| `--runtime-image REF --runtime-input-sha256 SHA` | Repin each worker's sandbox image to a published `ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:...` digest and its CI frozen-input identity, both from the image's CI publication. Give both or neither. This changes only the image, never the OpenShell version. |

Every line of output also goes to `~/.mac/logs/fleet-update-<timestamp>.log`.

### Hosts

Worker names are looked up in `~/.mac/fleet-hosts` (override the path with
`FLEET_UPDATE_HOSTS`). Each line has the form `<name> <ssh_target> <agent_id>`:

```text
# name       ssh target              hub agent id
worker-1     <user>@<mesh-ip-1>      agent_worker-1
worker-2     <user>@<mesh-ip-2>      agent_worker-2
```

Create this file before the first run. If it doesn't exist, the script falls
back to a built-in list of two workers (`host_entry` in the script), which is
only right for the fleet it was first written for. `all` updates every worker listed there, in file order.

Other knobs, mostly useful for testing: `FLEET_UPDATE_SRC` (the checkout, by
default `~/.mac/src/mac`), `FLEET_UPDATE_HUB_URL` (default
`http://127.0.0.1:8789`), `FLEET_UPDATE_MAC_CLI` (default `mac`),
`FLEET_UPDATE_POLL_SECONDS`, `FLEET_UPDATE_DRAIN_TIMEOUT` (default 600) and
`FLEET_UPDATE_HEALTH_TIMEOUT` (default 180).

## Preflight (every target)

1. `git fetch origin` in `~/.mac/src/mac` on the hub.
2. Resolve `<sha>` to a full commit and refuse it unless some `origin/*`
   branch contains it.
3. Read the commit the host currently runs. On the hub that is the checkout's
   `HEAD`; on a worker it is read over ssh.
4. Compare `src/mac/data/postgres/migrations/*.sql` between the two commits:
   - migrations that the target adds are listed;
   - if the deployed commit has a migration the target lacks, the script refuses.
     It never goes backwards across a migration.
5. The checkout must be clean (`git status --porcelain` empty).

## Hub (macOS)

1. `sudo -n launchctl bootout system/com.mac.control-plane`, then wait for the
   `mac.hub_serve` process to exit.
2. Run `~/.mac/venv/bin/mac-pg-backup --json --out ~/.mac/backups/fleet-update-<ts>`
   and require `restore_verified == true`. `MAC_DATABASE_URL` is read from
   `~/.mac/mac.env` and passed through the environment, never on a command line.
3. Run `git checkout --detach <sha>`.
4. Do the locked install in place (`python -m mac.native_runtime`, the same
   command as step 4 of "Provision a new host") if `pyproject.toml` or
   `uv.lock` changed. Also do it if `python -m mac.native_runtime --check`
   reports that the venv's locked baseline no longer holds. Worker pip installs
   check that baseline and fail closed without it, and a plain `uv pip install
   -e` leaves it stale. A failed repair of a stale baseline is only a warning.
5. Run `~/.mac/venv/bin/python -c 'import mac.services'`.
6. Run `mac-schema-migrate --status`, then
   `mac-schema-migrate --applied-by fleet-update:<user>@<host>:<sha12>`.
7. Write `~/.mac/current/source-commit` and `~/.mac/current/generation-id`
   (`legacy-<sha12>`). `deploy/bin/mac-service` ignores them. They are written
   for a hub still running the `mac-service` that the deleted
   `deploy/fleet-node-install.sh` generated, which runs `~/.mac/current/source`
   only when its `HEAD` equals `source-commit`.
8. Run `sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.control-plane.plist`.
9. Poll `/health`, then require `/startup-attestation`'s `source_commit` to equal
   the target. `mac-service` sets it from the `HEAD` of `~/.mac/src/mac`.
10. Run `python -m mac.fleet_context_service --source ~/.mac/src/mac`. It
    installs or repairs the LaunchAgent `com.mac.fleet-context`, which refreshes
    the live fleet block in this agent's runtime context every 3 minutes, and
    runs one refresh. Its JSON result is logged; a failure never fails the update.
11. Run `launchctl kickstart -k gui/<uid>/com.mac.agent`. With `--hermes`, also
    kickstart `gui/<uid>/ai.hermes.gateway`.

The hub step doesn't reinstall `~/.mac/bin/mac-service`. Its content is in
`deploy/bin/mac-service`: it runs `~/.mac/venv/bin/python -m mac.hub_serve`
from `~/.mac/src/mac` and nothing else. The wrapper that
the deleted `deploy/fleet-node-install.sh` used to generate also ran
`~/.mac/current/venv/bin/mac-hub-upgrade-supervisor recover-all` when that
script existed. Hub self-upgrade is deleted, so replace that wrapper with
`deploy/bin/mac-service` by hand.

### Hub rollback

- **Failure before the schema can have changed.** This covers a backup that
  fails or isn't verified, a failed checkout, install or import, and any
  failure when `--status` reported no pending migrations. The script checks out
  the old commit, reinstalls it if the dependency files differ, rewrites
  `source-commit`, bootstraps the old hub and exits non-zero.
- **Failure once migrations have run.** This covers a migration that fails
  part-way (the hub is left stopped) and a hub that won't come up healthy after
  migrating. The script **stops** and prints what to do. It never downgrades
  the schema. Fix forward with a new sha if you can. A full restore discards
  every write made after the backup, so do it by hand:
  1. Stop the hub.
  2. `pg_restore` the dump into the database. The dump is listed in the
     backup's manifest.
  3. Run `git -C ~/.mac/src/mac checkout --detach <old>` and write that sha to
     `~/.mac/current/source-commit`.
  4. Bootstrap the LaunchDaemon.

If the script exits while the hub is stopped, its last line says so and gives
the `launchctl bootstrap` command.

## Linux worker

1. **Hold.** Read the agent with `mac agent show <agent_id>`. If it already has
   a dispatch hold, note it and leave it alone; this run will neither add nor
   remove a hold, such as an operator's fleet-wide dispatch pause. If there is
   no hold,
   `mac agent hold <agent_id> --reason "fleet-update <sha12>"`.
2. **Drain.** Wait up to 10 minutes for `current_task_id` to clear. If it
   doesn't, release the script's own hold (if it set one) and stop. Nothing has
   changed on the host at that point.
3. **Update over ssh.** A single `ssh <target> bash -s` runs these steps:
   - `git fetch` and `git checkout --detach <sha>` in `~/.mac/src/mac`;
   - the locked install when the dependency files changed or the baseline
     check fails, as on the hub;
   - `python -c 'import mac'`. If the import fails, the old checkout and its
     dependencies are restored before anything is restarted.
   - install `deploy/bin/{mac-agent-service, mac-agent-startup-self-test,
     mac-task-executor, mac-task-executor.py}` and `deploy/mac-crash-observer.py`
     into `~/.mac/bin`. Each file is renamed into place, so a running wrapper
     keeps its old inode.
   - `python -m mac.fleet_context_service --source ~/.mac/src/mac`, as on the
     hub but with the `mac-fleet-context` systemd timer: the units are rendered
     from `deploy/systemd` into `/etc/systemd/system`, a failed timer is reset,
     and one refresh runs. A failure is logged, never fatal.
   - `python -m mac.openshell_image_pin`. A worker pins its sandbox image in
     one place, `~/.mac/openshell/runtime-image-ref`. This step rewrites
     `MAC_OPENSHELL_CREATE_ARGS --from` in `~/.mac/mac.env` to match that pin
     and removes `MAC_HUB_VERIFY_IMAGE`, because the test gate now reads the
     pin too. `mac.env` is backed up before it changes. A host with no managed
     pin is left alone. With `--runtime-image`, the step first pulls the
     image, checks its build-revision and frozen-input labels, and writes the
     pin files. If that fails, the old checkout is restored and nothing is
     restarted. A plain sync failure is logged, never fatal.
   - `sudo -n systemctl restart mac-agent`. With `--hermes`, also
     `systemctl --user restart hermes-gateway`.
   - after 10 seconds, `systemctl is-active mac-agent`.
4. **Health.** Within 3 minutes, the hub must report the new commit for this
   agent: `resources.source_state.commit_sha == <sha>` with `dirty == false`.
5. **Resume.** The script resumes dispatch only if it placed the hold itself,
   and only if the hold reason is still `fleet-update <sha12>`. If someone
   replaced the hold meanwhile, the script leaves it alone.
6. **Failure.** The rollout stops, the host stays held, and the script prints
   an inspect command, a rollback command and the resume command.

### Worker rollback

```console
ssh <target> 'git -C ~/.mac/src/mac checkout --detach <old-sha> && sudo -n systemctl restart mac-agent'
mac agent resume <agent_id>      # only if fleet-update placed the hold
```

GKE pods (supervisord `mac-agent`) are not handled by this script.

## Worker tokens

`mac admin worker-token` issues long-lived worker bearer tokens using the
existing `worker_credentials` table. It runs on the hub, against
`MAC_DATABASE_URL` (or `mac --db <dsn>`).

```console
mac admin worker-token issue  <agent_id> [--days 365] [--out FILE] [--install HOST]
mac admin worker-token rotate <agent_id> [--days 365] [--out FILE] [--install HOST]
mac admin worker-token list   [<agent_id>]
```

- `issue` and `rotate` are the same operation. Each mints a new credential that
  expires after `--days` days (default 365) and makes it the agent's only active
  credential; earlier ones become `superseded`.
- The token is shown exactly once:
  - with no options, the raw token goes alone on stdout and a summary goes to
    stderr;
  - `--out FILE` writes the token to a mode-0600 file instead;
  - with `--install`, the token is not printed at all.
- Without `--install`, the old token stops working immediately. Install the new
  one straight away.
- `--install HOST` keeps the old token working until the new one is in place.
  While the new credential is still pending (the old one keeps authenticating),
  the command runs `ssh HOST bash -s`, with the token carried on stdin, never
  in argv. On the host, that session:
  1. rewrites every `MAC_WORKER_TOKEN` / `MAC_WORKER_TOKEN__<FLEET>` key that
     `~/.mac/mac.env` already has (or adds `MAC_WORKER_TOKEN` if it has none),
     and moves any hub-facing alias that held the old token;
  2. runs `~/.mac/venv/bin/python -m mac.hermes_chat_config --hermes-home ~/.hermes --mac-env ~/.mac/mac.env`,
     because Hermes embeds the token;
  3. restarts `mac-agent` and `hermes-gateway`.

  Only after that succeeds is the new credential activated. If the install
  fails before `mac.env` is rewritten, the new credential is revoked and the
  old one stays active. If `mac.env` was rewritten but the Hermes resync or a
  restart failed, the new credential is activated anyway (the host already
  holds it) and the command tells you to restart the services by hand.
- `list` prints credential metadata. It never prints the token or its hash.

## Provision a new host

`fleet-update` only moves an already-provisioned host to a new commit. The
deleted deploy scripts did the rest, so do these by hand. `$HERMES_HOME` is
the host's Hermes home (default `~/.hermes`).

1. Install Tailscale and join the tailnet. Confirm that `ssh <host>` works from
   the hub without a password.
2. Install git 2.38 or newer (`merge-tree --write-tree`; Ubuntu 22.04 ships
   2.34), `gh` with HTTPS credentials
   (`GH_TOKEN=... gh auth setup-git --hostname github.com`), and the review key
   at `~/.ssh/mac_github_review_id` (mode 0600, plus a `Host github.com`
   `IdentityFile`/`IdentitiesOnly yes` entry in `~/.ssh/config`). The hub needs
   a working GitHub SSH identity; workers only use it for reviews.
3. Grant passwordless sudo for what `fleet-update` runs: on a Linux worker,
   `<user> ALL=(root) NOPASSWD: /usr/bin/systemctl restart mac-agent`; on the
   macOS hub, `<user> ALL=(root) NOPASSWD: /bin/launchctl`. Check with
   `sudo -n true`.
4. `git clone <origin> ~/.mac/src/mac`, then do the locked install and put
   `mac` on `PATH`:

   ```console
   PYTHONPATH=~/.mac/src/mac/src python3 -m mac.native_runtime \
     --source ~/.mac/src/mac --venv ~/.mac/venv \
     --snapshot ~/.mac/logs/native-runtime-packages.json \
     --footprint ~/.mac/agent-footprint.json --uv "$(command -v uv)" \
     --record ~/.mac/logs/native-runtime-locked.json
   ln -sf ~/.mac/venv/bin/mac ~/.local/bin/mac
   ```

   The operator side of `fleet-update` calls plain `mac` (`FLEET_UPDATE_MAC_CLI`).
5. Create `~/.mac/mac.env` (mode 0600). `deploy/systemd/mac.env.example` is
   written for a hub and lacks most worker keys, so set these by hand on a
   worker: `MAC_CONTROL_PLANE_ROLE=client`, `MAC_HUB_URL` and `MAC_URL`,
   `MAC_FLEET_NAME`, `MAC_AGENT_ID`, `MAC_WORKER_TOKEN` (written by step 7),
   `MAC_WORKER_MODE=loop`, `MAC_WORKER_AGENT_NAME`, `MAC_WORKER_HOSTNAME`,
   `MAC_WORKER_CAPABILITIES`, `HERMES_HOME`, `HERMES_REDACT_SECRETS=true`,
   `MAC_MEMORY_TOPOLOGY_FILE`, `GH_TOKEN`, plus the OpenShell keys your hosts
   use. The `mac-agent` wrapper refuses to start without `MAC_HUB_URL` and
   `MAC_WORKER_TOKEN`.
6. Provider keys live only in the hub vault, never in worker env. On the hub,
   store each as `<provider>-upstream`:
   `printf %s "$KEY" | mac admin secret set openai-upstream --from-stdin --scopes '{"capabilities":["router-upstream"]}' --created-by <you>`.
   On a worker, remove `{NVIDIA,OPENAI,ANTHROPIC,PERPLEXITY}_API_KEY` and
   `_BASE_URL`, `NVIDIA_API_BASE`, `NVIDIA_IMAGE_BASE_URL`,
   `PERPLEXITY_API_BASE`, `FAL_KEY`, `VLLM_API_KEY`, `HAIMAKER_API_KEY`,
   `LLM_KEY`, `LLM_URL`, `QDRANT_API_KEY` and `FIRECRAWL_API_KEY` from
   `mac.env` and `$HERMES_HOME/.env`.
7. On the hub, add the host to `~/.mac/fleet-hosts`. A token can only be
   issued to a registered agent, so register it first:
   `mac admin machine register <hostname> --machine-id <machine_id>` and
   `mac agent register <machine_id> <name> --agent-id <agent_id>`. Then run
   `mac admin worker-token issue <agent_id> --install <host>`.
8. Hub only: run `deploy/install-postgres-service.sh`,
   `deploy/install-qdrant-service.sh`, `deploy/install-firecrawl-gateway.sh`
   and `deploy/install-webdav-server.sh` (each with `MAC_HOME`, `WORKSPACE`
   and `FLEET_NAME` set). The Postgres installer writes `MAC_DATABASE_URL` into
   `~/.mac/mac.env`; confirm it is there. The others write `QDRANT_URL`, the
   `FIRECRAWL_*` and `MAC_WEB_SEARCH_*` keys and `MAC_PUBLISH_*`.
9. Install the systemd units from `deploy/systemd`. `mac-agent.service` is
   rendered from `deploy/systemd/mac-agent.service.in`; the sed command is at
   the top of that file. Then run `sudo systemctl daemon-reload && sudo systemctl enable mac-agent`.
10. Prepare Hermes:
    - write `$HERMES_HOME/mac-memory-topology.json` (schema
      `mac.hermes.memory_topology.v1`, naming the hub's Qdrant and Firecrawl
      URLs) and, in `$HERMES_HOME/.env`, `MAC_MEMORY_TOPOLOGY_FILE`,
      `QDRANT_URL`, `FIRECRAWL_API_URL`, `MAC_WEB_SEARCH_PROVIDER=firecrawl`,
      `HERMES_WEB_SEARCH_BACKEND=firecrawl` and
      `HERMES_WEB_EXTRACT_BACKEND=firecrawl`;
    - force secret redaction: `redact_secrets: true` in
      `$HERMES_HOME/config.yaml` and `HERMES_REDACT_SECRETS=true` in
      `$HERMES_HOME/.env`;
    - write the runtime context, without which `prepare` refuses to run:

      ```console
      ~/.mac/venv/bin/python -m mac.hermes_runtime \
        "$HERMES_HOME/mac-runtime-context.json" "$HERMES_HOME/mac-runtime-context.md" "$HERMES_HOME/.env" \
        --agent-name <name> --fleet-name <fleet> --mac-url "$MAC_HUB_URL" \
        --hermes-home "$HERMES_HOME" --mac-home ~/.mac --workspace ~/.mac/src/mac \
        --tenant-id "$MAC_FLEET_TENANT_ID" --persona-id "$MAC_HERMES_PERSONA_ID" \
        --hermes-instance-id "$MAC_HERMES_INSTANCE_ID" --agent-id <agent_id>
      ```

    - run `deploy/openshell/bootstrap-openshell.sh`, then
      `deploy/hermes/install-hermes-gateway.sh prepare`.
11. Copy each `deploy/skills/fleet/<skill>/` that has a `SKILL.md` into
    `$HERMES_HOME/workspace/skills/`. On a GPU host (`nvidia-smi -L` works),
    also `tar xzf deploy/skills/omniverse-skills.tar.gz -C $HERMES_HOME/workspace/skills`
    and add the `local-gen` media units `<fleet>-gen-server` (port 8189),
    `<fleet>-gen-audio-server` (8190) and `<fleet>-gen-video-server` (8191).
    Each runs `deploy/local-gen/{openai_image_server,audio_server,video_server}.py`
    with `~/.mac/mac.env` sourced.
12. From the hub: `scripts/fleet-update <host> <sha>`. This installs
    `deploy/bin` into `~/.mac/bin` and starts the worker.
