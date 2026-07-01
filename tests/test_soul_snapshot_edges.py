from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mac import soul_snapshot as snapshot


def _result(returncode: int, stdout: str = "", stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_ssh_transport_read_and_fallback_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = snapshot.SSHTransport(ssh_extra=["-v"])
    assert transport._argv("host", "echo ok")[-2:] == ["host", "echo ok"]
    assert transport._remote("SOUL.md") == '"$HOME/.hermes/SOUL.md"'

    replies = iter([_result(7), _result(2, stderr="denied"), _result(0, "soul")])
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *args, **kwargs: next(replies))
    assert transport.read_text("host", "SOUL.md") is None
    with pytest.raises(RuntimeError, match="denied"):
        transport.read_text("host", "SOUL.md")
    assert transport.read_text("host", "SOUL.md") == "soul"


def test_ssh_transport_backup_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = snapshot.SSHTransport()
    replies = iter(
        [
            _result(2, stderr="backup denied"),
            _result(0),
            _result(0, "COPIED\n"),
            _result(3, stderr="write denied"),
            _result(0),
        ]
    )
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *args, **kwargs: next(replies))
    with pytest.raises(RuntimeError, match="backup denied"):
        transport.backup("host", "USER.md", stamp="T")
    assert transport.backup("host", "USER.md", stamp="T") is None
    assert transport.backup("host", "USER.md", stamp="T") == "USER.md.bak.T"
    with pytest.raises(RuntimeError, match="write denied"):
        transport.write_text("host", "USER.md", "text")
    transport.write_text("host", "USER.md", "text")


def test_ssh_transport_stat_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = snapshot.SSHTransport()
    replies = iter(
        [
            _result(7),
            _result(4, stderr="stat denied"),
            _result(0, "12 34 abc123\n"),
            _result(0, "malformed\n"),
        ]
    )
    monkeypatch.setattr(snapshot.subprocess, "run", lambda *args, **kwargs: next(replies))
    assert transport.stat("host", "state.db") is None
    with pytest.raises(RuntimeError, match="stat denied"):
        transport.stat("host", "state.db")
    assert transport.stat("host", "state.db", checksum=True) == {
        "present": True,
        "bytes": 12,
        "mtime": 34,
        "sha256": "abc123",
    }
    assert transport.stat("host", "state.db") == {"present": True}


def test_plain_conversion_and_push_result_filter() -> None:
    class Dictish:
        def to_dict(self) -> dict[str, int]:
            return {"x": 1}

    class IterablePairs:
        def __iter__(self):
            return iter([("y", 2)])

    opaque = object()
    assert snapshot._as_plain(Dictish()) == {"x": 1}
    assert snapshot._as_plain(IterablePairs()) == {"y": 2}
    assert snapshot._as_plain(opaque) is opaque
    result = snapshot.PushResult(
        changes=[
            snapshot.FileChange("a", "h", "SOUL.md", "new"),
            snapshot.FileChange("a", "h", "USER.md", "unchanged"),
        ]
    )
    assert [item.relpath for item in result.to_apply] == ["SOUL.md"]


def test_plan_push_skips_missing_local_snapshot(tmp_path: Path) -> None:
    class Transport:
        def read_text(self, *args: Any) -> str:
            raise AssertionError("missing local file must not read remote")

    result = snapshot.plan_and_push(
        tmp_path,
        {
            "agents": {
                "agent": {
                    "target": "host",
                    "files": {"SOUL.md": {"present": True}},
                }
            }
        },
        Transport(),
        stamp="T",
    )
    assert result.changes == []
