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
3. Define a profile-port operation between interfaces (identity/soul, memory,
   home-channel bindings) — the piece that does not exist yet in either
   direction.
4. Establish a re-vendor cadence for Hermes and a digest-bump cadence for
   OpenClaw, so "both supported" does not decay into "both stale".
5. Gateway readiness coverage for both arms, so a broken interface fails in CI
   rather than on a host.

## References

* `docs/hermes-retirement-premises.md` — why the migration was halted
* `docs/home-consolidation.md` — the four-homes analysis and target
* `deploy/hermes/LOCAL_PATCHES.md` — the Hermes patch set
* `deploy/openclaw/patches/UPSTREAM-ISSUE-stuck-session-recovery.md`
