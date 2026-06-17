"""Guard the per-CSP fleet-sample convention.

The repo must stay generic for ANY fleet owner. A real fleet's topology lives
OUTSIDE git in ~/.mac/specs/<fleet>.fleet.yaml; the repo ships only generic,
de-personalized, per-CSP samples under deploy/fleet/samples/. This test
codifies that principle so the jordanh-gke.fleet.yaml bleed-through (and any
future per-user fleet) can't come back.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
SAMPLES = DEPLOY / "fleet" / "samples"

# Per-user / operator identity that must never appear in a checked-in sample.
# Real agent names, hosts, internal DNS, email domains, and bare IPv4 addresses
# are all placeholders only ("<...>").
IDENTITY = re.compile(
    r"jordanh|jkh|rocky|natasha|bullwinkle|\bhorde\b|ov-agent-farm"
    r"|nvidia\.com|\b\d{1,3}(\.\d{1,3}){3}\b"
)

# Generic, non-identifying network constants the IPv4 rule must NOT flag:
# loopback (127.0.0.0/8) and the unspecified bind address (0.0.0.0). These are
# CSP-meaningful structure (loopback service URLs, reverse-tunnel port maps,
# control-plane bind host), not anyone's real host address.
_GENERIC_IPS = re.compile(r"\b(?:127(?:\.\d{1,3}){3}|0\.0\.0\.0)\b")


def _strip_generic_ips(line: str) -> str:
    return _GENERIC_IPS.sub("", line)


def _checked_in_fleet_yamls() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "deploy"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in out.stdout.splitlines() if line.endswith(".fleet.yaml")]


def test_every_checked_in_fleet_yaml_is_a_sample_under_fleet_dir():
    yamls = _checked_in_fleet_yamls()
    assert yamls, "expected at least one checked-in *.fleet.yaml sample"
    for path in yamls:
        rel = path.relative_to(ROOT)
        # Must live under deploy/fleet/ (the samples area).
        assert str(rel).startswith("deploy/fleet/"), (
            "%s is a checked-in fleet config outside deploy/fleet/ — per-fleet "
            "specs belong outside git in ~/.mac/specs/" % rel
        )
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), "%s must be a YAML mapping" % rel
        assert data.get("sample") is True, "%s must be marked sample: true" % rel


def test_samples_carry_no_operator_identity():
    yamls = _checked_in_fleet_yamls()
    offenders: list[str] = []
    for path in yamls:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if IDENTITY.search(_strip_generic_ips(line)):
                offenders.append("%s:%d: %s" % (path.relative_to(ROOT), lineno, line.strip()))
    assert not offenders, "operator/per-fleet identity leaked into samples:\n" + "\n".join(offenders)


def test_jordanh_gke_bleed_through_is_gone():
    assert not (DEPLOY / "jordanh-gke.fleet.yaml").exists(), (
        "deploy/jordanh-gke.fleet.yaml is a per-operator fleet bleed-through and "
        "must not be checked in; its shape lives in deploy/fleet/samples/gke.fleet.yaml"
    )


def test_gke_sample_exists_and_is_placeholder_only():
    gke = SAMPLES / "gke.fleet.yaml"
    assert gke.is_file(), "expected deploy/fleet/samples/gke.fleet.yaml"
    data = yaml.safe_load(gke.read_text(encoding="utf-8"))
    assert data.get("sample") is True
    assert data.get("schema") == "mac.fleet_setup.v1"
    assert data.get("hub") == "gke-hub"


def test_list_samples_includes_gke():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "setup-fleet.py"), "--list-samples"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "gke" in result.stdout


def test_init_from_copies_sample_to_specs_dir(tmp_path):
    specs_dir = tmp_path / "specs"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--init-from",
            "gke",
            "--name",
            "my-gke",
            "--specs-dir",
            str(specs_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    dest = specs_dir / "my-gke.fleet.yaml"
    assert dest.is_file(), "expected --init-from to copy the sample to %s" % dest
    data = yaml.safe_load(dest.read_text(encoding="utf-8"))
    assert data.get("sample") is True
    assert data.get("hub") == "gke-hub"

    # Refuses to clobber without --force.
    again = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "setup-fleet.py"),
            "--init-from",
            "gke",
            "--name",
            "my-gke",
            "--specs-dir",
            str(specs_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 2, again.stdout
