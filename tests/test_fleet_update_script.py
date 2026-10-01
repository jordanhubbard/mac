"""scripts/fleet-update preflight and dispatch-hold bookkeeping, with every
fleet-facing command (ssh, mac, launchctl, systemctl, curl, sudo) stubbed on PATH.

The rollout itself is exercised only against a throwaway git repository and
fake hosts: nothing here can reach a real hub or worker.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet-update"
MIG = "src/mac/data/postgres/migrations"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

MAC_STUB = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_AGENT_STATE"]
state = json.load(open(state_path))
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write("mac " + " ".join(sys.argv[1:]) + "\n")
cmd = sys.argv[1:3]
if cmd == ["agent", "show"]:
    print(json.dumps(state))
elif cmd == ["agent", "hold"]:
    state.update(dispatch_hold=True, dispatch_hold_reason=sys.argv[sys.argv.index("--reason") + 1])
elif cmd == ["agent", "resume"]:
    state.update(dispatch_hold=False, dispatch_hold_reason=None)
else:
    sys.exit("unexpected mac call: %r" % sys.argv)
json.dump(state, open(state_path, "w"))
"""

# ssh: answers the read-only HEAD probe; for the update script it swallows
# stdin, records the call, and (on success) makes the "hub" see the new sha.
SSH_STUB = r"""#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_CALLS"
case "$*" in
  *"rev-parse HEAD"*) echo "$FAKE_REMOTE_HEAD"; exit 0 ;;
  *"bash -s --"*)
    cat > /dev/null
    sha=$(printf '%s\n' "$@" | grep -E '^[0-9a-f]{40}$' | head -1)
    python3 - "$sha" <<'PY'
import json, os, sys
p = os.environ["FAKE_AGENT_STATE"]; s = json.load(open(p))
if os.environ.get("FAKE_REPORT_SHA", "1") == "1":
    s["resources"]["source_state"]["commit_sha"] = sys.argv[1]
if os.environ.get("FAKE_STEAL_HOLD"):
    s.update(dispatch_hold=True, dispatch_hold_reason=os.environ["FAKE_STEAL_HOLD"])
json.dump(s, open(p, "w"))
PY
    exit "${FAKE_SSH_RC:-0}" ;;
esac
exit 99
"""

FORBIDDEN_STUB = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$FAKE_CALLS"
echo "fleet-update test: $(basename "$0") must not run here" >&2
exit 97
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fleet(tmp_path: Path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    src = tmp_path / "home" / ".mac" / "src" / "mac"
    subprocess.run(["git", "clone", "-q", str(origin), str(src)], check=True, capture_output=True)
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "t")
    _git(src, "checkout", "-q", "-b", "main")
    shas = {
        "a": _commit(src, "base", {f"{MIG}/0001_base.sql": "--\n", "pyproject.toml": "a\n"}),
        "b": _commit(src, "add 0002", {f"{MIG}/0002_more.sql": "--\n"}),
        "c": _commit(src, "docs only", {"README": "c\n"}),
    }
    _git(src, "push", "-q", "origin", "main")
    shas["local"] = _commit(src, "never pushed", {"LOCAL": "x\n"})
    _git(src, "checkout", "-q", "--detach", shas["a"])

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"mac": MAC_STUB, "ssh": SSH_STUB}.items():
        (bindir / name).write_text(body)
    for name in ("launchctl", "systemctl", "curl", "sudo", "pgrep"):
        (bindir / name).write_text(FORBIDDEN_STUB)
    for stub in bindir.iterdir():
        stub.chmod(0o755)

    state = tmp_path / "agent.json"
    calls = tmp_path / "calls.log"
    calls.write_text("")
    env = {
        "PATH": "%s:%s" % (bindir, os.environ.get("PATH", "")),
        "HOME": str(tmp_path / "home"),
        "MAC_HOME": str(tmp_path / "home" / ".mac"),
        "LC_ALL": os.environ.get("LC_ALL", "C"),
        "FLEET_UPDATE_POLL_SECONDS": "0",
        "FLEET_UPDATE_DRAIN_TIMEOUT": "0",
        "FLEET_UPDATE_HEALTH_TIMEOUT": "0",
        "FAKE_AGENT_STATE": str(state),
        "FAKE_CALLS": str(calls),
        "FAKE_REMOTE_HEAD": shas["a"],
    }

    class Fleet:
        pass

    f = Fleet()
    f.src, f.shas, f.env, f.state, f.calls, f.tmp = src, shas, env, state, calls, tmp_path

    def set_agent(*, held: bool, reason: str | None = None, task: str | None = None) -> None:
        state.write_text(
            json.dumps(
                {
                    "id": "agent_natasha",
                    "dispatch_hold": held,
                    "dispatch_hold_reason": reason,
                    "current_task_id": task,
                    "resources": {"source_state": {"commit_sha": shas["a"], "dirty": False}},
                }
            )
        )

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=120,
        )

    f.set_agent, f.run = set_agent, run
    set_agent(held=False)
    return f


