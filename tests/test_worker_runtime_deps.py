"""Tests for the agent's version-aware probe+install dependency reconciliation.

`ensure_pip` must install/upgrade only the (name, version) tuples that are
missing or out-of-range — not blindly skip a present-but-stale package (the old
name-only behavior) and not reinstall an already-satisfied one. The declarative
`REQUIRED_RUNTIME_PIP` manifest is reconciled at agent-lifecycle startup so a
fresh or stale node self-converges to the right versions on demand.
"""

from __future__ import annotations

import pytest

from mac.worker import REQUIRED_RUNTIME_PIP, MacWorker


class _DummyLock:
    def close(self) -> None:  # matches the file-handle returned by _install_lock
        pass


def _worker() -> MacWorker:
    """A bare MacWorker with the heavy/side-effecting bits stubbed."""
    w = object.__new__(MacWorker)
    w._agent_venv_python = lambda: "python3"
    w._install_lock = lambda: _DummyLock()
    w._update_footprint = lambda *a, **k: None
    return w


# -- version-aware probe ----------------------------------------------------


@pytest.mark.parametrize(
    "spec,installed,expected",
    [
        ("nemo-relay==0.3.0", {"nemo-relay": "0.3.0"}, True),   # exact match
        ("nemo-relay==0.3.0", {"nemo-relay": "0.2.0"}, False),  # present but stale -> upgrade
        ("nemo-relay==0.3.0", {}, False),                       # absent -> install
        ("nemo-relay>=0.3.0", {"nemo-relay": "0.3.1"}, True),   # range satisfied
        ("nemo-relay>=0.3.0", {"nemo-relay": "0.2.9"}, False),  # range not met
        ("requests", {"requests": "2.0"}, True),                # no pin -> presence is enough
        ("requests", {}, False),                                # no pin, absent
        ("Nemo_Relay==0.3.0", {"nemo-relay": "0.3.0"}, True),   # name normalization
    ],
)
def test_pip_spec_satisfied(spec, installed, expected):
    assert MacWorker._pip_spec_satisfied(spec, installed) is expected


# -- ensure_pip installs only the deltas ------------------------------------


def test_ensure_pip_skips_when_all_satisfied():
    w = _worker()
    w._pip_installed = lambda py: {"nemo-relay": "0.3.0"}
    called = []
    w._run_install = lambda argv, **k: called.append(argv) or {"ok": True}
    res = w.ensure_pip(["nemo-relay==0.3.0"])
    assert res.get("skipped") == "already satisfied"
    assert called == []  # nothing installed when already satisfied


def test_ensure_pip_installs_only_unsatisfied():
    w = _worker()
    # nemo-relay present but stale -> reinstall; requests satisfied -> skip
    w._pip_installed = lambda py: {"nemo-relay": "0.2.0", "requests": "2.31.0"}
    captured = {}
    w._run_install = lambda argv, **k: captured.update(argv=argv) or {"ok": True}
    res = w.ensure_pip(["nemo-relay==0.3.0", "requests"])
    assert res.get("ok")
    assert "nemo-relay==0.3.0" in captured["argv"]
    assert "requests" not in captured["argv"]


def test_ensure_pip_installs_when_absent():
    w = _worker()
    w._pip_installed = lambda py: {}
    captured = {}
    w._run_install = lambda argv, **k: captured.update(argv=argv) or {"ok": True}
    res = w.ensure_pip(["nemo-relay==0.3.0"])
    assert res.get("ok")
    assert "nemo-relay==0.3.0" in captured["argv"]


# -- lifecycle reconcile uses the manifest ----------------------------------


def test_reconcile_runtime_deps_uses_manifest():
    w = _worker()
    seen = {}
    w.ensure_pip = lambda specs, **k: seen.update(specs=specs, reason=k.get("reason")) or {
        "ok": True,
        "skipped": "already satisfied",
    }
    res = w.reconcile_runtime_deps()
    assert res.get("ok")
    assert seen["specs"] == list(REQUIRED_RUNTIME_PIP)
    assert "nemo-relay==0.3.0" in seen["specs"]


def test_required_runtime_manifest_contains_relay():
    assert any(s.startswith("nemo-relay==") for s in REQUIRED_RUNTIME_PIP)
