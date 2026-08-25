"""The script-job home untangle (task_c1265181e22448c89c1dbbfd91ece11f).

One process used to READ from the Hermes home (``~/.hermes/scripts``) and WRITE
to the OpenClaw home (``~/.mac/openclaw/script-jobs/output``), so a job's code
and its output lived in different trees and Hermes-named artefacts piled up
under OpenClaw. These tests are the end-state guard:

* no live runtime path of the host cron runner resolves into a Hermes home;
* the one path that still names it is the explicitly read-only fallback, which
  never wins over the sanctioned home and can be switched off;
* the runner's and relocator's stdlib-only *mirrors* of ``mac.mac_paths`` agree
  with the real resolver, so the contract cannot drift silently;
* the relocator moves and archives idempotently, verifies by digest, and refuses
  rather than guesses when the tree is ambiguous.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from mac import mac_paths

ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_DIR = ROOT / "deploy" / "openclaw"
RUNNER_PATH = OPENCLAW_DIR / "run-script-cron-job.py"
RELOCATOR_PATH = OPENCLAW_DIR / "relocate-script-job-home.py"
INSTALLER = OPENCLAW_DIR / "install-openclaw-gateway.sh"

# Every env knob that can move a home, so a guard can start from a clean slate.
_HOME_ENV = [
    "MAC_HOME",
    "HERMES_HOME",
    "MAC_OPENCLAW_HOST_DIR",
    "MAC_OPENCLAW_SCRIPT_JOB_SCRIPTS_DIR",
    "MAC_OPENCLAW_SCRIPT_JOB_OUTPUT_DIR",
    "MAC_OPENCLAW_LEGACY_SCRIPTS_DIR",
    "MAC_HERMES_SCRIPTS_DIR",
]


def _load(path: Path, name: str):
    """Import a hyphenated deploy script from its path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load(RUNNER_PATH, "run_script_cron_job_home")
relocator = _load(RELOCATOR_PATH, "relocate_script_job_home")


@pytest.fixture()
def clean_home(tmp_path, monkeypatch):
    for var in _HOME_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# The decision: one home per artefact class                                     #
# --------------------------------------------------------------------------- #
def test_script_job_artefacts_share_one_home(clean_home):
    """Scripts and output resolve into the SAME directory, not two homes."""
    home = mac_paths.script_jobs_dir()
    assert home == clean_home / ".mac" / "openclaw" / "script-jobs"
    assert mac_paths.script_jobs_scripts_dir() == home / "scripts"
    assert mac_paths.script_jobs_output_dir() == home / "output"
    # The whole point: input and output share a parent.
    assert mac_paths.script_jobs_scripts_dir().parent == mac_paths.script_jobs_output_dir().parent


def test_script_job_home_relocates_with_its_root(clean_home, monkeypatch):
    monkeypatch.setenv("MAC_HOME", str(clean_home / "relocated"))
    assert mac_paths.script_jobs_scripts_dir() == (
        clean_home / "relocated" / "openclaw" / "script-jobs" / "scripts"
    )
    assert mac_paths.script_jobs_output_dir() == (
        clean_home / "relocated" / "openclaw" / "script-jobs" / "output"
    )


# --------------------------------------------------------------------------- #
# The guard: no live runtime path resolves into a Hermes home                   #
# --------------------------------------------------------------------------- #
def test_no_live_runner_path_resolves_into_a_hermes_home(clean_home):
    """With a clean environment, nothing the runner reads or writes is under
    the gateway home — the fallback aside, which is asserted separately."""
    legacy_gateway = clean_home / ".hermes"
    live = {
        "scripts": Path(runner.script_jobs_scripts_dir()),
        "output": Path(runner.script_jobs_output_dir()),
        "agent_bin": Path(runner._default_home_bin("openclaw-agent")),
        "message_bin": Path(runner._default_home_bin("openclaw-message")),
        "jobs_home": mac_paths.openclaw_home(),
    }
    for label, path in live.items():
        assert legacy_gateway not in path.parents and path != legacy_gateway, (
            "%s still resolves into the Hermes home: %s" % (label, path)
        )
        assert mac_paths.mac_home() in path.parents, "%s must live under the MAC root: %s" % (
            label,
            path,
        )


