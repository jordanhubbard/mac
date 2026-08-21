# Fleet node onboarding checklist

This is the completion contract for adding or rebuilding any long-lived MAC
fleet node. It consolidates the deployment paths, live acceptance checks, and
failure lessons recorded in the task ledger and repository history.

The checklist deliberately uses role names and placeholders. Real agent names,
targets, jump hosts, and credentials belong only in `~/.mac/fleets.yaml`, a
home-scoped fleet spec, the encrypted hub vault, or a mode-`0600` env file.

## Completion rule

A node is onboarded only when all four proofs are current and agree:

1. **Reachability:** the definitive SSH route works non-interactively with
   strict host-key checking, and the node reaches the hub and required shared
   services.
2. **Runtime identity:** one stable agent row heartbeats with the intended
   role, supervisor, capabilities, hardware, source revision, and no dispatch
   hold.
3. **Execution trust:** startup self-test, OpenShell policy, attestation,
   repository access, tests, push, and independent review all pass through a
   real canary.
4. **Usable model route:** at least one coding CLI is not merely installed or
   configured, but has a fresh matching end-to-end sandbox verification.

Do not promote a node because it appears in `mac admin fleet snapshot`. A heartbeat
proves liveness, not source convergence, disk headroom, valid signing state, or
successful sandbox execution.

## Authorities and variables

Use these authorities in order:

1. `~/.mac/fleets.yaml` for the current target, jump route, OS, supervisor,
   hub URL, and role. Never reconstruct a target from an agent name, SSH alias,
   `known_hosts`, old deploy log, task history, or prior conversation.
2. `~/.mac/specs/<fleet>.fleet.yaml` for declarative desired state, when the
   fleet was created from a setup spec.
3. The hub task ledger and live agent resources for lifecycle and actual-state
   evidence.
4. `~/.mac/logs/deploy-manifest-latest.json` and the matching deploy logs for
   what was installed.
5. Git history for the release implementation, never for live target mapping.

Set role-neutral shell variables while working through a node:

```console
export FLEET=<fleet-name>
export HUB=<hub-agent-name>
export AGENT=<agent-name>
export EXPECTED_SHA=<approved-release-sha>
```

Create one held ledger task for the onboarding or recovery. Release it only
when automatic dispatch is appropriate; host-only repair uses the separately
authorized break-glass path.

```console
mac task create "Onboard ${AGENT}" --project=mac --no-dispatch
```

Never put credentials, authenticated URLs, or raw secret-bearing output in the
task description, evidence, memory, or deploy manifest.

## Phase 1: registry and route preflight

- [ ] The node appears exactly once as `enabled: true` in
      `~/.mac/fleets.yaml`.
- [ ] Its `target`, `ssh_jump`, `identity_file`, known-hosts source, OS, and
      supervisor describe the current host rather than a retired predecessor.
- [ ] The hub URL is reachable from the node's network namespace. A host-local
      URL is rewritten to the OpenShell host bridge only when the sandbox policy
      also permits that exact host and port.
- [ ] The route resolves portably and does not silently depend on
      `~/.ssh/config` or an interactive agent:

```console
mac --json admin fleet ssh-spec \
  --fleet "$FLEET" --agent "$AGENT" --portable
```

- [ ] A non-interactive SSH probe uses `BatchMode=yes`, strict host checking,
      the resolved identity, known-hosts file, and jump host. Never weaken host
      verification to get through onboarding.
- [ ] Long deploy SSH sessions have keepalives. Minimal SSH servers must use
      the supported SCP transport rather than assuming SFTP is present.
- [ ] If a setup spec exists, both validation stages pass before any deploy:

```console
mac admin fleet validate --spec ~/.mac/specs/${FLEET}.fleet.yaml
mac admin fleet doctor --spec ~/.mac/specs/${FLEET}.fleet.yaml
```

**Stop conditions:** unknown host key, non-portable route, ambiguous target,
unreachable hub, or a registry/live-host identity mismatch. Fix the registry or
network first; do not deploy to a guessed host.

## Phase 2: host and filesystem preflight

