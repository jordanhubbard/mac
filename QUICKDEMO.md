# QUICKDEMO — Watership Down fleet, end to end

> **Live-demo note (2026-08-20).** `mac admin fleet connect` and
> `mac admin persona-instance` are NOT in the installed CLI yet (PR #541). This
> file uses only commands that exist today: `mac admin hermes register` for
> persona instances, and the two-command URL/token lookup in step 3.
>
> Step 6 needs the review-evidence fix, which landed on `main` as `531cb0fa`.
> A hub bootstrapped from `main` has it. A hub deployed before 2026-08-20
> does not, and strands tasks in `reviewing`.

A fungible demo: provision a fleet, staff it with personas, hand it a feature
request, and watch it land. Everything here is torn down at the end (step 8).

Read `AGENTS.md` and `CLAUDE.md` first. Two rules from them bite in this demo:
work happens in a git worktree, never in `~/Src/mac`; and issues live in the
`mac task` ledger, not in markdown.

---

## 0. Preconditions (check before an audience is watching)

```bash
hgx doctor                     # CLI + server access
hgx clusters                   # confirm gke-newhouse has free capacity
hgx gpus
mac task stats                 # existing hub reachable
```

**Cluster: `gke-newhouse`, explicitly.** `hgx create` defaults to
`--cluster dgxc-az27`, which was 387/464 used at last check; `gke-newhouse` was
0/372. Taking the default is how this demo fails at step 1.

---

## 1. Provision three nodes

The fleet is **Watership Down**. Hazel is the hub; Fiver and Bigwig are workers.

Disk is **not** a flag — it comes from `--profile`. That is the single most
common way this step goes wrong:

| Profile | PVC | Use |
|---|---|---|
| `lightweight` | 50Gi | Fiver, Bigwig |
| `kit-isaac` | 250Gi | *too small for Hazel* |
| `kit-isaac-debug` | 500Gi | Hazel — meets the ≥300GB requirement |

Only Hazel needs the space: Hazel carries the Postgres database. The workers
keep no durable state that matters here.

```bash
# Hub — 500Gi PVC, >=32Gi RAM
hgx create --name Hazel --cluster gke-newhouse \
  --profile kit-isaac-debug --memory 32Gi --gpu 0 \
  --wait --wait-for ssh --wait-timeout 900

# Workers — 50Gi is plenty
for rabbit in Fiver Bigwig; do
  hgx create --name "$rabbit" --cluster gke-newhouse \
    --profile lightweight --memory 32Gi --gpu 0 \
    --wait --wait-for ssh --wait-timeout 900
done
```

**On `--gpu 0`:** `gke-newhouse` is a GPU-slice cluster, so a zero-GPU request
may be refused. None of these nodes needs a GPU, and per the demo's intent a
GPU-bearing instance is acceptable — if `--gpu 0` is rejected, drop the flag and
take the default of 1 rather than hunting for a CPU-only cluster.

Confirm reachability before moving on:

```bash
hgx ssh Hazel -- hostname
hgx ssh Fiver -- hostname
hgx ssh Bigwig -- hostname
```

---

## 2. Bootstrap the mac stack on Hazel

Standard provisioning workflow — `skills/setup-mac-fleet/SKILL.md`:

```bash
bash setup.sh --fleets-config ~/.mac/fleets.yaml --env-file ~/.mac/.env
```

Answer: **hub**, fleet name `watership-down`, at least one LLM provider.

**Use the slug `watership-down`, not `Watership Down`.** Both resolve to the
same credential variable — the fleet name is normalised by
`re.sub(r"[^A-Za-z0-9]+", "_", …).upper()` (`src/mac/fleet_env.py:57`), so
either spelling yields `MAC_API_TOKEN__WATERSHIP_DOWN` — but the slug avoids
quoting `--fleet "Watership Down"` at every later call.

Then run `setup.sh` again per worker, answering **worker**, pointing at Hazel.

---

## 3. Hand over the URL and token

```bash
hgx ssh Hazel -- tailscale ip -4          # hub URL is http://<tailscale-ip>:8789
grep MAC_API_TOKEN ~/.mac/.env            # print the bearer token for the operator
```

Open the URL in a browser and print the token to the terminal.

---

## 4. Staff the warren

A persona belongs to a **tenant**, and the demo's tenant does not exist until
you make it. Do this first — both persona commands below take `tenant_id` as a
required positional, and there is no default:

```bash
mac admin tenant register watership-down --tenant-id tenant_watership-down
mac admin tenant list                     # read the id back; ids look like tenant_<name>
```

Everything in this step uses `tenant_watership-down` as `<tenant_id>`.

Every agent runs **OpenClaw**. A personality is a `SOUL.md` file — one of the
three identity documents OpenClaw carries (`SOUL.md`, `USER.md`, `MEMORY.md`,
`src/mac/human_interface_profile.py:58`). `SOUL.md` is the personality; the
other two are the operator profile and accumulated memory.

Write each soul into the agent's OpenClaw identity directory, which is
`$MAC_HOME/openclaw/workspace` (`src/mac/mac_paths.py:116`):

```bash
hgx ssh Hazel -- 'mkdir -p ~/.mac/openclaw/workspace && cat > ~/.mac/openclaw/workspace/SOUL.md' <<'SOUL'
# Hazel

Chief Rabbit by consent, not by force. You lead a warren that followed you
because you were right, not because you outrank anyone.

You decide by listening. When someone else has the better idea you take it and
say whose it was. You take the risk yourself rather than spending someone else
on it. You are not the strongest or the most gifted rabbit here and you know
it — your job is to see the whole warren and keep it together.
SOUL
```

Fiver — Hazel's smaller brother, who has prophetic visions; his warning that
the original warren will be destroyed starts the whole journey. Bigwig — a
large, powerful former Owsla officer, the group's best fighter, and Hazel's
most important lieutenant. Write their souls the same way, onto their own
nodes.

Then register each one. `soul_ref` is the **path to that SOUL.md** and
`memory_scope` is the home that contains it (`src/mac/hermes_runtime.py:570`):

```bash
mac admin persona register tenant_watership-down hazel \
  --soul-ref ~/.mac/openclaw/workspace/SOUL.md \
  --memory-scope ~/.mac/openclaw
mac admin hermes register tenant_watership-down hazel --persona-id <persona_id>
mac agent register <machine_id> hazel --persona-instance-id <instance_id>
```

Repeat for `fiver` and `bigwig`.

**Two naming traps in front of an audience.** `mac persona` and `mac hermes`
both answer *"moved under `admin`"* — type `mac admin …` directly. And the
persona-instance command is still called `hermes` even though nothing here runs
Hermes; it registers a generic persona instance.

## 5. Register artlab

```bash
mac project register git@github.com:jordanhubbard/artlab.git --project artlab
```

**`register` also files a contract-authoring task**, and that onboarding task
must finish before the fidget-spinner task can be claimed. Watch it land before
step 6, or the demo appears to hang on a queue that is in fact working as
designed:

```bash
mac task list --project artlab
```

---

## 6. The fidget spinner, watched through the pipeline

```bash
cd ~/Src/artlab
mac task create "Add a fidget spinner demo to artlab" \
  --description-file=- --project artlab <<'EOF'
Add a fidget spinner demo to artlab: a spinner the user can flick with the
pointer, which spins up and coasts to a stop under friction.
EOF
```

Watch it:

```bash
watch -n 10 'mac task show <task_id> | tail -30'
mac task list --project artlab --state=reviewing
```

> **Release dependency.** The review stage strands tasks on `main` before
> PR #537: the hub captured only the last 2000 bytes of a verification run,
> which for a failing gate is coverage output and `ssh exited with status 1` —
> never the reason. Rejections were classified as a dead harness, no verdict
> was signed, and tasks retried forever in `reviewing`. Six did exactly that
> for six hours on 2026-08-20. **Bootstrap Hazel from a build containing #537**,
> or this step — the climax of the demo — is the failure it is meant to show off
> surviving.

---

## 7. Start artlab

artlab is a Vite app:

```bash
cd ~/Src/artlab && npm install && npm run dev
```

Hand over the printed URL (Vite defaults to `http://localhost:5173`).

---

## 8. Tear down

```bash
for rabbit in Hazel Fiver Bigwig; do hgx delete "$rabbit"; done
hgx list
```

`hgx stop` keeps the PVC; `hgx delete` frees it. This demo is fungible — delete.

---

## Known gaps in this script

1. **Worker bootstrap (step 2)** — the wizard is interactive; a fully scripted
   run needs its answers pre-seeded.
2. **The mesh may not come up at all (steps 1–3)** — `gke-newhouse` pods have
   no `/dev/net/tun` and no `NET_ADMIN`, so `tailscaled` cannot open a TUN
   device and step 3's `tailscale ip -4` has nothing to print. Tracked as
   `task_4ca2519c…`; a userspace-networking fallback for
   `deploy/install-tailscale.sh` is in flight and has not landed. Until it
   does, confirm the hub is reachable by a non-mesh route before the demo.

`tenant_id` and #537 are no longer gaps: step 4 now registers the tenant, and
#537 merged on 2026-08-20 as `531cb0fa`, so any hub bootstrapped from `main`
after that date carries the review-evidence fix.