def test_runner_defaults_have_no_hermes_literal_left():
    """The two offending default lines are gone from the source, so a future
    edit cannot resurrect them without this failing."""
    source = RUNNER_PATH.read_text(encoding="utf-8")
    assert 'Path.home() / ".hermes" / "scripts"' not in source
    assert 'Path.home() / ".mac" / "openclaw"' not in source
    # The only sanctioned mention of the legacy home is the named fallback.
    hermes_lines = [
        line
        for line in source.splitlines()
        if '".hermes"' in line and not line.lstrip().startswith("#")
    ]
    assert hermes_lines == []


def test_installer_schedules_the_openclaw_scripts_home_not_hermes():
    installer = INSTALLER.read_text(encoding="utf-8")
    # The scheduled units export the new var; the default is the OpenClaw home.
    assert "MAC_OPENCLAW_SCRIPT_JOB_SCRIPTS_DIR" in installer
    assert (
        'local scripts_dir="${MAC_OPENCLAW_SCRIPT_JOB_SCRIPTS_DIR:-'
        '${MAC_HERMES_SCRIPTS_DIR:-$OPENCLAW_HOST_DIR/script-jobs/scripts}}"'
    ) in installer
    # The pre-untangle default is gone from the scheduling path.
    assert (
        'scripts_dir="${MAC_HERMES_SCRIPTS_DIR:-${HERMES_HOME:-$HOME/.hermes}/scripts}"'
        not in installer
    )
    # The units no longer teach the split by exporting the Hermes-named var.
    assert "Environment=MAC_HERMES_SCRIPTS_DIR=" not in installer
    assert "<key>MAC_HERMES_SCRIPTS_DIR</key>" not in installer
    # ...and the installer performs the on-disk move itself.
    assert "relocate_script_job_home" in installer
    assert "relocate-script-job-home.py" in installer


# --------------------------------------------------------------------------- #
# The mirrors must not drift from mac.mac_paths                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "resolver",
    [
        "mac_home",
        "openclaw_home",
        "gateway_home",
        "script_jobs_dir",
        "script_jobs_scripts_dir",
        "legacy_gateway_scripts_dir",
    ],
)
@pytest.mark.parametrize("module", [runner, relocator], ids=["runner", "relocator"])
def test_stdlib_mirror_matches_mac_paths(module, resolver, clean_home, monkeypatch):
    """The deploy scripts are stdlib-only (copied to hosts without the package),
    so they mirror the resolvers. Pin the mirror, defaults AND relocated."""
    assert getattr(module, resolver)() == getattr(mac_paths, resolver)()
    monkeypatch.setenv("MAC_HOME", str(clean_home / "moved"))
    monkeypatch.setenv("HERMES_HOME", str(clean_home / "moved-gateway"))
    assert getattr(module, resolver)() == getattr(mac_paths, resolver)()


def test_runner_output_mirror_matches_mac_paths(clean_home, monkeypatch):
    assert Path(runner.script_jobs_output_dir()) == mac_paths.script_jobs_output_dir()
    monkeypatch.setenv("MAC_OPENCLAW_SCRIPT_JOB_OUTPUT_DIR", str(clean_home / "out"))
    assert Path(runner.script_jobs_output_dir()) == mac_paths.script_jobs_output_dir()


# --------------------------------------------------------------------------- #
# The read-only fallback: safety without re-introducing the split               #
# --------------------------------------------------------------------------- #
def test_sanctioned_home_wins_over_the_legacy_home(tmp_path):
    primary, legacy = tmp_path / "new", tmp_path / "old"
    primary.mkdir()
    legacy.mkdir()
    (primary / "dream_cycle.py").write_text("print('new')\n", encoding="utf-8")
    (legacy / "dream_cycle.py").write_text("print('old')\n", encoding="utf-8")
    chosen, used_legacy = runner.select_scripts_dir(str(primary), "dream_cycle.py", str(legacy))
    assert Path(chosen) == primary
    assert used_legacy is False


