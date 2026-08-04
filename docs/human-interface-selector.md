# The human interface: support both, activate one

Status: design note, 2026-08-04. Describes a model the deploy already mostly
implements, and what remains to make it explicit.

## The term

Hermes and OpenClaw do the same job: they interface with human users and with
the plugin ecosystems built during the era of human-to-service maps. Call that
role the **human interface**. MAC is the control plane; the human interface is
how people talk to it and how it reaches services people already use.

Naming the role matters because it reframes the question. "Hermes or OpenClaw"
sounds like a migration. "Which human interface is active on this agent"
sounds like configuration — which is what it always was.

## The model

Both human interfaces are **supported and deployable**. Exactly **one is
active per agent**. Agents may differ from one another.

```yaml
gateway_impl: hermes | openclaw | none
```

`none` is a pure worker with no human interface at all.

## This is not a new architecture

Three facts, verified 2026-08-04:

**The selector already has this shape.** `gateway_impl` was always a
three-valued choice. The 2026-07-26 change (`ab7a5020`) did not simplify an
architecture; it deleted one arm of a switch that still exists.

**Mutual exclusion is already enforced.** `install_linux_no_gateway_service`
and its Darwin counterpart iterate `OPENCLAW_SERVICE_NAME`,
`HERMES_SERVICE_NAME` **and** `NEMOCLAW_SERVICE_NAME`, proving each inactive
and disabled. The restored `install_*_hermes_service` functions go further:
they call `darwin_set_auxiliary_restore` on the OpenClaw and NemoClaw plists so
that switching interfaces is a bounded launchd transaction with rollback, not a
stop-and-hope. The "never two at once" property the model depends on was built
in from the start.

**Home consolidation is already half-done, in the right direction.** Per
`docs/home-consolidation.md`, OpenClaw's real home is `~/.mac/openclaw/`;
`~/.openclaw` is only a container symlink, and `.nemoclaw` is an env-var prefix
rather than a directory. The single holdout is the legacy `~/.hermes` sibling.
Once that moves under `~/.mac/hermes/`, "port the profile between interfaces"
becomes a local operation under one root rather than a cross-home migration.

## Why keep both rather than pick one

**The premises that justified picking one were false.** All three — that MAC
had superseded Hermes' learning capability, that OpenClaw needed no patches,
that OpenClaw would run as `nemoclaw` in a constrained OpenShell sandbox — were
measured false on 2026-08-04. See `docs/hermes-retirement-premises.md`. A
system that can hold both options does not have to be right the first time.

**Neither is currently tracking upstream ToT.** Both are roughly two months
behind:

| interface | pin | dated |
|---|---|---|
| Hermes | `NousResearch/hermes-agent` @ `b1a25404` | vendored 2026-05-31 |
| OpenClaw | `ghcr.io/openclaw/openclaw:2026.6.11@sha256:3814fb…` | released 2026-06-11 |

So "we migrated to OpenClaw, therefore we are current" is not true today. Both
codebases move quickly, and staleness is a property of our update cadence, not
of which one we chose.

**The maintenance models are asymmetric, and that is the real cost.**

