"""Small validation and formatting edges for fleet deployment helpers."""

from __future__ import annotations

import pytest

from mac.fleet_deploy import SshTarget, parse_ssh_target, shell_words


def test_ssh_target_properties_and_shell_words() -> None:
    target = SshTarget("operator@hub", port=2222)
    assert target.ssh_target == "operator@hub"
    assert target.scp_target_prefix == "operator@hub"
    assert shell_words(["ssh", target.ssh_target]) == "ssh operator@hub"


@pytest.mark.parametrize(
    ("value", "port", "message"),
    [
        (" ", None, "required"),
        ("operator@hub", 0, "positive"),
    ],
)
def test_parse_ssh_target_rejects_invalid_values(value, port, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_ssh_target(value, port=port)