def test_legacy_home_serves_only_what_the_new_home_lacks(tmp_path):
    """A host that has not run the relocator keeps its enabled jobs running."""
    primary, legacy = tmp_path / "new", tmp_path / "old"
    primary.mkdir()
    legacy.mkdir()
    (legacy / "dream_cycle.py").write_text("print('old')\n", encoding="utf-8")
    chosen, used_legacy = runner.select_scripts_dir(str(primary), "dream_cycle.py", str(legacy))
    assert Path(chosen) == legacy
    assert used_legacy is True


def test_missing_everywhere_reports_the_sanctioned_home(tmp_path):
    """The 'not found' note must name where the script is SUPPOSED to live."""
    primary, legacy = tmp_path / "new", tmp_path / "old"
    primary.mkdir()
    legacy.mkdir()
    chosen, used_legacy = runner.select_scripts_dir(str(primary), "ghost.py", str(legacy))
    assert Path(chosen) == primary
    assert used_legacy is False


def test_fallback_can_be_switched_off(clean_home):
    """A fully-relocated fleet can prove it no longer depends on the Hermes home."""
    args = runner.parser().parse_args(["--name", "j", "--legacy-scripts-dir", ""])
    assert runner._resolve_legacy_scripts_dir(args) == ""
    args = runner.parser().parse_args(["--name", "j"])
    assert runner._resolve_legacy_scripts_dir(args) == str(mac_paths.legacy_gateway_scripts_dir())


def test_fallback_env_opt_out(clean_home, monkeypatch):
    monkeypatch.setenv("MAC_OPENCLAW_LEGACY_SCRIPTS_DIR", "none")
    args = runner.parser().parse_args(["--name", "j"])
    assert runner._resolve_legacy_scripts_dir(args) == ""


def test_run_job_reports_when_it_fell_back_to_the_legacy_home(tmp_path):
    """The fallback is visible in the result, so the fleet can be swept for it
    instead of quietly depending on the old home forever."""
    primary, legacy = tmp_path / "new", tmp_path / "old"
    primary.mkdir()
    legacy.mkdir()
    (legacy / "dream_cycle.py").write_text("print('dreamt')\n", encoding="utf-8")
    result = runner.run_job(
        {"name": "dream-cycle", "legacy_script": "dream_cycle.py", "delivery": "local"},
        scripts_dir=str(primary),
        agent_bin="/nonexistent",
        message_bin="/nonexistent",
        output_dir=str(tmp_path / "out"),
        legacy_scripts_dir=str(legacy),
        agent_runner=lambda *a, **k: "ok",
    )
    assert result["legacy_scripts_home"] is True
    assert Path(result["scripts_dir"]) == legacy
    assert result["script_ran"] is True
    # Output landed in the sanctioned home, not next to the legacy script.
    assert Path(result["local_path"]).parent == tmp_path / "out"


def test_run_job_reports_no_fallback_once_relocated(tmp_path):
    scripts = tmp_path / "new"
    scripts.mkdir()
    (scripts / "dream_cycle.py").write_text("print('dreamt')\n", encoding="utf-8")
    result = runner.run_job(
        {"name": "dream-cycle", "legacy_script": "dream_cycle.py", "delivery": "local"},
        scripts_dir=str(scripts),
        agent_bin="/nonexistent",
        message_bin="/nonexistent",
        output_dir=str(tmp_path / "out"),
        legacy_scripts_dir=str(tmp_path / "old"),
        agent_runner=lambda *a, **k: "ok",
    )
    assert result["legacy_scripts_home"] is False
    assert Path(result["scripts_dir"]) == scripts


# --------------------------------------------------------------------------- #
# The relocator: scripts                                                        #
# --------------------------------------------------------------------------- #
def test_relocate_scripts_moves_verifies_and_symlinks(tmp_path):
    source, destination = tmp_path / "hermes" / "scripts", tmp_path / "jobs" / "scripts"
    source.mkdir(parents=True)
    (source / "dream_cycle.py").write_text("print('dreamt')\n", encoding="utf-8")
    (source / "lib").mkdir()
    (source / "lib" / "helper.py").write_text("X = 1\n", encoding="utf-8")

    result = relocator.relocate_scripts(source, destination, apply=True)

    assert result["status"] == "ok"
    assert sorted(result["moved"]) == ["dream_cycle.py", "lib/helper.py"]
    assert (destination / "dream_cycle.py").read_text(encoding="utf-8") == "print('dreamt')\n"
    assert (destination / "lib" / "helper.py").read_text(encoding="utf-8") == "X = 1\n"
    # The compat symlink keeps an un-reinstalled schedule resolving.
    assert result["symlinked"] is True
    assert source.is_symlink()
    assert (source / "dream_cycle.py").is_file()


