"""Validation tests for the NemoClaw gateway pilot OpenShell compatibility check.

Task: task_dae5ccbd2986447898e231f0f8e0011d
Summary: Confirm NemoClaw's exact OpenShell requirement, compare it with MAC's
current fleet pin, and determine whether the pilot can proceed without bumping
the fleet pin.

Findings (as of 2026-07-05):
- NemoClaw requires OpenShell == 0.0.72 (documented in
  docs/security/openshell-0.0.72-compatibility-review.mdx).
- MAC's fleet pin was 0.0.72 at the time (advanced from 0.0.62, validated
  2026-07-04).

Update 2026-10-04: the fleet pin moved to OpenShell 0.1.2 (task
task_5f45058ee332407da79236b32e80c77c, worker canary 2026-10-03). Neither
NemoClaw nor OpenClaw runs anywhere, so NemoClaw's 0.0.72 requirement no longer
constrains the fleet pin; the historical review below stays as a record.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"
REVIEWED_ASSETS = ROOT / "deploy" / "openshell" / "reviewed-cli-assets.sh"
COMPAT_REVIEW = ROOT / "docs" / "security" / "openshell-0.0.72-compatibility-review.mdx"
NEMOCLAW_COMPOSE = ROOT / "deploy" / "nemoclaw" / "docker-compose.yaml"
NEMOCLAW_RUNBOOK = ROOT / "deploy" / "nemoclaw" / "RUNBOOK.md"
SANDBOX_DOC = ROOT / "docs" / "openshell-sandbox.md"


# ---------------------------------------------------------------------------
# 1. Confirm NemoClaw's OpenShell requirement
# ---------------------------------------------------------------------------


def test_nemoclaw_openshell_requirement_is_documented():
    """The compat-review doc records NemoClaw's exact OpenShell requirement."""
    text = COMPAT_REVIEW.read_text(encoding="utf-8")
    # NemoClaw requires == 0.0.72 (exact pin)
    assert "NemoClaw gateway pilot requires OpenShell == 0.0.72" in text


def test_nemoclaw_compose_enforces_openshell_required():
    """The NemoClaw docker-compose.yaml sets MAC_OPENSHELL_REQUIRED=1,
    meaning the pilot runs under OpenShell enforcement."""
    text = NEMOCLAW_COMPOSE.read_text(encoding="utf-8")
    assert 'MAC_OPENSHELL_REQUIRED: "1"' in text


# ---------------------------------------------------------------------------
# 2. Confirm MAC's current fleet pin
# ---------------------------------------------------------------------------


def test_mac_fleet_pin_is_0_1_2():
    """The centralized reviewed-asset registry pins the fleet at 0.1.2."""
    registry = REVIEWED_ASSETS.read_text(encoding="utf-8")
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'OPENSHELL_REVIEWED_CLI_VERSION="0.1.2"' in registry
    assert 'OPENSHELL_VERSION="${OPENSHELL_VERSION:-$OPENSHELL_REVIEWED_CLI_VERSION}"' in bootstrap
    assert '. "$OPENSHELL_ASSET_REGISTRY"' in bootstrap


def test_mac_openshell_reconcile_default_version_is_0_1_2():
    """The Python reconcile module's DEFAULT_OPENSHELL_VERSION matches the pin."""
    from mac.openshell_reconcile import DEFAULT_OPENSHELL_VERSION

    assert DEFAULT_OPENSHELL_VERSION == "0.1.2"


def test_mac_cli_default_openshell_version_is_0_1_2():
    """The CLI's --openshell-version default matches the fleet pin."""
    cli_src = (ROOT / "src" / "mac" / "cli.py").read_text(encoding="utf-8")
    assert '"--openshell-version"' in cli_src
    assert 'default="0.1.2"' in cli_src


def test_bootstrap_reviews_exactly_the_registry_version():
    """Bootstrap carries gateway digests only for the registry's version."""
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert "\n  0.1.2)\n" in bootstrap
    assert "\n  0.0.72)\n" not in bootstrap


# ---------------------------------------------------------------------------
# 3. Historical record of the 0.0.62 -> 0.0.72 advance
# ---------------------------------------------------------------------------


def test_mac_fleet_pin_was_advanced_from_0_0_62():
    """Document the version history: 0.0.62 -> 0.0.72.

    The task description referenced MAC's 'current OpenShell 0.0.62 pin', which
    was accurate at task creation (2026-07-04). The pin was advanced to 0.0.72
    in the same cycle, validated in docs/security/openshell-0.0.72-compatibility-review.mdx.
    This test confirms 0.0.62 is the PRIOR pin (historical), not the current one.
    """
    text = COMPAT_REVIEW.read_text(encoding="utf-8")
    # Prior pin is documented as 0.0.62
    assert "Prior pin:** 0.0.62" in text
    # Current (candidate) pin is 0.0.72
    assert "Candidate pin:** 0.0.72" in text
    # Recommendation is to advance
    assert "Advance the fleet pin to 0.0.72" in text


def test_compat_review_all_surfaces_pass():
    """The compat review must document PASS on all three MAC sandbox surfaces."""
    text = COMPAT_REVIEW.read_text(encoding="utf-8")
    # All three surfaces must pass
    assert "Verdict:** **PASS** — no CLI surface change" in text  # Surface A
    assert "Verdict:** **PASS** — no interface change affecting" in text  # Surface B
    assert "Verdict:** **PASS** — gateway confinement behavior" in text  # Surface C


def test_existing_mac_policy_requires_no_adjustment_for_0_0_72():
    """The compat review documents that no policy adjustment is needed."""
    text = COMPAT_REVIEW.read_text(encoding="utf-8")
    assert "Policy Adjustments Required" in text
    assert "**None.**" in text


def test_sandbox_doc_records_0_0_72_validation():
    """docs/openshell-sandbox.md records the 0.0.72 validation outcome."""
    text = SANDBOX_DOC.read_text(encoding="utf-8")
    assert "OpenShell 0.0.72 compatibility" in text
    assert "validated 2026-07-04" in text


# ---------------------------------------------------------------------------
# 4. Risk and rollback documentation is present
# ---------------------------------------------------------------------------


def test_rollback_condition_is_documented_in_compat_review():
    """A documented rollback path must exist in case 0.0.72 causes issues."""
    text = COMPAT_REVIEW.read_text(encoding="utf-8")
    assert "Rollback Condition" in text
    # Rollback involves repinning to 0.0.62
    assert "OPENSHELL_VERSION=0.0.62" in text


def test_nemoclaw_pilot_coexistence_notes_present():
    """The sandbox doc records coexistence notes for the NemoClaw pilot."""
    text = SANDBOX_DOC.read_text(encoding="utf-8")
    assert "NemoClaw single-host pilot observations" in text
    assert "separate HERMES_HOME" in text or "separate Slack" in text


def test_nemoclaw_uses_separate_hermes_home():
    """NemoClaw must use a dedicated HERMES_HOME, not the existing gateway's."""
    compose = NEMOCLAW_COMPOSE.read_text(encoding="utf-8")
    runbook = NEMOCLAW_RUNBOOK.read_text(encoding="utf-8")
    # docker-compose sets HERMES_HOME to the pilot path
    assert "hermes-nemoclaw" in compose
    # runbook documents the isolation
    assert "hermes-nemoclaw" in runbook
