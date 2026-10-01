-- Slim worker credentials to one hashed bearer token per worker.
-- `mac admin worker-token` issues, rotates, installs and lists those tokens;
-- the deploy protocol that pinned a credential to a source commit, runtime
-- digest and capability set, recorded install receipts, and flipped the fleet
-- between compatibility and enforced identity modes is gone. The hub always
-- ran in compatibility mode, which is now the only behaviour.
--
-- worker_credential_events was written but never read, and
-- worker_credential_policy_state only held the identity mode. The dropped
-- worker_credentials columns are the deploy-time pinning (fleet, environment,
-- expected_source_commit, expected_runtime_digest, required_capabilities,
-- package_capable) and the install destination. Every row keeps its id, agent, hash, fingerprint,
-- scopes, state, version, timestamps and supersession link, so active tokens
-- keep authenticating unchanged.
--
-- No CASCADE, as in 0004 to 0008: an object nobody declared here makes the
-- migration fail and roll back instead of being dropped silently. A column's
-- CHECK constraint goes with it. IF EXISTS keeps this a no-op for an object
-- that was never created.

DROP TABLE IF EXISTS worker_credential_events;
DROP TABLE IF EXISTS worker_credential_policy_state;

ALTER TABLE worker_credentials DROP COLUMN IF EXISTS fleet;
ALTER TABLE worker_credentials DROP COLUMN IF EXISTS environment;
ALTER TABLE worker_credentials DROP COLUMN IF EXISTS expected_source_commit;
ALTER TABLE worker_credentials DROP COLUMN IF EXISTS expected_runtime_digest;
ALTER TABLE worker_credentials DROP COLUMN IF EXISTS required_capabilities;
ALTER TABLE worker_credentials DROP COLUMN IF EXISTS package_capable;
ALTER TABLE worker_credentials DROP COLUMN IF EXISTS destination;
