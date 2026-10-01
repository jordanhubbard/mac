# Updating the fleet with `fleet-update`

`scripts/fleet-update` is the human-run way to move the hub and the workers to
a new commit. You run it on the hub, as the operator account. It
updates one host at a time, checks each host before it moves on, and stops at
the first failure.

It sits alongside the older deploy protocol (`deploy/deploy-mac-fleet.sh`,
release epochs, hub self-upgrade). In the 90 days before it was written, release
epochs aborted 62% of the time and hub self-upgrade never succeeded. The old
machinery will be deleted once this script has been proven on the real fleet.

## Usage

```console
scripts/fleet-update [--dry-run] [--hermes] [--yes] <hub|HOST|all> <sha>
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
4. Run `uv pip install -e` (or the venv's `pip`), but only if `pyproject.toml`,
   `uv.lock` or `setup.py` changed.
5. Run `~/.mac/venv/bin/python -c 'import mac.services'`.
6. Run `mac-schema-migrate --status`, then
   `mac-schema-migrate --applied-by fleet-update:<user>@<host>:<sha12>`.
7. Write `~/.mac/current/source-commit` and `~/.mac/current/generation-id`
   (`legacy-<sha12>`). `mac-service` only uses `~/.mac/current/source` when
   `source-commit` matches its `HEAD`.
8. Run `sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.control-plane.plist`.
9. Poll `/health`, then require `/startup-attestation`'s `source_commit` to equal
   the target.
10. Run `launchctl kickstart -k gui/<uid>/com.mac.agent`. With `--hermes`, also
    kickstart `gui/<uid>/ai.hermes.gateway`.

The hub step doesn't reinstall `~/.mac/bin/mac-service`. Its content is in
`deploy/bin/mac-service` if it ever needs replacing by hand.

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
   - a conditional `uv pip install -e`;
   - `python -c 'import mac'`. If the import fails, the old checkout and its
     dependencies are restored before anything is restarted.
   - install `deploy/bin/{mac-agent-service, mac-agent-startup-self-test,
     mac-task-executor, mac-task-executor.py}` and `deploy/mac-crash-observer.py`
     into `~/.mac/bin`. Each file is renamed into place, so a running wrapper
     keeps its old inode.
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

1. Install Tailscale and join the tailnet. Confirm that `ssh <host>` works from
   the hub without a password.
2. `git clone <origin> ~/.mac/src/mac`.
3. Run `python3 -m venv ~/.mac/venv && ~/.mac/venv/bin/pip install -e ~/.mac/src/mac`
   (or use `uv venv` and `uv pip install -e`).
4. Create `~/.mac/mac.env` (mode 0600) from `deploy/systemd/mac.env.example`.
   That example is written for a hub, so for a worker set `MAC_HUB_URL`,
   `MAC_AGENT_ID`, `MAC_WORKER_MODE=loop` and the Hermes and OpenShell keys
   your hosts use.
5. On the hub, add the host to `~/.mac/fleet-hosts`. A token can only be
   issued to a registered agent, so register it first:
   `mac admin machine register <hostname> --machine-id <machine_id>` and
   `mac agent register <machine_id> <name> --agent-id <agent_id>`. Then run
   `mac admin worker-token issue <agent_id> --install <host>`.
6. Install the systemd units from `deploy/systemd`. `mac-agent.service` is
   rendered from `deploy/systemd/mac-agent.service.in`; the sed command is at
   the top of that file. Then run `sudo systemctl daemon-reload && sudo systemctl enable mac-agent`.
7. Run `deploy/openshell/bootstrap-openshell.sh` on the host, then
   `deploy/hermes/install-hermes-gateway.sh prepare`.
8. From the hub: `scripts/fleet-update <host> <sha>`. This installs
   `deploy/bin` into `~/.mac/bin` and starts the worker.