- [ ] Architecture and OS match the registry.
- [ ] The selected supervisor is real: `launchd` on the macOS hub, `systemd`
      on ordinary Linux hosts, or `supervisord` in init-less pods.
- [ ] The deploy user can control that supervisor non-interactively. A root-only
      supervisord socket requires passwordless `sudo supervisorctl`.
- [ ] Free bytes and inodes are checked for the home directory, MAC state, and
      container/OpenShell storage. Keep at least the configured worker GC high
      watermark; the default minimum is 10 GiB.
- [ ] `~/.mac/agent-workspaces` is bounded. The active worktree and recent keep
      set are protected, while completed workspaces age out.
- [ ] `MAC_WORKER_WORKSPACE_GC_ENABLED` is not disabled accidentally, and a
      `worker.workspace_gc.disk_low` warning is treated as a dispatch blocker.
- [ ] A pinned Python meeting the repository requirement is available. Fresh
      hosts use the deployer's checksum-reviewed `uv 0.8.22` native asset to
      provision exact Python `3.12.11` instead of inheriting an old base-image
      Python. The reviewed asset matrix covers Linux amd64/arm64 and Darwin
      x86_64/arm64; an unknown OS/architecture or SHA-256 mismatch stops deploy.
- [ ] `git`, `gh`, `codegraph`, the selected coding CLIs, container runtime, and
      OpenShell prerequisites are present in the worker's service PATH, not only
      an interactive login shell.
- [ ] CodeGraph `v1.5.0` is installed from its versioned native release archive
      after SHA-256 verification. No fleet credential-bearing process executes
      a downloaded installer script. Verified archives are cached under
      `~/.mac/cache/reviewed-assets`; checksum verification is repeated before
      reuse. Index initialization is asynchronous and bounded; it must not block
      heartbeats indefinitely.
- [ ] The crash observer is installed outside the MAC virtualenv and the native
      supervisor has restart enabled.

**Stop conditions:** low disk/inodes, failed workspace GC, unsupported Python,
unsupported tool OS/architecture, reviewed-asset download or checksum failure,
missing supervisor control, or a required tool visible only in an interactive
shell. Do not bypass a checksum failure with an ambient `curl | sh` installer.
A credential sync does not repair any of these failures.

## Phase 3: secrets and credential projection

- [ ] Secrets come from the encrypted vault or a mode-`0600` host/operator env
      file. Caller environment has documented precedence over env files.
- [ ] Only secret source names are logged. Secret values move over the resolved
      SSH route on stdin, never argv, task metadata, stdout, or the fleet YAML.
- [ ] The node receives an agent-scoped hub token, not the hub's admin token,
      database, secret-encryption key, or provider vault.
- [ ] Git-host credentials are projected into the actual worker or runner
      environment. A vault record by itself does not populate `GH_TOKEN`.
- [ ] Fleet deploy reuses an explicit deploy token, standard GitHub token env,
      or the operator's authenticated `gh` keychain in that precedence order.
      Pure workers fail before drain/source replacement when GitHub rejects the
      projected credential.
- [ ] `GH_TOKEN`, `GITHUB_TOKEN`, and Gitea equivalents enter OpenShell only
      through the private mode-`0600` sandbox environment bundle, never through
      `sandbox create --env`, the SSH command line, or copied host SSH keys.
- [ ] Coding credentials are synced from the workstation with the freshest
      interactive login only when the node report says they are needed:

```console
mac --json admin fleet creds-status
mac admin fleet creds-sync --fleet "$FLEET" --agent "$AGENT" --dry-run
mac admin fleet creds-sync --fleet "$FLEET" --agent "$AGENT"
```

- [ ] Environment-backed Codex auth wins over stale rotating file auth inside
      OpenShell. Do not upload a stale `~/.codex/auth.json` over a working
      `OPENAI_API_KEY` route.
- [ ] Repository-access learnings are inspected before retrying a failed clone.
      Repair the credential source or authorization and let a real success
      supersede the failure; never fabricate a success record.

```console
mac --json admin memory search \
  --record-type fleet_learning:repository_access \
  --order desc --limit 50
```

