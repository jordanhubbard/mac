# Controller Changeset-Adoption Core Spec

Status: specification only. No feature code is implemented by this document.
It establishes ground truth for the parent implementation task
"Implement controller changeset-adoption core: binding + attestation" so the
follow-up change is scoped, testable, and consistent with the existing
work-graph control plane.

## Ground truth captured

- The contract gate `scripts/run-contract-tests.sh` collects cleanly on the
  task base: `.venv/bin/python -m pytest --collect-only -q` reports
  `9019 tests collected` with zero collection errors. The parent feature does
  not break collection; it simply does not exist yet.
- No `changeset-adoption` module, schema, or test exists anywhere in the repo
  (a case-insensitive scan for `changeset.adoption` / `adopt_changeset` /
  `adopted_changeset` across `*.py` and `*.md` returns nothing).
- The publication pipeline already models the surrounding records in
  `src/mac/models.py`: `WorkPackageIntegrationBatch` (exact candidate identity),
  `WorkPackageCertification` (exact-candidate verification), and the fenced
  landing records `WorkPackageLandingStream` / `WorkPackageLandingIntent` /
  `WorkPackageLandingAttempt` / `WorkPackageLandingReceipt`. Landing itself is
  implemented in `src/mac/landing_service.py`.
- Signed proofs are produced by `sign_verification_manifest` /
  `verify_verification_manifest_signature` in `src/mac/services.py` (HMAC-SHA256,
  `v1:` base64url tag over a canonicalized manifest). Deployment-side attestation
  precedent lives in `src/mac/deployment_attestation.py`.

## Where the core sits

An "adopted changeset" is an exact candidate SHA that has been landed on a
controller-owned target ref and proven by a `WorkPackageLandingReceipt`
(`recovered`/`observed_sha` prove the remote compare-and-swap outcome). The
changeset-adoption core is the deterministic controller step that runs after a
receipt is recorded and before the package advances: it durably **binds** that
adopted changeset to controller-owned target state, and emits a signed
**attestation** of the adoption so downstream consumers can trust the binding
without re-reading the remote.

This core actuates; it does not decide. It refuses (fails closed) unless an
exact landing receipt, certification, and stream fence already authorize the
adoption. It never pushes, never mutates the remote, and never invents a second
queue.

## Binding

Binding associates one adopted changeset to the controller-owned target and
advances that target's durable adoption state exactly once.

- New durable record `WorkPackageAdoptionBinding` (dataclass in
  `src/mac/models.py`) with schema constant
  `WORK_PACKAGE_ADOPTION_BINDING_SCHEMA = "mac.work_package.adoption_binding.v1"`.
  Fields: `id`, `package_id`, `plan_version`, `epoch`, `repository_id`,
  `target_ref`, `candidate_sha`, `batch_id`, `certification_id`, `intent_id`,
  `receipt_id`, `landing_base_sha`, `adopted_sha` (== the receipt `observed_sha`),
  `stream_fence`, `binding_digest`, `bound_by`, `created_at`.
- The binding is append-only and idempotent per
  `(repository_id, target_ref, candidate_sha, stream_fence)`. Re-adopting the
  same receipt returns the existing binding; a stale stream fence is rejected.
- Store surface: `record_work_package_adoption_binding(...)` and
  `get_work_package_adoption_binding(...)` on `Store` (and both backends), plus
  a `work_package_adoption_bindings` table/DDL mirroring the landing-record
  migrations.

## Attestation

Attestation is the signed, verifiable proof that a specific changeset was
adopted and bound.

