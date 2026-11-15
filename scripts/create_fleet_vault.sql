-- Fleet secret vault migration
-- Run as: psql -d agent_brain -f scripts/create_fleet_vault.sql
-- (Must be run by a superuser / table owner on the hub)

CREATE TABLE IF NOT EXISTS fleet_memory.agent_secrets (
    agent_name   TEXT        NOT NULL,
    service      TEXT        NOT NULL,
    key_name     TEXT        NOT NULL,
    ciphertext   BYTEA       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_name, service, key_name)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON fleet_memory.agent_secrets TO fleet_agent;

COMMENT ON TABLE fleet_memory.agent_secrets IS
  'Encrypted fleet secrets. Values are Fernet-encrypted using a key derived '
  'from MAC_WORKER_TOKEN via PBKDF2-SHA256. Raw Postgres access cannot decrypt.';