def test_relocate_scripts_is_idempotent(tmp_path):
    source, destination = tmp_path / "hermes" / "scripts", tmp_path / "jobs" / "scripts"
    source.mkdir(parents=True)
    (source / "a.py").write_text("A\n", encoding="utf-8")
    relocator.relocate_scripts(source, destination, apply=True)

    again = relocator.relocate_scripts(source, destination, apply=True)
    assert again["status"] == "already-relocated"
    assert again["moved"] == []
    assert (destination / "a.py").read_text(encoding="utf-8") == "A\n"


def test_relocate_scripts_dry_run_touches_nothing(tmp_path):
    source, destination = tmp_path / "hermes" / "scripts", tmp_path / "jobs" / "scripts"
    source.mkdir(parents=True)
    (source / "a.py").write_text("A\n", encoding="utf-8")

    result = relocator.relocate_scripts(source, destination, apply=False)

    assert result["moved"] == ["a.py"]
    assert result["applied"] is False
    assert (source / "a.py").is_file()
    assert not destination.exists()


def test_relocate_scripts_refuses_to_pick_between_differing_copies(tmp_path):
    source, destination = tmp_path / "hermes" / "scripts", tmp_path / "jobs" / "scripts"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "a.py").write_text("OLD\n", encoding="utf-8")
    (destination / "a.py").write_text("NEW\n", encoding="utf-8")

    result = relocator.relocate_scripts(source, destination, apply=True)

    assert result["status"] == "conflicts"
    assert result["conflicts"] == ["a.py"]
    # Neither side was clobbered.
    assert (source / "a.py").read_text(encoding="utf-8") == "OLD\n"
    assert (destination / "a.py").read_text(encoding="utf-8") == "NEW\n"


def test_relocate_scripts_drops_an_identical_duplicate(tmp_path):
    source, destination = tmp_path / "hermes" / "scripts", tmp_path / "jobs" / "scripts"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "a.py").write_text("SAME\n", encoding="utf-8")
    (destination / "a.py").write_text("SAME\n", encoding="utf-8")

    result = relocator.relocate_scripts(source, destination, apply=True)

    assert result["status"] == "ok"
    assert result["skipped_identical"] == ["a.py"]
    assert result["symlinked"] is True


def test_relocate_scripts_with_no_legacy_tree_is_a_no_op(tmp_path):
    result = relocator.relocate_scripts(
        tmp_path / "missing", tmp_path / "jobs" / "scripts", apply=True
    )
    assert result["status"] == "nothing-to-do"


