"""Multi-agent coordinator control plane."""

from typing import TYPE_CHECKING

__all__ = [
    "ControlPlane",
    "Store",
    "StoreError",
    "make_store_from_env",
    "PostgresStore",
]

if TYPE_CHECKING:  # type-checkers / IDEs only — never imported at runtime
    from mac.services import ControlPlane
    from mac.store import Store, StoreError, make_store_from_env
    from mac.store_postgres import PostgresStore


def __getattr__(name: str):
    """Lazily re-export the heavy control-plane classes (PEP 562).

    Keeps ``import mac`` import-light so dependency-free submodules — notably
    ``mac.deploy_env``, which ``deploy/deploy-mac-fleet.sh`` runs via
    ``python -m mac.deploy_env`` on the bootstrap python *before* the deploy venv
    exists — don't transitively pull in ``mac.services`` and its third-party deps
    (yaml, cryptography, …). ``from mac import ControlPlane`` still works; it just
    imports ``mac.services`` on first access instead of at package import.
    """
    if name == "ControlPlane":
        from mac.services import ControlPlane

        return ControlPlane
    if name in {"Store", "StoreError", "make_store_from_env"}:
        import mac.store as _store

        return getattr(_store, name)
    if name == "PostgresStore":
        # psycopg is an optional install ('mac[postgres]'); raise the normal
        # AttributeError when the extra is absent so callers can fall back.
        from mac.store_postgres import PostgresStore

        return PostgresStore
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
