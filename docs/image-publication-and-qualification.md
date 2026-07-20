# Image Publication and Pre-Publication Qualification

Protected-main CI treats the controller revision and runtime image identity as
separate facts. A documentation-only or other non-image change does not rebuild
the MAC or OpenShell runtime image. Instead, CI computes a canonical digest of
the files and reviewed build arguments that can affect that image, resolves the
corresponding content tag, and reuses its exact OCI digest only after anonymous
label, platform, runtime, and GitHub provenance verification.

The owner-private `mac.image_publication_identity.v1` artifact binds:

- the requested controller revision and the revision that originally built the
  image;
- the canonical frozen-input digest and content tag;
- the immutable multi-platform OCI digest;
- anonymous execution evidence for `linux/amd64` and `linux/arm64`; and
- verified GitHub provenance for that exact digest and original build revision.

The reviewed OpenShell build-recipe inputs include its Containerfile, copied
source and lock files, fixed tool-version arguments, asset preparation script,
reviewed checksum registry, Bash contract, and pinned base-image references. Generated
`.mac-openshell-build-assets` are deliberately represented by their reviewed
generator, exact checksum registry, and arguments, so a reuse probe avoids the
downloads entirely. Change `mac.image_frozen_inputs.v1` whenever the
fingerprinting boundary or interpretation changes.

This recipe identity is not a claim of bit-for-bit reproducibility. Debian and
npm package resolution inside the reviewed Containerfile is not fully pinned
to immutable snapshots, so rebuilding the same recipe later can resolve a
different OCI digest. Reuse always preserves and verifies the already-built
immutable digest; the receipt records that digest and its original provenance
as authoritative. A future reproducible-build gate must pin or receipt those
resolved package materials before making a stronger claim.

Per-image workflow concurrency is serialized without cancellation. This keeps
two protected-main runs from racing to establish the same content tag while
retaining the exact immutable digest as the deployment identity.

## Local read-only fleet qualification

Before publishing a candidate source revision, an operator can run the fleet's
existing read-only preflight through the local wrapper:

```bash
install -d -m 0700 "$HOME/.mac/receipts"
scripts/prepublish-fleet-qualification.py \
  --hub rocky \
  --fleets-config "$HOME/.mac/fleets.yaml" \
  --output "$HOME/.mac/receipts/prepublication-$(git rev-parse HEAD).json" \
  bullwinkle natasha
```

The wrapper executes this deployment interface without a mutating phase:

```text
deploy/deploy-mac-fleet.sh --hub <hub> --preflight-only \
  --qualification-receipt <private-temporary-path> \
  --fleets-config <path> [agents...]
```

It requires a clean tracked worktree, exact `HEAD`, an owner-controlled deploy
script and fleet registry, fresh passing node evidence, canonical payload
digests, distinct endpoint identities, and an upstream receipt whose
`authorizes_deployment` value is false. The final receipt and its directory are
owner-private (`0600` and `0700`, respectively); command output is represented
only by bounded SHA-256 digests, and the temporary upstream receipt is removed.

This receipt is owner-private operator evidence produced immediately before a
push or publication; CI does not consume it automatically. This operator
evidence does not authorize deployment, open a hub epoch, quiesce workers, or replace
protected-main certification. Any source revision, fleet registry, selection,
endpoint identity, or freshness mismatch requires a new qualification.
