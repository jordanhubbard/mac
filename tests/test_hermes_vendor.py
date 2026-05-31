"""Tests for the vendored-Hermes bootstrap (ADR 0001, hu-02/hu-03).

These verify the sys.path bootstrap contract. When no snapshot is vendored
(e.g. a lean checkout), the import-dependent assertions skip rather than fail.
"""

import sys

import pytest

from mac import hermes_vendor


def test_ensure_on_path_raises_when_not_vendored(monkeypatch, tmp_path):
    # Point VENDOR_DIR at an empty dir to simulate "not vendored".
    monkeypatch.setattr(hermes_vendor, "VENDOR_DIR", str(tmp_path / "_hermes"))
    assert hermes_vendor.is_vendored() is False
    with pytest.raises(RuntimeError):
        hermes_vendor.ensure_on_path()


@pytest.mark.skipif(not hermes_vendor.is_vendored(), reason="no vendored Hermes snapshot present")
def test_vendored_snapshot_is_importable():
    pin = hermes_vendor.snapshot_pin()
    assert pin and len(pin) >= 12

    d = hermes_vendor.ensure_on_path()
    assert d.endswith("_hermes")
    assert d in sys.path
    # Idempotent: a second call must not add a duplicate path entry.
    before = sys.path.count(d)
    hermes_vendor.ensure_on_path()
    assert sys.path.count(d) == before

    # Hermes' flat top-level packages import unchanged from the vendored tree.
    import hermes_constants  # noqa: F401

    import hermes_cli.runtime_provider as rp

    assert hasattr(rp, "resolve_runtime_provider")
