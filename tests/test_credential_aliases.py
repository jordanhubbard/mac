from __future__ import annotations

from pathlib import Path

import pytest

from mac.credential_aliases import CredentialAliasError, canonical_value, convert, main
from mac.fleet_env import parse_env_file


def _write(path: Path, text: str) -> None:
    path.write_text("MAC_FLEET_NAME=rocky\n" + text, encoding="utf-8")


def test_convert_alias_only_to_canonical_with_backup(tmp_path: Path):
    path = tmp_path / "mac.env"
    _write(path, "MAC_WORKER_TOKEN__ROCKY=secret-value\n")

    result = convert(path)

    assert result["changed"] is True
    assert parse_env_file(path)["MAC_WORKER_TOKEN"] == "secret-value"
    assert "MAC_WORKER_TOKEN__ROCKY" not in parse_env_file(path)
    backup = Path(str(result["backup"]))
    assert backup.is_file()
    assert "MAC_WORKER_TOKEN__ROCKY=secret-value" in backup.read_text(encoding="utf-8")


def test_convert_equal_canonical_and_alias_removes_alias(tmp_path: Path):
    path = tmp_path / "mac.env"
    _write(path, "MAC_WORKER_TOKEN=same\nMAC_WORKER_TOKEN__ROCKY=same\n")

    assert convert(path)["changed"] is True
    assert parse_env_file(path)["MAC_WORKER_TOKEN"] == "same"
    assert "MAC_WORKER_TOKEN__ROCKY" not in parse_env_file(path)


def test_convert_different_canonical_and_alias_refuses_without_change(tmp_path: Path):
    path = tmp_path / "mac.env"
    _write(path, "MAC_WORKER_TOKEN=canonical\nMAC_WORKER_TOKEN__ROCKY=alias\n")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(CredentialAliasError, match="canonical variable is MAC_WORKER_TOKEN"):
        convert(path)

    assert path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.bak-credential-aliases-*"))


def test_convert_already_canonical_is_noop_without_backup(tmp_path: Path):
    path = tmp_path / "mac.env"
    _write(path, "MAC_WORKER_TOKEN=canonical\n")

    assert convert(path) == {"changed": False, "path": str(path), "converted": []}
    assert not list(tmp_path.glob("*.bak-credential-aliases-*"))


def test_converter_output_never_prints_secret(tmp_path: Path, capsys):
    path = tmp_path / "mac.env"
    _write(path, "MAC_WORKER_TOKEN__ROCKY=do-not-print-me\n")

    assert main(["--convert", "--env-file", str(path)]) == 0

    output = capsys.readouterr()
    assert "do-not-print-me" not in output.out + output.err
    assert "sha256:" in output.out


def test_reader_refuses_alias_and_names_canonical_variable():
    with pytest.raises(CredentialAliasError, match="canonical variable MAC_WORKER_TOKEN"):
        canonical_value(
            "MAC_WORKER_TOKEN",
            fleet="rocky",
            environ={"MAC_WORKER_TOKEN__ROCKY": "secret-value"},
        )


def test_reader_accepts_only_canonical_value():
    assert canonical_value("MAC_WORKER_TOKEN", environ={"MAC_WORKER_TOKEN": "value"}) == "value"