def _calls(fleet) -> list[str]:
    return fleet.calls.read_text().splitlines()


def test_dry_run_resolves_short_sha_lists_new_migrations_and_changes_nothing(fleet) -> None:
    result = fleet.run("--dry-run", "hub", fleet.shas["b"][:9])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "target: %s" % fleet.shas["b"] in result.stdout
    assert "0002_more.sql" in result.stdout
    assert "PLAN: hub:" in result.stdout
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["a"]
    assert _calls(fleet) == []  # no launchctl/sudo/curl/ssh/mac at all
    logs = list((fleet.tmp / "home" / ".mac" / "logs").glob("fleet-update-*.log"))
    assert len(logs) == 1 and "0002_more.sql" in logs[0].read_text()


def test_no_migrations_reported_as_none(fleet) -> None:
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["b"])
    result = fleet.run("--dry-run", "hub", fleet.shas["c"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "and %s: none" % fleet.shas["c"][:12] in result.stdout


def test_refuses_to_go_backwards_across_a_migration(fleet) -> None:
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["c"])
    result = fleet.run("--dry-run", "hub", fleet.shas["a"])
    assert result.returncode != 0
    assert "refusing to go backwards across migration(s): 0002_more.sql" in result.stdout


def test_backwards_without_migration_change_is_allowed(fleet) -> None:
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["c"])
    result = fleet.run("--dry-run", "hub", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_unknown_and_unpushed_commits(fleet) -> None:
    unknown = fleet.run("--dry-run", "hub", "deadbeef")
    assert unknown.returncode != 0 and "not a known commit" in unknown.stdout
    unpushed = fleet.run("--dry-run", "hub", fleet.shas["local"])
    assert unpushed.returncode != 0 and "not on any origin branch" in unpushed.stdout


def test_worker_backwards_migration_uses_the_workers_deployed_commit(fleet) -> None:
    result = fleet.run("--dry-run", "natasha", fleet.shas["a"], FAKE_REMOTE_HEAD=fleet.shas["b"])
    assert result.returncode != 0
    assert "refusing to go backwards" in result.stdout
    assert not any("hold" in call for call in _calls(fleet))


def test_unknown_host_is_refused(fleet) -> None:
    result = fleet.run("--dry-run", "boris", fleet.shas["b"])
    assert result.returncode != 0 and "unknown host 'boris'" in result.stdout


def test_hosts_file_maps_names_to_ssh_targets_and_agents(fleet) -> None:
    hosts = fleet.tmp / "hosts"
    hosts.write_text("# name ssh agent\nboris jkh@10.0.0.9 agent_boris\n")
    result = fleet.run("--dry-run", "boris", fleet.shas["b"], FLEET_UPDATE_HOSTS=str(hosts))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boris (jkh@10.0.0.9, agent_boris)" in result.stdout
    assert any(
        call.startswith("ssh -o BatchMode=yes -o ConnectTimeout=10 jkh@10.0.0.9")
        for call in _calls(fleet)
    )


def test_worker_dry_run_neither_holds_nor_updates(fleet) -> None:
    result = fleet.run("--dry-run", "natasha", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PLAN: natasha: mac agent hold agent_natasha --reason 'fleet-update" in result.stdout
    calls = _calls(fleet)
    assert calls == [
        "ssh -o BatchMode=yes -o ConnectTimeout=10 100.87.229.125 "
        'git -C "$HOME/.mac/src/mac" rev-parse HEAD',
        "mac agent show agent_natasha",
    ]


def test_worker_sets_and_then_releases_its_own_hold(fleet) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    reason = "fleet-update %s" % fleet.shas["b"][:12]
    hold = calls.index("mac agent hold agent_natasha --reason %s" % reason)
    update = next(i for i, c in enumerate(calls) if "bash -s -- %s 0" % fleet.shas["b"] in c)
    resume = calls.index("mac agent resume agent_natasha")
    assert hold < update < resume
    assert json.loads(fleet.state.read_text())["dispatch_hold"] is False


def test_worker_never_removes_a_hold_it_did_not_set(fleet) -> None:
    stabilization = "stabilization 2026-09-30: fleet-wide dispatch pause"
    fleet.set_agent(held=True, reason=stabilization)
    result = fleet.run("--yes", "--hermes", "natasha", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    assert any("bash -s -- %s 1" % fleet.shas["b"] in c for c in calls)
    assert not any(c.startswith(("mac agent hold", "mac agent resume")) for c in calls)
    state = json.loads(fleet.state.read_text())
    assert state["dispatch_hold"] is True and state["dispatch_hold_reason"] == stabilization


def test_worker_leaves_a_replaced_hold_alone(fleet) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"], FAKE_STEAL_HOLD="operator: disk full")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mac agent resume agent_natasha" not in _calls(fleet)
    assert json.loads(fleet.state.read_text())["dispatch_hold_reason"] == "operator: disk full"


@pytest.mark.parametrize("failure", [{"FAKE_SSH_RC": "5"}, {"FAKE_REPORT_SHA": "0"}])
def test_failed_health_check_stops_and_leaves_the_host_held(fleet, failure) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"], **failure)
    assert result.returncode != 0
    assert "Rollout stopped; agent_natasha is left held" in result.stdout
    assert "mac agent resume agent_natasha" not in _calls(fleet)
    state = json.loads(fleet.state.read_text())
    assert state["dispatch_hold"] is True
    assert state["dispatch_hold_reason"] == "fleet-update %s" % fleet.shas["b"][:12]


def test_busy_worker_is_released_untouched_when_the_task_does_not_clear(fleet) -> None:
    fleet.set_agent(held=False, task="task_busy")
    result = fleet.run("--yes", "natasha", fleet.shas["b"])
    assert result.returncode != 0 and "still running task_busy" in result.stdout
    calls = _calls(fleet)
    assert not any("bash -s" in c for c in calls)
    assert "mac agent resume agent_natasha" in calls


def _remote_script() -> str:
    text = SCRIPT.read_text()
    start = text.index("<< 'REMOTE'\n") + len("<< 'REMOTE'\n")
    return text[start : text.index("\nREMOTE\n", start)] + "\n"


@pytest.fixture
def worker_home(fleet):
    """A fake worker $HOME whose checkout is the fixture repo, with service stubs."""
    home = fleet.tmp / "home"
    _git(fleet.src, "checkout", "-q", "--detach", "origin/main")
    _commit(fleet.src, "ship wrappers", {"deploy/bin/mac-agent-service": "#!/bin/sh\n"})
    for name in ("mac-agent-startup-self-test", "mac-task-executor", "mac-task-executor.py"):
        _commit(fleet.src, name, {"deploy/bin/" + name: name + "\n"})
    target = _commit(fleet.src, "observer", {"deploy/mac-crash-observer.py": "# observer\n"})
    _git(fleet.src, "push", "-q", "origin", "HEAD:main")
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["a"])
    venv_bin = home / ".mac" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (home / ".mac" / "bin").mkdir()
    (venv_bin / "python").write_text('#!/bin/sh\n[ -z "$FAKE_IMPORT_FAILS" ]\n')
    bindir = fleet.tmp / "bin"
    for name, body in {
        "sudo": '#!/bin/sh\necho "sudo $*" >> "$FAKE_CALLS"\n',
        "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$FAKE_CALLS"\n',
        "sleep": "#!/bin/sh\n",
        "uv": '#!/bin/sh\necho "uv $*" >> "$FAKE_CALLS"\n',
    }.items():
        (bindir / name).write_text(body)
    for path in [*venv_bin.iterdir(), *bindir.iterdir()]:
        path.chmod(0o755)
    return home, target


def _run_remote(fleet, home: Path, sha: str, **extra: str):
    return subprocess.run(
        ["bash", "-s", "--", sha, "1"],
        input=_remote_script(),
        env={**fleet.env, "HOME": str(home), **extra},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_remote_update_installs_wrappers_and_restarts_services(fleet, worker_home) -> None:
    home, target = worker_home
    result = _run_remote(fleet, home, target)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(fleet.src, "rev-parse", "HEAD") == target
    bin_dir = home / ".mac" / "bin"
    modes = {p.name: p.stat().st_mode & 0o777 for p in bin_dir.iterdir()}
    assert modes == {
        "mac-agent-service": 0o700,
        "mac-agent-startup-self-test": 0o700,
        "mac-task-executor": 0o700,
        "mac-task-executor.py": 0o600,
        "mac-crash-observer": 0o755,
    }
    calls = _calls(fleet)
    assert "sudo -n systemctl restart mac-agent" in calls
    assert "systemctl --user restart hermes-gateway" in calls
    assert "systemctl is-active --quiet mac-agent" in calls
    assert not any(c.startswith("uv ") for c in calls)  # pyproject.toml unchanged


def test_remote_update_restores_the_old_checkout_when_import_fails(fleet, worker_home) -> None:
    home, target = worker_home
    _git(fleet.src, "checkout", "-q", "--detach", target)
    _commit(fleet.src, "deps", {"pyproject.toml": "b\n"})
    _git(fleet.src, "push", "-q", "origin", "HEAD:main")
    new = _git(fleet.src, "rev-parse", "HEAD")
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["a"])
    result = _run_remote(fleet, home, new, FAKE_IMPORT_FAILS="1")
    assert result.returncode == 4
    assert "import mac failed" in result.stderr
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["a"]
    calls = _calls(fleet)
    assert sum(c.startswith("uv pip install -q --python") for c in calls) == 2  # forward + back
    assert not any("restart" in c for c in calls)


@pytest.fixture
def hub(fleet):
    """Stub rocky: launchctl/sudo/pgrep/curl on PATH, mac tools in the venv."""
    mac_home = fleet.tmp / "home" / ".mac"
    (mac_home / "mac.env").write_text("MAC_DATABASE_URL=postgresql://stub/mac\n")
    (mac_home / "current").mkdir(parents=True)
    venv_bin = mac_home / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    stubs = {
        venv_bin
        / "python": '#!/bin/sh\necho "import $*" >> "$FAKE_CALLS"\n[ -z "$FAKE_IMPORT_FAILS" ]\n',
        venv_bin / "mac-pg-backup": (
            '#!/bin/sh\necho "mac-pg-backup $* dsn=$MAC_DATABASE_URL" >> "$FAKE_CALLS"\n'
            'echo \'{"path": "/b/dump", "restore_verified": true}\'\n'
        ),
        venv_bin / "mac-schema-migrate": (
            '#!/bin/sh\necho "mac-schema-migrate $*" >> "$FAKE_CALLS"\n'
            'case "$1" in --status) echo "{\\"pending\\": ${FAKE_PENDING:-[]}}" ;; '
            '*) exit "${FAKE_MIGRATE_RC:-0}" ;; esac\n'
        ),
        fleet.tmp / "bin" / "sudo": '#!/bin/sh\necho "sudo $*" >> "$FAKE_CALLS"\n',
        fleet.tmp / "bin" / "launchctl": '#!/bin/sh\necho "launchctl $*" >> "$FAKE_CALLS"\n',
        fleet.tmp / "bin" / "pgrep": "#!/bin/sh\nexit 1\n",
        fleet.tmp / "bin" / "uv": '#!/bin/sh\necho "uv $*" >> "$FAKE_CALLS"\n',
        # /health follows FAKE_HEALTH; the attestation reports the checkout's HEAD,
        # exactly as mac-service derives MAC_SOURCE_COMMIT.
        fleet.tmp / "bin" / "curl": (
            '#!/bin/sh\ncase "$*" in\n'
            '  */health) [ "${FAKE_HEALTH:-1}" = 1 ] ;;\n'
            '  */startup-attestation) printf \'{"source_commit": "%s"}\' '
            '"$(git -C "$FLEET_UPDATE_SRC" rev-parse HEAD)" ;;\n  *) exit 22 ;;\nesac\n'
        ),
    }
    for path, body in stubs.items():
        path.write_text(body)
        path.chmod(0o755)
    fleet.env["FLEET_UPDATE_SRC"] = str(fleet.src)
    return mac_home


def test_hub_update_runs_the_manual_swap_in_order(fleet, hub) -> None:
    target = fleet.shas["b"]
    result = fleet.run("--yes", "--hermes", "hub", target, FAKE_PENDING='["0002_more"]')
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(fleet.src, "rev-parse", "HEAD") == target
    assert (hub / "current" / "source-commit").read_text() == target + "\n"
    assert (hub / "current" / "generation-id").read_text() == "legacy-%s\n" % target[:12]
    calls = _calls(fleet)
    order = [
        "sudo -n launchctl bootout system/com.mac.control-plane",
        next(c for c in calls if c.startswith("mac-pg-backup --json --out")),
        "import -c import mac.services",
        "mac-schema-migrate --status",
        next(c for c in calls if c.startswith("mac-schema-migrate --applied-by fleet-update:")),
        "sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.control-plane.plist",
    ]
    assert [calls.index(c) for c in order] == sorted(calls.index(c) for c in order)
    # the DSN reaches the backup through the environment, never argv or the log
    backup = order[1]
    assert backup.endswith("dsn=postgresql://stub/mac") and "--dsn" not in backup
    assert "postgresql://stub/mac" not in result.stdout
    assert any(
        c.startswith("launchctl kickstart -k gui/") and c.endswith("/com.mac.agent") for c in calls
    )
    assert any(c.endswith("/ai.hermes.gateway") for c in calls)
    assert not any(c.startswith("uv ") for c in calls)  # dependency files unchanged a..b


def test_hub_failure_before_migrate_restores_the_old_checkout(fleet, hub) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_IMPORT_FAILS="1")
    assert result.returncode != 0
    assert "ROLLBACK to %s (import mac.services failed)" % fleet.shas["a"][:12] in result.stdout
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["a"]
    assert (hub / "current" / "source-commit").read_text() == fleet.shas["a"] + "\n"
    calls = _calls(fleet)
    assert not any(c.startswith("mac-schema-migrate") for c in calls)
    assert (
        calls[-1]
        == "sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.control-plane.plist"
    )


@pytest.mark.parametrize("failure", [{"FAKE_MIGRATE_RC": "1"}, {"FAKE_HEALTH": "0"}])
def test_hub_failure_after_migrating_never_rolls_back(fleet, hub, failure) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_PENDING='["0002_more"]', **failure)
    assert result.returncode != 0
    assert "NOTHING is rolled back" in result.stdout
    assert "ROLLBACK" not in result.stdout
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["b"]
    assert not any("kickstart" in c for c in _calls(fleet))
