"""Unit tests for the MAC_TEST_DISABLE_GROUPS namespace switch.

The behaviour lives in ``tests/conftest.py``'s ``pytest_collection_modifyitems``
hook: it auto-tags the big filename clusters with marker "namespaces" and then
*deselects* any namespace the operator switched off via MAC_TEST_DISABLE_GROUPS.
We load the real conftest module by path and drive the hook with lightweight
fakes so the assertions exercise the shipped logic, not a copy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_CONFTEST_PATH = Path(__file__).resolve().parent / "conftest.py"


def _load_conftest():
    spec = importlib.util.spec_from_file_location("mac_conftest_under_test", _CONFTEST_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFTEST = _load_conftest()


class _FakeItem:
    """Minimal stand-in for a pytest Item: enough for the hook's needs."""

    def __init__(self, fspath: str) -> None:
        self.fspath = fspath
        self._markers: list = []

    def add_marker(self, mark) -> None:
        self._markers.append(mark)

    def iter_markers(self):
        # ``pytest.mark.<name>`` decorators expose ``.name`` just like Mark.
        return list(self._markers)

    @property
    def marker_names(self) -> set[str]:
        return {getattr(m, "name", str(m)) for m in self._markers}


class _FakeConfig:
    def __init__(self) -> None:
        self.deselected: list = []
        parent = self

        class _Hook:
            def pytest_deselected(self, items):
                parent.deselected.extend(items)

        self.hook = _Hook()


def _run_hook(items: list[_FakeItem], disabled: str | None, monkeypatch) -> _FakeConfig:
    if disabled is None:
        monkeypatch.delenv("MAC_TEST_DISABLE_GROUPS", raising=False)
    else:
        monkeypatch.setenv("MAC_TEST_DISABLE_GROUPS", disabled)
    config = _FakeConfig()
    CONFTEST.pytest_collection_modifyitems(config, items)
    return config


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/repo/tests/test_fleet_node_rollback_supervisor.py", "fleet"),
        ("/repo/tests/test_deploy_fleet_drain.py", "fleet"),
        ("/repo/tests/test_work_package_pipeline.py", "work_package"),
        ("/repo/tests/test_worker_credentials.py", "worker"),
        ("/repo/tests/test_full_rollout_e2e.py", "heavy_e2e"),
        ("/repo/tests/test_documentation_book.py", "heavy_e2e"),
    ],
)
def test_path_namespaces_are_auto_tagged(path, expected, monkeypatch):
    """Each cluster path gets its namespace marker even with no flag set."""
    item = _FakeItem(path)
    _run_hook([item], None, monkeypatch)
    assert expected in item.marker_names


def test_directory_markers_still_applied(monkeypatch):
    """The pre-existing api/cli/ui directory markers must survive the change."""
    api = _FakeItem("/repo/tests/api/test_api.py")
    cli = _FakeItem("/repo/tests/cli/test_cli_extended.py")
    ui = _FakeItem("/repo/tests/ui/test_dashboard.py")
    _run_hook([api, cli, ui], None, monkeypatch)
    assert "api" in api.marker_names
    assert "cli" in cli.marker_names
    assert "ui" in ui.marker_names


def test_no_flag_deselects_nothing(monkeypatch):
    items = [_FakeItem("/repo/tests/test_fleet_node.py")]
    config = _run_hook(items, None, monkeypatch)
    assert config.deselected == []
    assert len(items) == 1


def test_empty_flag_deselects_nothing(monkeypatch):
    items = [_FakeItem("/repo/tests/test_fleet_node.py")]
    config = _run_hook(items, "  ,  ", monkeypatch)
    assert config.deselected == []
    assert len(items) == 1


def test_disabled_namespace_is_deselected_and_removed(monkeypatch):
    fleet = _FakeItem("/repo/tests/test_fleet_node.py")
    other = _FakeItem("/repo/tests/test_work_package_pipeline.py")
    items = [fleet, other]
    config = _run_hook(items, "fleet", monkeypatch)

    assert fleet in config.deselected
    assert other not in config.deselected
    # Deselected items are physically removed from the run list (zero wall-clock).
    assert items == [other]


def test_multiple_namespaces_disabled_with_whitespace(monkeypatch):
    fleet = _FakeItem("/repo/tests/test_fleet_node.py")
    worker = _FakeItem("/repo/tests/test_worker_credentials.py")
    keep = _FakeItem("/repo/tests/test_work_package_pipeline.py")
    items = [fleet, worker, keep]
    config = _run_hook(items, " fleet , worker ", monkeypatch)

    assert set(config.deselected) == {fleet, worker}
    assert items == [keep]


def test_unknown_namespace_is_a_noop(monkeypatch):
    """An unmatched group name must not silently drop unrelated tests."""
    item = _FakeItem("/repo/tests/test_fleet_node.py")
    items = [item]
    config = _run_hook(items, "does_not_exist", monkeypatch)
    assert config.deselected == []
    assert items == [item]