- New module `src/mac/changeset_adoption.py` exposing:
  - `ADOPTION_ATTESTATION_SCHEMA = "mac.work_package.adoption_attestation.v1"`.
  - `build_adoption_attestation(binding, *, receipt, certification) -> dict`
    that assembles the canonical attestation manifest (schema, package identity,
    repository/target, `candidate_sha`, `adopted_sha`, `landing_base_sha`,
    `certification_id`, `receipt_id`, `binding_digest`, `bound_by`, timestamp).
  - `sign_adoption_attestation(key, manifest) -> str` delegating to
    `sign_verification_manifest`.
  - `verify_adoption_attestation(key, manifest, signature) -> bool` delegating to
    `verify_verification_manifest_signature`.
  - `AdoptionAttestationError` for fail-closed contract violations
    (missing receipt, fence mismatch, candidate/observed SHA disagreement).
- New durable record `WorkPackageAdoptionAttestation` in `src/mac/models.py`
  (`id`, `binding_id`, `package_id`, `repository_id`, `target_ref`,
  `candidate_sha`, `attestation_digest`, `signature`, `signed_by`, `created_at`)
  with store surface `record_work_package_adoption_attestation(...)` /
  `get_work_package_adoption_attestation(...)`.

## Controller entry point

Add `adopt_changeset(...)` to the changeset-adoption core (a small service class
`ChangesetAdoptionController` in `src/mac/changeset_adoption.py`, disabled by
default like landing). Given a recorded `WorkPackageLandingReceipt` it:

1. Loads the receipt, its intent, attempt, certification, and stream; verifies
   the stream fence is still held and `observed_sha == candidate_sha` for a
   successful landing (or the receipt's recovery outcome for a recovered one).
2. Records the `WorkPackageAdoptionBinding` idempotently.
3. Builds, signs, and records the `WorkPackageAdoptionAttestation`.
4. Returns `(binding, attestation)`; raises `AdoptionAttestationError` /
   `LandingLeaseLostError` on any fence or identity mismatch without side effects.

## Files to create or modify

- Create `src/mac/changeset_adoption.py` (core + controller + attestation).
- Modify `src/mac/models.py` (two dataclasses + two schema constants).
- Modify `src/mac/store.py` (record/get methods + table DDL + migration) and the
  SQLite/Postgres backends behind it.
- Create `tests/test_changeset_adoption.py` (the proof set below).
- Modify `mkdocs.yml` nav (this spec) and regenerate
  `docs/reference/documentation-inventory.md` via
  `scripts/generate-docs-reference.py --write`.

## Exact test set that proves the core

`tests/test_changeset_adoption.py` must contain:

1. `test_adopt_changeset_binds_receipt_to_target` — a successful receipt yields
   a binding with `adopted_sha == observed_sha == candidate_sha`.
2. `test_adopt_changeset_is_idempotent_per_stream_fence` — re-adopting the same
   receipt returns the identical binding id (no duplicate row).
3. `test_adopt_changeset_rejects_stale_stream_fence` — a superseded stream fence
   raises `LandingLeaseLostError` and writes nothing.
4. `test_adopt_changeset_rejects_candidate_sha_mismatch` — receipt/candidate SHA
   disagreement raises `AdoptionAttestationError`.
5. `test_adopt_changeset_disabled_by_default` — the controller fails closed
   unless explicitly enabled.
6. `test_build_adoption_attestation_manifest_shape` — manifest carries the schema
   constant and every required identity field.
7. `test_sign_and_verify_adoption_attestation_roundtrip` — a signed attestation
   verifies under the same key.
8. `test_verify_adoption_attestation_rejects_tampered_manifest` — mutating any
   manifest field fails verification.
9. `test_verify_adoption_attestation_rejects_wrong_key` — a different key fails
   verification.
10. `test_binding_digest_is_content_addressed` — `binding_digest` is a stable
    function of the bound identity fields.
11. `test_store_roundtrips_binding_and_attestation` — records persist and reload
    equal across the store backend.
12. `test_recovered_receipt_binds_recovery_outcome` — a `recovered=True` receipt
    binds the recovered `observed_sha` rather than failing closed.

These twelve tests, all passing on top of the existing 9019-test gate with zero
new collection errors, are the completion proof for the parent implementation
task.