**Stop conditions:** secret in argv/logs, raw hub/admin material copied to a
spoke, auth failure repeatedly retried with the same pattern, or a repository
remote retaining an authenticated URL.

## Phase 4: deploy one node transactionally

- [ ] The intended revision has passed its repository contract and CodeGraph
      audit and is pushed to the canonical remote.
- [ ] The node is idle or intentionally held before replacement. Do not clear
      all fleet holds to repair one node.
- [ ] Deploy one node, then verify it before continuing:

```console
deploy/deploy-mac-fleet.sh --hub "$HUB" "$AGENT"
```

- [ ] The deploy selects the registry supervisor, installs the pinned source and
      venv, writes pre/post manifests, installs the crash observer, and creates
      `rollback-latest.sh`.
- [ ] The post manifest records the source revision, supervisor, role, gateway
      mode, model configuration, and success. Logs and handoff artifacts have
      restrictive permissions and contain no bearer token.
- [ ] A service restart is initiated by the deployer/supervisor or a detached
      process outside the worker's process group. Never issue a synchronous
      supervisor restart from the worker group being killed.
- [ ] Rollback is exercised or at least inspected before promotion:

```console
~/.mac/logs/rollback-latest.sh
```

Run the rollback only on the resolved target and only when rollback is actually
required; the command mutates the live host.

**Stop conditions:** deploy manifest missing, source install incomplete,
supervisor restart kills the only recovery process, startup self-test fails, or
the node returns on a different agent identity.

## Phase 5: source and registration convergence

- [ ] Exactly one stable agent row exists for the node. Re-registration updates
      that row rather than creating a duplicate identity.
- [ ] Heartbeats are fresh, health is `healthy`, and there is no unexplained
      dispatch hold or quarantine.
- [ ] Reported OS, architecture, hardware, capabilities, role, and OpenShell
      requirement match desired state.
- [ ] `resources.source_state.dirty` is false.
- [ ] `resources.source_state.commit_sha` equals `$EXPECTED_SHA`; a clean old
      checkout is still drift.
- [ ] The runtime/build digest is non-null and belongs to the expected release
      bucket:

```console
mac --json admin fleet snapshot
mac --json admin fleet build-distribution
mac --json agent list
```

- [ ] Startup source import, checkout SHA, and active installed runtime agree.
      A Git pull without venv/artifact/supervisor reconciliation is not an
      upgrade.
- [ ] Source-refresh and repo-update evidence is fresh. If it lags, redeploy or
      repair convergence rather than accepting a heartbeat from an old binary.

**Stop conditions:** null digest, dirty source, source/runtime SHA mismatch,
duplicate agent row, stale repo-update trail, or a hold that was inherited from
a failed rollout.

## Phase 6: attestation and publication trust

- [ ] The worker has an attestation key in its service environment and the hub
      verifies it.
- [ ] Missing or invalid keys may rotate during bootstrap. A valid key is not
      rotated merely because another check failed.
- [ ] A worker process that was alive during key rotation adopts and persists
      the new key, re-signs once, and retries submission once.
- [ ] A verdict signed before one rotation remains verifiable through the
      retained previous key. After two rotations, re-review is required rather
      than weakening signature verification.
- [ ] A code canary produces signed executor evidence, passes the detected test
      command and CodeGraph audit, pushes a branch, receives a distinct signed
      review verdict, publishes, and reaches `completed`.
- [ ] A report canary uses a substantive `operator_result`; it is not used to
      bypass a repository change contract.

Inspect every non-completing canary before retrying:

```console
mac task show <task-id>
mac task summary <task-id>
```

**Stop conditions:** signature rejection loop, repeated key rotation,
publication hot loop, phantom push evidence, stale branch conflict, missing new
files, dirty worktree, or a review signed by an untrusted/indistinct route.

## Phase 7: coding-route acceptance

For every installed CLI, keep these states distinct:

- `on_path`: the service can find the binary;
- `configured`: a supported credential source exists;
- `verified`: the exact provider/protocol/auth/endpoint/model fingerprint ran
  successfully inside the production OpenShell sandbox.

