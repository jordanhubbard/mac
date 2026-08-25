"""Contract tests for fleet deploy output and log permission hardening.

Covers:
- SshTarget validation and formatting
- write_owner_only_file() atomicity, permissions (0o600), and encoding
- ensure_owner_only_directory() creation, nesting, permission tightening, idempotency
- cleanup_retention_plan() entries (mac logs, acc deploy, /tmp)
- cleanup_path_strings() pipe-delimited format and count consistency
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from mac.fleet_deploy import (
    SshTarget,
    canonicalize_mesh_ssh_target,
    cleanup_path_strings,
    cleanup_retention_plan,
    ensure_owner_only_directory,
    is_cleanup_protected_path,
    parse_ssh_target,
    phase_failure_evidence_dir,
    preserved_cleanup_paths,
    shell_words,
    write_owner_only_file,
)


MESH_STATUS = {
    "BackendState": "Running",
    "Self": {
        "HostName": "operator",
        "DNSName": "operator.example.ts.net.",
        "TailscaleIPs": ["100.64.0.1", "fd7a:115c:a1e0::1"],
    },
    "Peer": {
        "node-key": {
            "HostName": "puck",
            "DNSName": "puck.example.ts.net.",
            "TailscaleIPs": ["100.72.16.110", "fd7a:115c:a1e0::2"],
        }
    },
}


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


def test_mesh_target_replaces_only_local_mdns_and_preserves_user_and_port() -> None:
    assert (
        canonicalize_mesh_ssh_target(
            "jkh@puck.local:2201",
            provider="tailscale",
            status=MESH_STATUS,
        )
        == "jkh@100.72.16.110:2201"
    )
    assert (
        canonicalize_mesh_ssh_target(
            "puck.local",
            provider="headscale",
            status=MESH_STATUS,
        )
        == "100.72.16.110"
    )
    assert (
        canonicalize_mesh_ssh_target(
            "jkh@host.example.com:2201",
            provider="none",
        )
        == "jkh@host.example.com:2201"
    )
    assert (
        canonicalize_mesh_ssh_target(
            "jkh@192.0.2.10",
            provider="none",
        )
        == "jkh@192.0.2.10"
    )


def test_mesh_target_rejects_local_mdns_without_a_resolvable_mesh_peer() -> None:
    with pytest.raises(ValueError, match="configure tailscale/headscale"):
        canonicalize_mesh_ssh_target("puck.local", provider="none")
    with pytest.raises(ValueError, match="no tailscale/headscale peer"):
        canonicalize_mesh_ssh_target(
            "missing.local",
            provider="tailscale",
            status=MESH_STATUS,
        )


# ---------------------------------------------------------------------------
# write_owner_only_file()
# ---------------------------------------------------------------------------


def test_write_owner_only_file_creates_file_with_correct_content(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    write_owner_only_file(target, "hello world")
    assert target.read_text() == "hello world"


def test_write_owner_only_file_sets_mode_0o600(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    write_owner_only_file(target, "data")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_write_owner_only_file_creates_parent_directory(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "secret.txt"
    assert not target.parent.exists()
    write_owner_only_file(target, "nested content")
    assert target.exists()
    assert target.read_text() == "nested content"


def test_write_owner_only_file_atomicity_no_leftover_tempfile(tmp_path: Path) -> None:
    target = tmp_path / "atomic.txt"
    write_owner_only_file(target, "atomic data")
    # Only the target file should exist; no stray temp files
    remaining = list(tmp_path.iterdir())
    assert remaining == [target], f"unexpected extra files: {remaining}"


def test_write_owner_only_file_overwrites_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "overwrite.txt"
    target.write_text("old content")
    write_owner_only_file(target, "new content")
    assert target.read_text() == "new content"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_write_owner_only_file_respects_encoding(tmp_path: Path) -> None:
    target = tmp_path / "unicode.txt"
    text = "café résumé naïve"
    write_owner_only_file(target, text, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------
# ensure_owner_only_directory()
# ---------------------------------------------------------------------------


def test_ensure_owner_only_directory_creates_with_mode_0o700(tmp_path: Path) -> None:
    target = tmp_path / "private_dir"
    ensure_owner_only_directory(target)
    assert target.is_dir()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o700, f"expected 0o700, got {oct(mode)}"


def test_ensure_owner_only_directory_creates_nested_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    ensure_owner_only_directory(target)
    assert target.is_dir()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o700


def test_ensure_owner_only_directory_tightens_existing_wide_permissions(tmp_path: Path) -> None:
    target = tmp_path / "loose_dir"
    target.mkdir(mode=0o777)
    # Sanity-check the setup
    assert stat.S_IMODE(target.stat().st_mode) != 0o700
    ensure_owner_only_directory(target)
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o700, f"expected 0o700 after tightening, got {oct(mode)}"


def test_ensure_owner_only_directory_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "idempotent_dir"
    ensure_owner_only_directory(target)
    ensure_owner_only_directory(target)  # second call must not raise
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o700


# ---------------------------------------------------------------------------
# cleanup_retention_plan() / cleanup_path_strings()
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path):
    """Return (home, mac_home) rooted in tmp_path for isolation."""
    home = tmp_path / "home" / "user"
    mac_home = home / ".mac"
    return home, mac_home


def test_cleanup_retention_plan_includes_mac_logs_at_30_days(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    plan = cleanup_retention_plan(home, mac_home)
    paths = {str(item.path): item for item in plan}
    mac_logs_key = str(mac_home / "logs")
    assert mac_logs_key in paths, "~/.mac/logs must appear in the retention plan"
    assert paths[mac_logs_key].retain_days == 30


def test_cleanup_retention_plan_includes_acc_deploy_at_14_days(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    plan = cleanup_retention_plan(home, mac_home)
    paths = {str(item.path): item for item in plan}
    acc_deploy_key = str(home / ".acc" / "deploy")
    assert acc_deploy_key in paths, "~/.acc/deploy must appear in the retention plan"
    assert paths[acc_deploy_key].retain_days == 14


def test_cleanup_retention_plan_includes_tmp_at_2_days(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    plan = cleanup_retention_plan(home, mac_home)
    tmp_entries = [item for item in plan if str(item.path) == "/tmp"]
    assert tmp_entries, "/tmp must appear in the retention plan"
    assert tmp_entries[0].retain_days == 2


def test_cleanup_path_strings_produces_pipe_delimited_format(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    strings = cleanup_path_strings(home, mac_home)
    assert strings, "cleanup_path_strings must return at least one entry"
    for entry in strings:
        parts = entry.split("|")
        assert len(parts) == 3, f"expected 3 pipe-delimited fields, got: {entry!r}"
        path_str, reason, days_str = parts
        assert path_str.strip(), "path field must not be empty"
        assert reason.strip(), "reason field must not be empty"
        assert days_str.isdigit(), f"retain_days must be a digit string, got: {days_str!r}"


def test_cleanup_path_strings_count_matches_retention_plan(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    plan = cleanup_retention_plan(home, mac_home)
    strings = cleanup_path_strings(home, mac_home)
    assert len(strings) == len(plan), (
        f"cleanup_path_strings returned {len(strings)} entries but "
        f"cleanup_retention_plan has {len(plan)}"
    )


# ---------------------------------------------------------------------------
# phase-failure evidence preservation through cleanup
# ---------------------------------------------------------------------------


def test_phase_failure_evidence_dir_lives_under_mac_home(tmp_path: Path) -> None:
    _, mac_home = _make_paths(tmp_path)
    evidence_dir = phase_failure_evidence_dir(mac_home)
    assert evidence_dir == mac_home / "phase-failure-evidence"
    assert evidence_dir.parent == mac_home


def test_preserved_cleanup_paths_includes_evidence_dir(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    preserved = preserved_cleanup_paths(home, mac_home)
    assert phase_failure_evidence_dir(mac_home) in preserved


def test_evidence_dir_is_not_in_cleanup_retention_plan(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    plan_paths = {str(item.path) for item in cleanup_retention_plan(home, mac_home)}
    assert str(phase_failure_evidence_dir(mac_home)) not in plan_paths


def test_is_cleanup_protected_path_matches_evidence_dir(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    evidence_dir = phase_failure_evidence_dir(mac_home)
    assert is_cleanup_protected_path(evidence_dir, home, mac_home)


def test_is_cleanup_protected_path_matches_nested_evidence_file(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    evidence_file = phase_failure_evidence_dir(mac_home) / "node-01.json"
    assert is_cleanup_protected_path(evidence_file, home, mac_home)


def test_is_cleanup_protected_path_rejects_unrelated_path(tmp_path: Path) -> None:
    home, mac_home = _make_paths(tmp_path)
    assert not is_cleanup_protected_path(mac_home / "logs", home, mac_home)
    assert not is_cleanup_protected_path(home / ".acc" / "deploy", home, mac_home)


def test_protection_guard_wins_even_when_a_retention_root_covers_evidence(
    tmp_path: Path,
) -> None:
    # Root mac_home directly under a swept cleanup root (/tmp) so a retention
    # entry would otherwise cover the evidence dir. The protection guard must
    # still shield it — that is the whole point of preserving failure evidence
    # through cleanup.
    mac_home = tmp_path / ".mac"
    home = tmp_path
    evidence_dir = phase_failure_evidence_dir(mac_home)
    covering_roots = [
        item.path
        for item in cleanup_retention_plan(home, mac_home)
        if _is_under(evidence_dir, item.path)
    ]
    # Regardless of whether any retention root happens to cover it, the guard
    # protects the evidence dir and its contents.
    assert is_cleanup_protected_path(evidence_dir, home, mac_home)
    assert is_cleanup_protected_path(evidence_dir / "node.json", home, mac_home)
    # And a sibling generated dir under the same covering root is NOT protected.
    for root in covering_roots:
        sibling = root / "some-generated-artifact"
        if sibling != evidence_dir and not _is_under(sibling, evidence_dir):
            assert not is_cleanup_protected_path(sibling, home, mac_home)


def _is_under(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True
