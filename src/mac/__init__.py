"""Multi-agent coordinator control plane."""

from typing import TYPE_CHECKING

__all__ = ["ControlPlane", "SQLiteStore"]

if TYPE_CHECKING:  # type-checkers / IDEs only — never imported at runtime
    from mac.services import ControlPlane
    from mac.store import SQLiteStore


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
    if name == "SQLiteStore":
        from mac.store import SQLiteStore

        return SQLiteStore
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