Checklist:

- [ ] At least one route is freshly `verified=true`.
- [ ] The verification fingerprint exactly matches the advertised route.
- [ ] Provider, wire protocol, endpoint, auth kind, and model are the intended
      values and contain no secret.
- [ ] Configured candidates are probed in priority order until one verifies. A
      broken preferred CLI is recorded as failed and must not shadow a working
      fallback.
- [ ] An explicit `MAC_CODING_AGENT=<cli>` pin remains strict. If that pinned
      route fails, the node fails closed rather than silently changing provider.
- [ ] A sandboxed canary reports the same selected route that the heartbeat
      advertised.
- [ ] `coding_agent_route_unverified` means route proof failed; it is not treated
      automatically as missing credentials. Inspect each per-route failure
      before running `creds-sync`.

**Stop conditions:** configured-but-unverified only, stale fingerprint,
endpoint reachable only from the host, preferred-route failure hiding a working
fallback, or dispatcher acceptance of a route the executor will not select.

## Role-specific checklists

Apply one of these to every enabled entry in `~/.mac/fleets.yaml`. Virtual
operator/reviewer identities that are not deployable registry entries are not
fleet nodes.

### Hub plus chat gateway

- [ ] Deploy the hub last, after every spoke has passed its canary.
- [ ] Control-plane health and authenticated ledger operations pass.
- [ ] The router is reachable locally and from every spoke/sandbox.
- [ ] Shared Qdrant and Firecrawl endpoints are bound to the intended mesh
      address, required, healthy, and reported by startup self-test.
- [ ] SQLite uses online backup rather than copying a live WAL database, or the
      configured Postgres HA contract is healthy.
- [ ] macOS uses launchd for the control plane, worker, gateway, and any required
      reverse tunnel. A GUI login session is not required.
- [ ] OpenClaw runs inside a Ready OpenShell sandbox, owns chat exclusively,
      and legacy gateway services are inactive.
- [ ] Channel, public identity, memory continuity, watchdog, crash observer, and
      startup self-test all pass live.
- [ ] OpenShell policy permits the host bridge alias for hub-local services.

### Linux edge chat gateway

- [ ] The strict ProxyJump route through the hub is portable and current.
- [ ] systemd controls the worker, crash observer, and OpenClaw gateway.
- [ ] The hub router, Qdrant, Firecrawl, artifact service, and AgentBus are
      reachable over the mesh; loopback is not copied from the hub config.
- [ ] OpenClaw is the exclusive channel owner inside OpenShell; legacy gateway
      services are inactive.
- [ ] Public identity, channel accounts, memory continuity, resource watchdog,
      and startup self-test pass.
- [ ] Reported CUDA architecture and memory match the host. Architecture-specific
      packages work on both x86_64 and arm64 where those edge types exist.
- [ ] Before replacing a legacy home, run the supported soul audit and preserve
      only intentional customizations:

```console
mac admin fleet soul-audit --fleet "$FLEET" --agent "$AGENT"
```

### Pure GPU worker in an init-less pod

- [ ] Registry sets `supervisor: supervisord`, `worker.mode: loop`, and
      `hermes.gateway_impl: none`.
- [ ] `worker.openshell_required` is explicitly true or the pure-worker default
      is visible in the generated deploy spec. Deploy automatically runs the
      OpenShell bootstrap with `--enable --fail-closed`; it does not assume a
      binary under ephemeral `~/.local` survived a pod replacement.
- [ ] `worker.github_credentials_required` is explicitly true or the
      pure-worker default is visible in the generated deploy spec. A fresh pod
      passes `gh auth status`, clone/fetch, and a temporary push/delete probe
      from both the service environment and a normal OpenShell sandbox.
- [ ] The route uses the declared bastion, strict known hosts, explicit identity,
      and in-cluster target. Never infer pod DNS from the agent name.
- [ ] Direct mesh reachability to the hub does not wait for an unnecessary
      reverse tunnel.
- [ ] `gateway_impl=none` skips all OpenClaw, Hermes, persona, Slack secret,
      home-channel, and gateway wrapper work. A pure worker touches no chat
      machinery during install or startup self-test.
