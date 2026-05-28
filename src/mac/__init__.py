"""Multi-agent coordinator control plane."""

from mac.services import ControlPlane
from mac.store import SQLiteStore, Store, StoreError, make_store_from_env

__all__ = [
    "ControlPlane",
    "SQLiteStore",
    "Store",
    "StoreError",
    "make_store_from_env",
]

try:  # psycopg is an optional install ('mac[postgres]')
    from mac.store_postgres import PostgresStore  # noqa: F401
except ImportError:  # pragma: no cover - exercised when extra is absent
    pass
else:
    __all__.append("PostgresStore")