*OpenClaw* is an image pin: an immutable multi-arch digest of an official
release plus one local patch (`patch-stuck-session-recovery.py`, filed upstream
as openclaw#105586). Updating is a digest bump and a rebuild.

*Hermes* is a source vendor: 12 patches and 7 overlay files re-applied over a
pristine upstream clone. Updating means re-vendoring at a newer commit and
resolving patch conflicts. That is more expensive — but far better tooled than
a typical fork. `scripts/vendor-hermes-snapshot.sh` reproduces the tree
**byte-for-byte**, `SNAPSHOT_PIN` records the exact upstream base,
`LOCAL_PATCHES.md` documents every patch's purpose, and
`tests/test_hermes_vendor_integrity.py` pins a content digest so drift — a hand
edit or a re-vendor — fails loudly instead of silently.

That paragraph was an assertion no build checked, and on 2026-08-04 it was
found to be **false**: two patches could not apply, so `--apply` aborted and
the tree could not be regenerated from its inputs at all. It is true again, and
now enforced rather than claimed — the `hermes-revendor` CI job performs the
reproduction on every build and fails if the result differs. The lesson
generalises past Hermes: a maintenance-cost argument resting on tooling nobody
exercises is worth what the exercise is worth.

## What carrying both actually costs

Honestly, not nothing:

* two deploy paths in `fleet-node-install.sh`, both needing to keep working;
* two sets of gateway readiness tests;
* a re-vendor cadence for Hermes that does not currently exist;
* the 594-file vendored tree in the repository.

The cheapest reduction available is to **shrink the Hermes patch set before
the next re-vendor**. Of the twelve: `zz-public-api-docstrings.patch` is
documentation-only, and `fts5-orphan-schema-recovery.patch` and
`remove-duplicate-top-level-skills.patch` fix upstream bugs and belong
upstream. Every patch removed is conflict surface removed from every future
re-vendor.

## What this model buys

**Per-agent comparison instead of assertion.** The learning question that
started this — does the human interface's self-training beat MAC's — is
answerable by running Hermes on one host and OpenClaw on another and measuring
the output. That is exactly the experiment that could not be run once one side
was deleted.

**Plugin ecosystems stay reachable.** The vendored tree carries 25 skill
families. Whether they are worth keeping is a usage question that has not been
measured; deleting them answers it by fiat.

**Reversibility.** The migration was halted only because nothing had been
deleted yet. Keeping both preserves that property for the next decision.

## What remains

1. Restore the Hermes service installers — done 2026-08-04 from `dbb25ad0^`.
2. Move `~/.hermes` under `~/.mac/hermes/` per `docs/home-consolidation.md`, so
   profile porting is intra-root.
3. Define a profile-port operation between interfaces — done 2026-08-04 as
   `src/mac/human_interface_profile.py`, both directions. See below.
4. Establish a re-vendor cadence for Hermes and a digest-bump cadence for
   OpenClaw, so "both supported" does not decay into "both stale".
5. Gateway readiness coverage for both arms, so a broken interface fails in CI
   rather than on a host.
6. **Fix the multi-Slack patch for `slack_bolt` 1.27 — this blocks activating
   Hermes anywhere.** The patch builds each account's app as
   `AsyncApp(token=bot_token)`; 1.27.0 rejects that with `signing_secret must
   not be empty` at construction, verified directly against the installed
   library on the hub. Socket Mode has no inbound HTTP request to verify, so
   the fix is `request_verification_enabled=False`, re-vendored through
   `scripts/vendor-hermes-snapshot.sh`. This is the staleness risk in this
   very document arriving in practice: a pinned source fork drifting against
   an unpinned dependency.

## Porting a profile between interfaces

The rule: **whichever interface the agent used last holds the freshest
configuration**, so it is the source, and it is ported before switching.

Both interfaces are **multi-account**. They differ only in encoding:

| | account list | encoding |
|---|---|---|
| Hermes | `~/.hermes/slack_accounts.json` | JSON array of `{name, bot_token, app_token}` — one `AsyncApp` and one websocket each, from `multi-slack-mvp.patch`. Flat `SLACK_BOT_TOKEN` is a single-account fallback used only when the file is absent. |
| OpenClaw | namespaced env | `MAC_OPENCLAW_SLACK_<ACCOUNT>_{BOT,APP}_TOKEN`, with `MAC_OPENCLAW_SLACK_ACCOUNT_ID` naming the default |

Two properties matter more than the translation itself:

**Accounts are a union, never a collapse.** The source wins for accounts it
has; an account known only to the *target* is carried through untouched. An
earlier draft of this module modelled Hermes as single-account and would have
written only the active workspace into the flat keys — silently dropping
`offtera` while reporting success. Both hosts carry both workspaces today.

**Identity documents are preserved, not overwritten.** Where both sides have a
document and they differ, the destination is kept and the incoming version is
written alongside as `<file>.incoming` for a human to reconcile. On the hub,
`USER.md` and `MEMORY.md` are genuinely disjoint — neither is a superset — so
any last-writer-wins copy would destroy knowledge.

**No signing secret is required.** Socket Mode carries no inbound HTTP
request, so there are no signatures to verify. `SLACK_SIGNING_SECRET` appears
in no `~/.hermes/.env` backup going back to 2026-05-13, and Hermes served both
workspaces for months without one. The port reports it as *not required*
rather than *missing*, so a complete port is not misread as a failed one.

## References

* `docs/hermes-retirement-premises.md` — why the migration was halted
* `docs/home-consolidation.md` — the four-homes analysis and target
* `deploy/hermes/LOCAL_PATCHES.md` — the Hermes patch set
* `deploy/openclaw/patches/UPSTREAM-ISSUE-stuck-session-recovery.md`