- [ ] supervisord control works through the supported privilege path and
      `autorestart=true` is effective.
- [ ] The base image's Python version is irrelevant because deploy provisions a
      pinned interpreter with `uv` when necessary.
- [ ] OpenShell executor configuration, sandbox route, GPU inventory, CodeGraph,
      workspace GC, crash observer, lease renewal, and hub connectivity pass.
- [ ] Qdrant is not accidentally started as a per-worker mandatory hub service.
      On a high-core constrained container, any required Qdrant instance uses a
      sufficient `QDRANT_PIDS_LIMIT`.
- [ ] A transient hub timeout is wrapped and retried; it cannot kill lease
      renewal or another background thread silently.
- [ ] The worker can complete one report canary and one pushed/reviewed code
      canary without chat-gateway dependencies.

Repeat this complete pure-worker checklist independently for worker-1,
worker-2, and every additional worker entry. Success on one pod is evidence for
the rollout pattern, not proof for its peers.

## Fleet promotion and closeout

- [ ] Roll out in this order: one canary worker, remaining workers one at a time,
      edge gateways one at a time, hub last.
- [ ] Between nodes, verify snapshot, startup self-test, source SHA/digest,
      route verification, disk headroom, crash reports, and canary completion.
- [ ] `mac task ready` shows the expected eligible agent count; use
      `mac task why-unclaimed <task-id>` when it does not.
- [ ] `mac admin dispatch tick --limit 10` assigns only after all holds and route gates
      are intentionally clear.
- [ ] Repository refs reconcile cleanly:

```console
mac admin repo refs status
mac admin repo refs audit --repo .
```

- [ ] The onboarding task is closed with the evidence summary, and the canonical
      branch is pushed and up to date.
- [ ] Any unresolved failure becomes a new ledger task with the node, failure
      class, secret-free evidence pointers, and a concrete acceptance test.

## Failure-derived stop matrix

| Symptom | Likely class | Required response |
|---|---|---|
| SSH reaches an old host or wrong pod | stale fleet mapping | Stop; correct `~/.mac/fleets.yaml`, then resolve the route again. |
| Worker dies during its own restart | restart inside worker process group | Recover through the supervisor from outside the dead group; use detached/deferred restart. |
| Healthy heartbeat but old commit or null digest | source/runtime drift | Redeploy or transactionally reconcile venv, artifacts, supervisor, and runtime; heartbeat alone is insufficient. |
| Idle worker, `coding_agent_route_unverified` | route probe failure or disk exhaustion | Inspect per-route verification and disk/GC warnings; sync credentials only for a true missing-auth report. |
| Preferred CLI fails while another is configured | priority shadowing | Probe the next configured CLI; publish every attempted route and select the first verified route. |
| Workspaces consume the disk | missing/unwired lifecycle GC | Protect active/recent worktrees, prune completed workspaces, restore the free-space watermark, then re-probe routes. |
| Repeated signature rejection after deploy | in-memory attestation key drift | Let bounded self-heal adopt/persist the current key or restart once; do not rotate repeatedly. |
| Reviews stop publishing after rotation | verdict signed under previous key | Verify against the one retained previous key; after a second rotation, re-review. |
| Lease renewal silently stops after hub timeout | unwrapped transport error | Treat transient OSError/timeout as recoverable and guard every background loop. |
| Pure worker deploy asks for Slack/persona/gateway | role coupling | Set and honor `gateway_impl=none`; skip all chat setup and checks. |
| Fresh pure worker reports every coding route failed and `openshell` is absent | ephemeral runtime prerequisite missing | Treat `gateway_impl=none` as OpenShell-required, bootstrap it during deploy, keep the worker drained on failure, then repeat the sandbox sentinel. |
| Fresh pod rejects Python requirement | base-image Python bleed-through | Use the deployer's pinned `uv` interpreter. |
| Native uv or CodeGraph bootstrap reports a SHA-256 mismatch | corrupt cache, incomplete download, or upstream asset drift | Keep the node drained, remove only the named file under `~/.mac/cache/reviewed-assets`, retry once, and investigate if the reviewed digest still differs. Never run the upstream installer script. |
| Native tool bootstrap rejects the OS or CPU | unsupported onboarding target | Add and review the exact versioned asset plus SHA-256 in `deploy/reviewed-tool-assets.sh`, with executable coverage, before onboarding that platform. |
| `supervisorctl` returns permission errors | root-only supervisord socket | Use the supported passwordless `sudo supervisorctl` path. |
| Qdrant panics spawning threads | PID cap too low on high-core host | Raise `QDRANT_PIDS_LIMIT`, redeploy, and require HTTP 200 readiness. |
| Hub-local sandbox request is cancelled | missing host-bridge egress | Allow the exact `host.openshell.internal:<port>` route for the required binaries. |
| Private clone/review auth repeats | stale or unauthorized Git credential | Inspect fleet learning, repair the named source/SSO scope, and prove with a real superseding success. |
| Deploy aborts on optional persona/gateway step | optional step treated as fatal | Make the step role-aware and non-fatal; mandatory worker installation must still complete. |
| Code work claims completion without push | evidence/finalizer failure | Run the contract and CodeGraph gates, add every new file, commit, push, review, and publish. |

