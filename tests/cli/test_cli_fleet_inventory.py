"""`mac admin fleet inventory`: the operator's view of the one host list."""

from __future__ import annotations

import io
import sys

import pytest

from mac.cli import main
from tests.test_fleet_inventory import INVENTORY


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        try:
            rc = main(list(args))
        except SystemExit as exc:
            rc = exc.code
    finally:
        sys.stdout = old
    return rc, out.getvalue()


def test_inventory_plan_lists_every_entry_with_its_disposition(tmp_path, monkeypatch):
    path = tmp_path / "fleets.yaml"
    path.write_text(INVENTORY)
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(path))

    rc, out = _run(tmp_path, "admin", "fleet", "inventory", "--fleets-config", str(path))

    assert rc in (0, None)
    for name, disposition in (
        ("natasha", "managed"),
        ("gone", "retired"),
        ("hgx-1", "external"),
        ("other-worker", "other_fleet"),
    ):
        line = next(line for line in out.splitlines() if line.strip().startswith(name))
        assert disposition in line


def test_an_inconsistent_inventory_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "fleets.yaml"
    path.write_text(INVENTORY.replace("  other:\n", "    - {name: twin, target: x@10.0.0.1}\n  other:\n"))

    rc, _ = _run(tmp_path, "admin", "fleet", "inventory", "--fleets-config", str(path))

    assert rc == 2
    assert "ssh host 10.0.0.1" in capsys.readouterr().err
