"""
Fleet secret vault — encrypted secrets in shared Postgres.

Key derivation: HKDF-SHA256 from MAC_WORKER_TOKEN (present on every agent
machine via ~/.mac/mac.env).  Any fleet agent with the token can read any
secret; nothing is readable from raw Postgres access alone.

Usage:
    from mac.vault import vault_set, vault_get, vault_list, vault_delete

    vault_set("MACFleetNatasha", "clawxiv", "api_key", "clx_abc...")
    key = vault_get("MACFleetNatasha", "clawxiv", "api_key")
    secrets = vault_list("MACFleetNatasha")          # own secrets
    secrets = vault_list()                            # all fleet secrets
    vault_delete("MACFleetNatasha", "clawxiv", "api_key")

CLI:
    python -m mac.vault set  <agent> <service> <key> <value>
    python -m mac.vault get  <agent> <service> <key>
    python -m mac.vault list [agent]
    python -m mac.vault delete <agent> <service> <key>
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from typing import Optional

import psycopg2
from cryptography.fernet import Fernet

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------

_VAULT_KEY: Optional[bytes] = None


def _derive_vault_key() -> bytes:
    """Derive a 32-byte Fernet key from MAC_WORKER_TOKEN via HKDF-SHA256."""
    token = os.environ.get("MAC_WORKER_TOKEN", "")
    if not token:
        # Try loading from mac.env
        env_path = os.path.expanduser("~/.mac/mac.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MAC_WORKER_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break

    if not token:
        raise RuntimeError(
            "MAC_WORKER_TOKEN not set — cannot derive vault key. "
            "Source ~/.mac/mac.env first."
        )

    # HKDF-SHA256: salt = b"fleet-vault-v1", info = b"agent-secret-encryption"
    salt = b"fleet-vault-v1"
    info = b"agent-secret-encryption"
    prk = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, iterations=100_000)
    okm = hashlib.pbkdf2_hmac("sha256", prk, info, iterations=1)
    # Fernet requires URL-safe base64-encoded 32 bytes
    return base64.urlsafe_b64encode(okm[:32])


def _fernet() -> Fernet:
    global _VAULT_KEY
    if _VAULT_KEY is None:
        _VAULT_KEY = _derive_vault_key()
    return Fernet(_VAULT_KEY)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_PG_DSN = "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"


def _connect():
    dsn = os.environ.get("FLEET_PG_DSN", _PG_DSN)
    return psycopg2.connect(dsn)


_TABLE = "fleet_memory.agent_secrets"


def _ensure_table(cur) -> None:
    """No-op: table is created by the hub migration script. Kept for API compatibility."""
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def vault_set(agent_name: str, service: str, key_name: str, value: str) -> None:
    """Encrypt and store (or update) a secret."""
    ct = _fernet().encrypt(value.encode())
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(f"""
                INSERT INTO {_TABLE} (agent_name, service, key_name, ciphertext, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (agent_name, service, key_name)
                DO UPDATE SET ciphertext = EXCLUDED.ciphertext,
                              updated_at = now()
            """, (agent_name, service, key_name, ct))
        conn.commit()


def vault_get(agent_name: str, service: str, key_name: str) -> Optional[str]:
    """Retrieve and decrypt a secret. Returns None if not found."""
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(f"""
                SELECT ciphertext FROM {_TABLE}
                WHERE agent_name=%s AND service=%s AND key_name=%s
            """, (agent_name, service, key_name))
            row = cur.fetchone()
    if row is None:
        return None
    return _fernet().decrypt(bytes(row[0])).decode()


def vault_list(agent_name: Optional[str] = None) -> list[dict]:
    """List stored secrets (names only, never values)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_table(cur)
            if agent_name:
                cur.execute(f"""
                    SELECT agent_name, service, key_name, created_at, updated_at
                    FROM {_TABLE} WHERE agent_name=%s ORDER BY service, key_name
                """, (agent_name,))
            else:
                cur.execute(f"""
                    SELECT agent_name, service, key_name, created_at, updated_at
                    FROM {_TABLE} ORDER BY agent_name, service, key_name
                """)
            rows = cur.fetchall()
    return [
        {
            "agent": r[0], "service": r[1], "key": r[2],
            "created_at": str(r[3]), "updated_at": str(r[4])
        }
        for r in rows
    ]


def vault_delete(agent_name: str, service: str, key_name: str) -> bool:
    """Delete a secret. Returns True if it existed."""
    with _connect() as conn:
        with conn.cursor() as cur:
            _ensure_table(cur)
            cur.execute(f"""
                DELETE FROM {_TABLE}
                WHERE agent_name=%s AND service=%s AND key_name=%s
            """, (agent_name, service, key_name))
            deleted = cur.rowcount > 0
        conn.commit()
    return deleted


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _cli():
    import json
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]

    if cmd == "set" and len(args) == 5:
        vault_set(args[1], args[2], args[3], args[4])
        print(f"Stored {args[1]}/{args[2]}/{args[3]}")

    elif cmd == "get" and len(args) == 4:
        val = vault_get(args[1], args[2], args[3])
        if val is None:
            print("Not found", file=sys.stderr)
            sys.exit(1)
        print(val)

    elif cmd == "list":
        agent = args[1] if len(args) > 1 else None
        rows = vault_list(agent)
        if not rows:
            print("(empty)")
        else:
            print(json.dumps(rows, indent=2))

    elif cmd == "delete" and len(args) == 4:
        ok = vault_delete(args[1], args[2], args[3])
        print("Deleted." if ok else "Not found.")

    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    _cli()