## Historical evidence index

The following durable records motivated the gates above. Repeated repair-child
tasks with the same root failure are intentionally represented by their root
incident or durable fix rather than copied one by one.

| Area | Ledger evidence | Durable implementation evidence |
|---|---|---|
| Definitive target mapping and stopped-node recovery | `task_010fdac981b540ddbed31cf618def51f`, `task_06937c0d1f5343d09372097ed8955428` | fleet registry resolver and break-glass runbook |
| Host recovery and sandbox proof | `task_f930082d7bcd41cc8a37c77437faa817`, `task_33dd4ba320af4d29904fce96033cec5e` | scoped break-glass authorization |
| Workspace disk exhaustion | `task_1c57ae62113943cfbfb0f05f3601d3d6`, `task_02ebb6c460bf46aeada0fcbe8b9cf957` | `3a523c2` |
| Attestation churn and publication wedge | task history from the completion-stall incident | `62021be`, `f0715ee` |
| Transient hub failures killing workers | completion-stall incident history | `2f4cb20` |
| Pure-worker/chat-gateway coupling | fresh-worker provisioning failures | `86ab73d`, `a99d121` |
| Fresh pure-worker OpenShell missing after pod replacement | second canary-worker route-verification failure during the 2026-07-16 rollout | role-derived OpenShell bootstrap in fleet deploy |
| Old host Python and fatal optional persona | fresh-worker provisioning failures | `37ac857` |
| Vault projection and macOS reverse tunnel | new-worker deploy failures | `412f8a1` |
| OpenShell hub-local egress | continuity/peer-bridge failure | `c914e3e` |
| Long deploy and minimal SSH compatibility | deploy retry history | `64345b5`, `ec1ac04`, `c1c46eb` |
| Secret-safe deploy output and env precedence | fleet deploy hardening tasks | `20b26b6`, `46b2271` |
| Repository-access learning | `task_f16df80ee0b4404091fa9f86fcba64da` | `7cf4e3d`, `7503ba0` |
| Source refresh without runtime convergence | `task_980bad699b5d49a9b968ba646a8304a0` | source-state heartbeat and deploy manifest gates |
| OpenClaw staged rollout | `task_7ef13eeb54dc4b93b584e3eca39a7f13`, `task_aa3d4fb914a84b1e851336cdf7da6c51`, `task_0bcf59e661824d18aba7925bc65e014f`, `task_672d8e050ad04a3781faac8789a9eb63` | spoke-first, hub-last conformance contract |

Related operator references:

- [Production deployment](production-deployment.md)
- [Coding-CLI credentials and model selection](coding-cli-credentials.md)
- [Fleet operational learning](fleet-operational-learning.md)
- [Break-glass host recovery](break-glass-host-recovery.md)
- [Crash diagnosis and autonomous repair](crash-diagnosis-and-repair.md)