# --------------------------------------------------------------------------- #
# The relocator: the config.yaml backup pile                                    #
# --------------------------------------------------------------------------- #
def _pile(home: Path) -> None:
    """The nine-variant pile measured on the hub on 2026-08-21."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("live: true\n", encoding="utf-8")
    for name in (
        "config.yaml.bak",
        "config.yaml.bak-mac-home-sync",
        "config.yaml.bak-mac-shutdown-quench",
        "config.yaml.chatbak",
        "config.yaml.provbak.20260701T000000Z",
        "config.yaml.provbak.20260702T000000Z",
        "config.yaml.mac-redaction-backup-20260703T000000Z",
        "config.yaml.mac-redaction-backup-20260704T000000Z",
    ):
        (home / name).write_text("stale: %s\n" % name, encoding="utf-8")


def test_config_backups_keep_one_archive_the_rest_with_a_dated_note(tmp_path):
    home, archive_root = tmp_path / "hermes", tmp_path / "mac" / "backups"
    _pile(home)

    result = relocator.archive_config_backups(home, archive_root, apply=True)

    assert result["status"] == "ok"
    assert len(result["archived"]) == 8
    # Exactly one config file remains in the gateway home: the live one.
    assert sorted(p.name for p in home.iterdir()) == ["config.yaml"]
    assert (home / "config.yaml").read_text(encoding="utf-8") == "live: true\n"

    archive = Path(result["archive"])
    assert archive.name.startswith("hermes-config-")
    for name in result["archived"]:
        assert (archive / name).is_file()

    note = (archive / "WHICH-WAS-LIVE.md").read_text(encoding="utf-8")
    assert "config.yaml" in note and "LEFT IN PLACE" in note
    for name in result["archived"]:
        assert name in note
    # The note carries a digest for the live file so the claim is checkable.
    assert relocator.digest(home / "config.yaml") in note


def test_config_backups_flag_variants_identical_to_live(tmp_path):
    home, archive_root = tmp_path / "hermes", tmp_path / "backups"
    home.mkdir()
    (home / "config.yaml").write_text("live: true\n", encoding="utf-8")
    (home / "config.yaml.bak").write_text("live: true\n", encoding="utf-8")
    (home / "config.yaml.chatbak").write_text("different\n", encoding="utf-8")

    result = relocator.archive_config_backups(home, archive_root, apply=True)

    by_name = {record["name"]: record for record in result["records"]}
    assert by_name["config.yaml.bak"]["identical_to_live"] is True
    assert by_name["config.yaml.chatbak"]["identical_to_live"] is False
    note = (Path(result["archive"]) / "WHICH-WAS-LIVE.md").read_text(encoding="utf-8")
    assert "identical to live" in note


def test_config_backups_refuse_when_the_live_file_is_missing(tmp_path):
    """Which backup was authoritative is not determinable from the tree — the
    exact ambiguity this task exists to remove. Refuse instead of promoting."""
    home, archive_root = tmp_path / "hermes", tmp_path / "backups"
    home.mkdir()
    (home / "config.yaml.bak").write_text("a\n", encoding="utf-8")
    (home / "config.yaml.chatbak").write_text("b\n", encoding="utf-8")

    result = relocator.archive_config_backups(home, archive_root, apply=True)

    assert result["status"] == "refused-no-live-config"
    assert (home / "config.yaml.bak").is_file()
    assert not archive_root.exists()


def test_config_backups_are_idempotent(tmp_path):
    home, archive_root = tmp_path / "hermes", tmp_path / "backups"
    _pile(home)
    relocator.archive_config_backups(home, archive_root, apply=True)

    again = relocator.archive_config_backups(home, archive_root, apply=True)
    assert again["status"] == "already-archived"
    assert again["archived"] == []


def test_config_backups_dry_run_touches_nothing(tmp_path):
    home, archive_root = tmp_path / "hermes", tmp_path / "backups"
    _pile(home)

    result = relocator.archive_config_backups(home, archive_root, apply=False)

    assert len(result["archived"]) == 8
    assert not archive_root.exists()
    assert (home / "config.yaml.bak").is_file()


# --------------------------------------------------------------------------- #
# The relocator CLI                                                             #
# --------------------------------------------------------------------------- #
def test_cli_dry_run_is_the_default_and_reports_both_operations(tmp_path, capsys):
    home = tmp_path / "hermes"
    _pile(home)
    (home / "scripts").mkdir()
    (home / "scripts" / "a.py").write_text("A\n", encoding="utf-8")
    destination = tmp_path / "jobs" / "scripts"

    code = relocator.main(
        [
            "all",
            "--source",
            str(home / "scripts"),
            "--destination",
            str(destination),
            "--gateway-home",
            str(home),
            "--archive-root",
            str(tmp_path / "backups"),
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["operation"] for item in payload] == ["scripts", "config-backups"]
    assert all(item["applied"] is False for item in payload)
    assert (home / "scripts" / "a.py").is_file()
    assert not destination.exists()


def test_cli_exits_nonzero_when_an_operator_is_needed(tmp_path, capsys):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml.bak").write_text("a\n", encoding="utf-8")

    code = relocator.main(
        [
            "config-backups",
            "--apply",
            "--gateway-home",
            str(home),
            "--archive-root",
            str(tmp_path / "backups"),
        ]
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["status"] == "refused-no-live-config"
