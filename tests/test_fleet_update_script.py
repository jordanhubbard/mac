"""scripts/fleet-update preflight and dispatch-hold bookkeeping, with every
fleet-facing command (ssh, mac, launchctl, systemctl, curl, sudo) stubbed on PATH.

The rollout itself is exercised only against a throwaway git repository and
fake hosts: nothing here can reach a real hub or worker.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet-update"
MIG = "src/mac/data/postgres/migrations"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")

# One state file per agent: agent_natasha's is FAKE_AGENT_STATE itself; any
# other agent starts as a copy of it with no hold and no task of its own.
MAC_STUB = r"""#!/usr/bin/env python3
import json, os, sys
base = os.environ["FAKE_AGENT_STATE"]
agent = sys.argv[3] if len(sys.argv) > 3 else "agent_natasha"
state_path = base if agent == "agent_natasha" else "%s.%s" % (base, agent)
if not os.path.exists(state_path):
    seed = json.load(open(base))
    seed.update(id=agent, dispatch_hold=False, dispatch_hold_reason=None, current_task_id=None)
    json.dump(seed, open(state_path, "w"))
state = json.load(open(state_path))
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write("mac " + " ".join(sys.argv[1:]) + "\n")
cmd = sys.argv[1:3]
if cmd == ["agent", "show"]:
    if os.environ.get("FAKE_BUSY_AGENT"):
        if sys.argv[3] == os.environ["FAKE_BUSY_AGENT"]:
            state["current_task_id"] = os.environ.get("FAKE_BUSY_TASK", "task_busy")
        else:
            state["current_task_id"] = None
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
  *"cat-file -e"*) exit "${FAKE_PRESTAGE_RC:-0}" ;;
  *"MAC_AGENT_ID"*)
    if [ -n "${FAKE_REMOTE_AGENT:-}" ]; then echo "$FAKE_REMOTE_AGENT"
    else case "$*" in *100.121.27.109*) echo agent_bullwinkle ;; *10.0.0.9*) echo agent_boris ;; *) echo agent_natasha ;; esac
    fi
    exit 0 ;;
  *"bash -s --"*)
    cat > /dev/null
    sha=$(printf '%s\n' "$@" | grep -E '^[0-9a-f]{40}$' | head -1)
    case "$*" in *100.121.27.109*) who=agent_bullwinkle ;; *) who=agent_natasha ;; esac
    python3 - "$sha" "$who" <<'PY'
import json, os, sys
p = os.environ["FAKE_AGENT_STATE"]
if sys.argv[2] != "agent_natasha":
    p = "%s.%s" % (p, sys.argv[2])
    if not os.path.exists(p):
        s = json.load(open(os.environ["FAKE_AGENT_STATE"]))
        s.update(id=sys.argv[2], dispatch_hold=False, dispatch_hold_reason=None, current_task_id=None)
        json.dump(s, open(p, "w"))
s = json.load(open(p))
if os.environ.get("FAKE_REPORT_SHA", "1") == "1":
    s["resources"]["source_state"].update(commit_sha=sys.argv[1])
    if not os.environ.get("FAKE_NOT_RESTARTED"):
        s["resources"]["source_state"].update(loaded_commit=sys.argv[1], restart_pending=False)
    else:
        s["resources"]["source_state"].update(restart_pending=True)
if os.environ.get("FAKE_STEAL_HOLD"):
    s.update(dispatch_hold=True, dispatch_hold_reason=os.environ["FAKE_STEAL_HOLD"])
json.dump(s, open(p, "w"))
PY
    exit "${FAKE_SSH_RC:-0}" ;;
esac
exit 99
"""

GH_STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as calls:
    calls.write("gh " + " ".join(args) + "\n")
if args[:2] == ["run", "list"]:
    if os.environ.get("FAKE_CI_RED"):
        print("[]")
    elif "--commit" in args:
        print(json.dumps([{"databaseId": 123, "conclusion": "success"}]))
    else:
        print(json.dumps([{
            "headSha": os.environ["FAKE_DEFAULT_PROMOTED"],
            "databaseId": 123,
            "conclusion": "success",
            "createdAt": "2026-01-01T00:00:00Z",
        }]))
elif args[:2] == ["run", "download"]:
    if os.environ.get("FAKE_NO_RECEIPT"):
        raise SystemExit(1)
    out = pathlib.Path(args[args.index("--dir") + 1])
    name = args[args.index("--name") + 1]
    out.mkdir(parents=True, exist_ok=True)
    sha = os.environ.get("FAKE_PROMOTION_SHA") or os.environ["FAKE_DEFAULT_PROMOTED"]
    disposition = os.environ.get("FAKE_RECEIPT_DISPOSITION", "built")
    build_revision = os.environ.get("FAKE_BUILD_REVISION", sha)
    digest = "sha256:" + "d" * 64
    frozen = "sha256:" + "f" * 64
    if name == "openshell-runtime-image-publication":
        path = out / "openshell-runtime-publication" / "publication-receipt.json"
        path.parent.mkdir()
        path.write_text(json.dumps({
            "schema": "mac.image_publication_identity.v1",
            "status": "passed", "kind": "openshell-runtime",
            "repository": "ghcr.io/jordanhubbard/mac-openshell-runtime",
            "requested_revision": sha, "image_digest": digest,
            "build_revision": build_revision, "disposition": disposition,
            "frozen_inputs_sha256": frozen,
        }))
    else:
        (out / "mac-release-manifest.json").write_text(json.dumps({
            "complete": True, "mac": {"commit": sha},
            "images": {"coding": "ghcr.io/jordanhubbard/mac-openshell-runtime@" + digest},
        }))
else:
    raise SystemExit("unexpected gh call: %r" % args)
"""

CURL_STUB = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_CALLS"
case "$*" in
  *ghcr.io/token*) printf '{"token":"stub"}' ;;
  *ghcr.io/v2/*)
    [ -n "$FAKE_UNTESTED_IMAGE" ] || printf 'HTTP/2 200\r\nDocker-Content-Digest: sha256:%064d\r\n' 0 | tr 0 d ;;
  *) echo "fleet-update test: unexpected curl $*" >&2; exit 97 ;;
esac
"""

INVENTORY = """\
version: 1
fleets:
  rocky:
    default: true
    hub_agent: rocky
    hub_url: http://127.0.0.1:8789
    agents:
    - {name: natasha, target: 100.87.229.125, os: linux}
    - {name: bullwinkle, target: 100.121.27.109, os: linux}
    - {name: rocky, target: jkh@127.0.0.1, os: darwin}
    - {name: old-pod, enabled: false, target: horde@old, os: linux, notes: pod is gone}
    - {name: hgx-1, instance_kind: fungible, target: horde@hgx-1, os: linux}
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
    for name, body in {"mac": MAC_STUB, "ssh": SSH_STUB, "gh": GH_STUB, "curl": CURL_STUB}.items():
        (bindir / name).write_text(body)
    for name in ("launchctl", "systemctl", "sudo", "pgrep"):
        (bindir / name).write_text(FORBIDDEN_STUB)
    for stub in bindir.iterdir():
        stub.chmod(0o755)

    inventory = tmp_path / "home" / ".mac" / "fleets.yaml"
    inventory.write_text(INVENTORY)
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
        "FAKE_DEFAULT_PROMOTED": shas["c"],
        # The inventory is read with this checkout's src and a Python that
        # has pyyaml; the hub tests stub the venv's python.
        "FLEET_UPDATE_PYTHON": sys.executable,
        # Stubbed venv pythons run mac.node_files for real, from this checkout.
        "REAL_PY": sys.executable,
        "REAL_SRC": str(ROOT / "src"),
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
                    "resources": {
                        "source_state": {
                            "commit_sha": shas["a"],
                            "loaded_commit": shas["a"],
                            "restart_pending": False,
                            "dirty": False,
                        }
                    },
                }
            )
        )

    def run(*args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        requested = args[-1] if args and len(args[-1]) >= 7 and args[-1] not in {"hub", "all", "natasha"} else shas["c"]
        requested = next((sha for sha in shas.values() if sha.startswith(requested)), requested)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**env, "FAKE_PROMOTION_SHA": requested, **extra},
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
    assert not any(call.startswith(("launchctl ", "sudo ", "ssh ", "mac ")) for call in _calls(fleet))
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
    assert unpushed.returncode != 0 and "commit not on main" in unpushed.stdout


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"FAKE_CI_RED": "1"}, "CI not green"),
        ({"FAKE_NO_RECEIPT": "1"}, "no release manifest or runtime image publication receipt"),
        ({"FAKE_UNTESTED_IMAGE": "1"}, "runtime image is not tested-tagged"),
    ],
)
def test_promotion_refuses_each_missing_release_piece_before_holds(fleet, extra, reason) -> None:
    result = fleet.run("--dry-run", "natasha", fleet.shas["b"], **extra)
    assert result.returncode != 0 and reason in result.stdout
    assert not any(call.startswith("mac agent hold") for call in _calls(fleet))


def test_allow_unpromoted_is_prominent_and_bypasses_promotion(fleet) -> None:
    result = fleet.run("--dry-run", "--allow-unpromoted", "hub", fleet.shas["local"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING: deploying UNPROMOTED target" in result.stdout
    assert "done (WARNING: UNPROMOTED emergency override)" in result.stdout


def test_default_target_is_newest_promoted_main_commit(fleet) -> None:
    result = fleet.run("--dry-run", "hub")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "target: %s" % fleet.shas["c"] in result.stdout


def test_reused_runtime_image_built_by_an_ancestor_is_promoted(fleet) -> None:
    result = fleet.run(
        "--dry-run",
        "hub",
        fleet.shas["c"],
        FAKE_RECEIPT_DISPOSITION="reused",
        FAKE_BUILD_REVISION=fleet.shas["a"],
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reused_runtime_image_built_by_a_non_ancestor_is_refused(fleet) -> None:
    result = fleet.run(
        "--dry-run",
        "hub",
        fleet.shas["b"],
        FAKE_RECEIPT_DISPOSITION="reused",
        FAKE_BUILD_REVISION=fleet.shas["c"],
    )
    assert result.returncode != 0
    assert "reused runtime image build commit is not an ancestor on main" in result.stdout


def test_worker_backwards_migration_uses_the_workers_deployed_commit(fleet) -> None:
    result = fleet.run("--dry-run", "natasha", fleet.shas["a"], FAKE_REMOTE_HEAD=fleet.shas["b"])
    assert result.returncode != 0
    assert "refusing to go backwards" in result.stdout
    assert not any("hold" in call for call in _calls(fleet))


def test_unknown_host_is_refused(fleet) -> None:
    result = fleet.run("--dry-run", "boris", fleet.shas["b"])
    assert result.returncode != 0 and "unknown host 'boris'" in result.stdout


def test_the_inventory_maps_names_to_ssh_targets_and_agents(fleet) -> None:
    inventory = fleet.tmp / "inventory.yaml"
    inventory.write_text(
        INVENTORY + "    - {name: boris, target: jkh@10.0.0.9, agent_id: agent_boris, os: linux}\n"
    )
    result = fleet.run("--dry-run", "boris", fleet.shas["b"], FLEET_UPDATE_INVENTORY=str(inventory))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boris (jkh@10.0.0.9, agent_boris)" in result.stdout
    assert any(
        call.startswith("ssh -o BatchMode=yes -o ConnectTimeout=10 jkh@10.0.0.9")
        for call in _calls(fleet)
    )


def test_a_replaced_host_is_refused_before_anything_changes(fleet) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"], FAKE_REMOTE_AGENT="agent_imposter")
    assert result.returncode != 0
    assert "identifies as 'agent_imposter', the inventory says 'agent_natasha'" in result.stdout
    assert not any(call.startswith("mac ") for call in _calls(fleet))


@pytest.mark.parametrize(
    ("host", "why"),
    [("old-pod", "is retired (pod is gone)"), ("hgx-1", "is external (instance_kind: fungible)")],
)
def test_retired_and_external_hosts_are_never_updated(fleet, host, why) -> None:
    result = fleet.run("--yes", host, fleet.shas["b"])
    assert result.returncode != 0 and why in result.stdout
    assert _calls(fleet) == []


def test_a_conflicting_inventory_stops_before_any_host_is_touched(fleet) -> None:
    inventory = fleet.tmp / "inventory.yaml"
    inventory.write_text(INVENTORY + "    - {name: natasha2, target: jkh@100.87.229.125, os: linux}\n")
    result = fleet.run("--yes", "all", fleet.shas["b"], FLEET_UPDATE_INVENTORY=str(inventory))
    assert result.returncode != 0
    assert "ssh host 100.87.229.125 is claimed by rocky/natasha and rocky/natasha2" in result.stdout
    assert _calls(fleet) == []


def test_the_inventory_hub_name_updates_the_hub(fleet) -> None:
    result = fleet.run("--dry-run", "rocky", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PLAN: hub:" in result.stdout


def test_worker_dry_run_neither_holds_nor_updates(fleet) -> None:
    result = fleet.run("--dry-run", "natasha", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PLAN: natasha: mac agent hold agent_natasha --reason 'fleet-update" in result.stdout
    calls = [call for call in _calls(fleet) if not call.startswith(("gh ", "curl "))]
    assert calls == [
        "ssh -o BatchMode=yes -o ConnectTimeout=10 100.87.229.125 "
        'git -C "$HOME/.mac/src/mac" rev-parse HEAD',
        "ssh -o BatchMode=yes -o ConnectTimeout=10 100.87.229.125 "
        'sed -n "s/^MAC_AGENT_ID=//p" "$HOME/.mac/mac.env" | tail -1',
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


def test_worker_prestages_before_holding(fleet) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    prestage = next(i for i, c in enumerate(calls) if "cat-file -e %s^{commit}" % fleet.shas["b"] in c)
    hold = next(i for i, c in enumerate(calls) if c.startswith("mac agent hold"))
    assert prestage < hold


def test_a_failed_prestage_changes_nothing(fleet) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"], FAKE_PRESTAGE_RC="1")
    assert result.returncode != 0
    assert "prestage failed" in result.stdout and "nothing changed on the host" in result.stdout
    calls = _calls(fleet)
    assert not any(c.startswith(("mac agent hold", "mac agent resume")) for c in calls)
    assert not any("bash -s --" in c for c in calls)


def test_a_worker_whose_process_did_not_restart_is_not_healthy(fleet) -> None:
    result = fleet.run("--yes", "natasha", fleet.shas["b"], FAKE_NOT_RESTARTED="1")
    assert result.returncode != 0
    assert "FAILED (code 6)" in result.stdout
    # The host stays held: its checkout moved but the running process did not.
    assert json.loads(fleet.state.read_text())["dispatch_hold"] is True


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


def test_all_skips_a_busy_worker_and_updates_the_rest(fleet, hub) -> None:
    # --no-drain: the hub boundary would otherwise refuse the whole rollout.
    result = fleet.run(
        "--yes",
        "--no-drain",
        "all",
        fleet.shas["b"],
        FAKE_BUSY_AGENT="agent_natasha",
        FAKE_BUSY_TASK="task_busy",
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "natasha: SKIPPED (still running task_busy)" in result.stdout
    assert "INCOMPLETE: skipped workers: natasha (task_busy): " in result.stdout
    assert "fleet-update --yes natasha %s" % fleet.shas["b"] in result.stdout
    assert "fleet-update: done" not in result.stdout
    calls = _calls(fleet)
    assert "mac agent resume agent_natasha" in calls
    assert any("100.121.27.109 bash -s -- %s" % fleet.shas["b"] in call for call in calls)


def test_all_stops_before_the_second_worker_when_first_fails_health(fleet, hub) -> None:
    result = fleet.run("--yes", "all", fleet.shas["b"], FAKE_REPORT_SHA="0")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "natasha: FAILED (code 6)" in result.stdout
    assert not any("100.121.27.109" in call for call in _calls(fleet))


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
    (venv_bin / "python").write_text(
        '#!/bin/sh\necho "python $*" >> "$FAKE_CALLS"\n'
        'case "$*" in *mac.node_files*|*mac.credential_aliases*) PYTHONPATH="$REAL_SRC" exec "$REAL_PY" "$@" ;;\n'
        '  *soul_install*) [ -z "$FAKE_SOUL_FAILS" ] && echo \'{"status": "ok"}\' ;;\n'
        '  *"openshell_image_pin --image"*) [ -z "$FAKE_PIN_FAILS" ] ;;\n'
        '  *"native_runtime --check"*) [ -z "$FAKE_UNLOCKED" ] ;;\n'
        '  *native_runtime*) [ -z "$FAKE_LOCK_FAILS" ] ;;\n'
        '  *) [ -z "$FAKE_IMPORT_FAILS" ] ;; esac\n'
    )
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


def _run_remote(fleet, home: Path, sha: str, *pin: str, **extra: str):
    return subprocess.run(
        ["bash", "-s", "--", sha, "1", *pin],
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


def test_remote_update_installs_the_soul_graph_before_restarting(fleet, worker_home) -> None:
    home, target = worker_home
    (home / ".mac" / "mac.env").write_text('HERMES_HOME="/srv/hermes"\n')
    result = _run_remote(fleet, home, target)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    soul = "python -m mac.soul_install --hermes-home /srv/hermes"
    assert soul in calls
    assert calls.index(soul) < calls.index("sudo -n systemctl restart mac-agent")
    assert 'remote: soul graph: {"status": "ok"}' in result.stdout
    fleet_context = "python -m mac.fleet_context_service --source %s/.mac/src/mac" % home
    assert fleet_context in calls
    assert calls.index(fleet_context) < calls.index("sudo -n systemctl restart mac-agent")


def test_remote_update_syncs_the_single_image_pin_before_restarting(fleet, worker_home) -> None:
    home, target = worker_home
    result = _run_remote(fleet, home, target)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    assert "python -m mac.openshell_image_pin" in calls
    assert calls.index("python -m mac.openshell_image_pin") < calls.index(
        "sudo -n systemctl restart mac-agent"
    )


def test_remote_update_repins_the_image_when_asked(fleet, worker_home) -> None:
    home, target = worker_home
    image = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "b" * 64
    result = _run_remote(fleet, home, target, image, "sha256:" + "c" * 64)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    repin = "python -m mac.openshell_image_pin --image %s --input-sha256 sha256:%s" % (
        image,
        "c" * 64,
    )
    assert repin in calls
    assert calls.index(repin) < calls.index("sudo -n systemctl restart mac-agent")


def test_a_failed_repin_restores_the_checkout_and_never_restarts(fleet, worker_home) -> None:
    home, target = worker_home
    image = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "b" * 64
    result = _run_remote(
        fleet, home, target, image, "sha256:" + "c" * 64, FAKE_PIN_FAILS="1"
    )
    assert result.returncode == 4
    assert "runtime image repin failed" in result.stderr
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["a"]
    assert not any("restart" in c for c in _calls(fleet))


def test_a_repin_needs_both_the_image_and_its_identity(fleet) -> None:
    result = fleet.run("--dry-run", "--runtime-image", "x", "hub", fleet.shas["c"])
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_a_failed_soul_install_does_not_fail_the_update(fleet, worker_home) -> None:
    home, target = worker_home
    result = _run_remote(fleet, home, target, FAKE_SOUL_FAILS="1")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    assert "python -m mac.soul_install --hermes-home %s/.hermes" % home in calls
    assert "sudo -n systemctl restart mac-agent" in calls


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
    assert sum(c.startswith("python -m mac.native_runtime --source") for c in calls) == 2  # forward + back
    assert not any("restart" in c for c in calls)


def _locked_installs(calls):
    return [c for c in calls if c.startswith("python -m mac.native_runtime --source")]


def test_a_dependency_change_gets_the_locked_install(fleet, worker_home) -> None:
    home, target = worker_home
    _git(fleet.src, "checkout", "-q", "--detach", target)
    _commit(fleet.src, "deps", {"uv.lock": "new\n"})
    _git(fleet.src, "push", "-q", "origin", "HEAD:main")
    new = _git(fleet.src, "rev-parse", "HEAD")
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["a"])
    result = _run_remote(fleet, home, new)
    assert result.returncode == 0, result.stdout + result.stderr
    [install] = _locked_installs(_calls(fleet))
    mac = home / ".mac"
    assert "--source %s/src/mac --venv %s/venv " % (mac, mac) in install
    assert "--footprint %s/agent-footprint.json --uv " % mac in install
    assert "--record %s/logs/native-runtime-locked-" % mac in install
    assert not any(c.startswith("uv pip") for c in _calls(fleet))


def test_a_stale_baseline_is_repaired_without_a_dependency_change(fleet, worker_home) -> None:
    home, target = worker_home
    result = _run_remote(fleet, home, target, FAKE_UNLOCKED="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "locked runtime baseline does not hold; reinstalling it" in result.stdout
    assert len(_locked_installs(_calls(fleet))) == 1
    assert "sudo -n systemctl restart mac-agent" in _calls(fleet)


def test_a_failed_repair_warns_but_does_not_fail_the_update(fleet, worker_home) -> None:
    home, target = worker_home
    result = _run_remote(fleet, home, target, FAKE_UNLOCKED="1", FAKE_LOCK_FAILS="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING locked install failed" in result.stderr
    assert _git(fleet.src, "rev-parse", "HEAD") == target


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
        / "python": (
            '#!/bin/sh\necho "import $*" >> "$FAKE_CALLS"\n'
            'case "$*" in *mac.node_files*) PYTHONPATH="$REAL_SRC" exec "$REAL_PY" "$@" ;;\n'
            # The deployed (old) venv has no mac.credential_aliases: the hub
            # converts before checkout, so only the script's own tree has it.
            '  *mac.credential_aliases*) [ "$PYTHONPATH" = "$REAL_SRC" ] || '
            '{ echo "No module named mac.credential_aliases" >&2; exit 1; }; exec "$REAL_PY" "$@" ;;\n'
            '  *"native_runtime --check"*) [ -z "$FAKE_UNLOCKED" ] ;;\n'
            '  *native_runtime*) [ -z "$FAKE_LOCK_FAILS" ] ;;\n'
            '  *) [ -z "$FAKE_IMPORT_FAILS" ] ;; esac\n'
        ),
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
            '  *ghcr.io/token*) echo "curl $*" >> "$FAKE_CALLS"; printf \'{"token":"stub"}\' ;;\n'
            '  *ghcr.io/v2/*) echo "curl $*" >> "$FAKE_CALLS"; printf \'HTTP/2 200\\r\\nDocker-Content-Digest: sha256:%064d\\r\\n\' 0 | tr 0 d ;;\n'
            '  */health) [ "${FAKE_HEALTH:-1}" = 1 ] ;;\n'
            '  */startup-attestation) [ -n "$FAKE_NO_SOURCE" ] && { printf \'{"source_commit": ""}\'; exit 0; }\n'
            '    printf \'{"source_commit": "%s"}\' '
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
    # The legacy ~/.mac/current markers are no longer written.
    assert not (hub / "current" / "source-commit").exists()
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
    assert any(c.startswith("launchctl bootout gui/") and c.endswith("/com.mac.agent") for c in calls)
    assert not any("kickstart" in c and c.endswith("/com.mac.agent") for c in calls)
    assert any("agent delete agent_rocky --actor fleet-update" in c for c in calls)
    assert any(c.endswith("/ai.hermes.gateway") for c in calls)
    soul = next(c for c in calls if "mac.soul_install" in c)
    assert soul.endswith("--hermes-home %s/.hermes" % hub.parent)
    hermes = next(c for c in calls if c.endswith("/ai.hermes.gateway"))
    assert calls.index(soul) < calls.index(hermes)
    assert "import -m mac.fleet_context_service --source %s" % fleet.src in calls
    assert not any(c.startswith("uv ") for c in calls)  # dependency files unchanged a..b


def test_hub_retires_its_agent_for_good(fleet, hub) -> None:
    """The plist would restart the agent at login; the token would break refreshes."""
    agents = fleet.tmp / "home" / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / "com.mac.agent.plist").write_text("<plist/>\n")
    env = hub / "mac.env"
    env.write_text(
        "MAC_DATABASE_URL=postgresql://stub/mac\n"
        "MAC_WORKER_TOKEN=retired-agent-token\n"
        "export MAC_WORKER_TOKEN__MAC=retired-agent-token\n"
        "MAC_API_TOKEN=admin-token\n"
    )
    env.chmod(0o600)
    result = fleet.run("--yes", "hub", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (agents / "com.mac.agent.plist").exists()
    assert (agents / "com.mac.agent.plist.disabled").read_text() == "<plist/>\n"
    assert env.read_text() == "MAC_DATABASE_URL=postgresql://stub/mac\nMAC_API_TOKEN=admin-token\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    (backup,) = hub.glob("mac.env.bak-hub-agent-*")
    assert "MAC_WORKER_TOKEN=retired-agent-token" in backup.read_text()
    assert "retired-agent-token" not in result.stdout

    again = fleet.run("--yes", "hub", fleet.shas["b"])
    assert again.returncode == 0, again.stdout + again.stderr
    assert len(list(hub.glob("mac.env.bak-hub-agent-*"))) == 1


def test_hub_installs_its_managed_files_including_mac_service(fleet, hub) -> None:
    _git(fleet.src, "checkout", "-q", "--detach", "origin/main")
    files = {
        "deploy/bin/" + name: name + "\n"
        for name in (
            "mac-agent-service",
            "mac-agent-startup-self-test",
            "mac-task-executor",
            "mac-task-executor.py",
            "mac-service",
        )
    }
    files["deploy/mac-crash-observer.py"] = "# observer\n"
    target = _commit(fleet.src, "hub wrappers", files)
    _git(fleet.src, "push", "-q", "origin", "HEAD:main")
    _git(fleet.src, "checkout", "-q", "--detach", fleet.shas["a"])

    result = fleet.run("--yes", "hub", target)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (hub / "bin" / "mac-service").read_text() == "mac-service\n"
    assert (hub / "bin" / "mac-service").stat().st_mode & 0o777 == 0o755
    assert "hub: managed files:" in result.stdout
    state = json.loads((hub / "managed-files.json").read_text())
    assert set(state["files"]) == {"bin/mac-service"}


def test_a_hub_reporting_no_source_identity_is_not_attested(fleet, hub) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_NO_SOURCE="1")
    assert result.returncode != 0
    assert "hub did not come up healthy" in result.stdout


def test_hub_repairs_a_stale_locked_runtime_while_it_is_stopped(fleet, hub) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_UNLOCKED="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hub: locked runtime baseline does not hold; reinstalling it" in result.stdout
    calls = _calls(fleet)
    install = next(c for c in calls if c.startswith("import -m mac.native_runtime --source"))
    assert "--venv %s/venv " % hub in install and "--footprint %s/agent-footprint.json" % hub in install
    stop = calls.index("sudo -n launchctl bootout system/com.mac.control-plane")
    assert stop < calls.index(install) < calls.index("import -c import mac.services")


def test_hub_failure_before_migrate_restores_the_old_checkout(fleet, hub) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_IMPORT_FAILS="1")
    assert result.returncode != 0
    assert "ROLLBACK to %s (import mac.services failed)" % fleet.shas["a"][:12] in result.stdout
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["a"]
    calls = _calls(fleet)
    assert not any(c.startswith("mac-schema-migrate") for c in calls)
    restart = "sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.control-plane.plist"
    # The old hub is serving again, so the drain holds are released after it.
    acts = [c for c in calls if not c.startswith("mac agent show")]
    assert acts[-3:] == [restart, "mac agent resume agent_natasha", "mac agent resume agent_bullwinkle"]


@pytest.mark.parametrize("failure", [{"FAKE_MIGRATE_RC": "1"}, {"FAKE_HEALTH": "0"}])
def test_hub_failure_after_migrating_never_rolls_back(fleet, hub, failure) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_PENDING='["0002_more"]', **failure)
    assert result.returncode != 0
    assert "NOTHING is rolled back" in result.stdout
    assert "ROLLBACK" not in result.stdout
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["b"]
    assert not any("kickstart" in c for c in _calls(fleet))


def test_mac_service_runs_the_checkout_and_attests_its_head(tmp_path: Path) -> None:
    """deploy/bin/mac-service starts ~/.mac/src/mac and nothing else.

    The ~/.mac/current indirection and the self-upgrade supervisor are gone: a
    stale current/ tree and a supervisor script left behind by an old install
    are both ignored. MAC_SOURCE_COMMIT is the checkout's HEAD, which is what
    fleet-update's hub_attested compares against the target sha.
    """
    home = tmp_path / "home"
    mac_home = home / ".mac"
    src = mac_home / "src" / "mac"
    src.mkdir(parents=True)
    _git(src, "init", "-q")
    (src / "README").write_text("x\n")
    _git(src, "add", "README")
    _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    head = _git(src, "rev-parse", "HEAD")
    (mac_home / "mac.env").write_text("MAC_DATABASE_URL=postgresql://stub/mac\n")
    record = tmp_path / "exec.json"
    python = mac_home / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        'printf \'{"argv": "%s", "commit": "%s", "generation": "%s"}\' '
        '"$*" "$MAC_SOURCE_COMMIT" "${MAC_HUB_GENERATION_ID:-}" > "$RECORD"\n'
    )
    python.chmod(0o755)
    stale_bin = mac_home / "current" / "venv" / "bin"
    stale_bin.mkdir(parents=True)
    supervisor = stale_bin / "mac-hub-upgrade-supervisor"
    supervisor.write_text('#!/bin/sh\ntouch "$RECORD.supervisor"\nexit 1\n')
    supervisor.chmod(0o755)
    (mac_home / "current" / "source-commit").write_text("f" * 40 + "\n")

    env = {"HOME": str(home), "PATH": os.environ["PATH"], "RECORD": str(record)}
    result = subprocess.run(
        ["bash", str(ROOT / "deploy" / "bin" / "mac-service")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(record.read_text()) == {
        "argv": "-m mac.hub_serve",
        "commit": head,
        "generation": "",
    }
    assert not Path(str(record) + ".supervisor").exists()


def test_startup_attestation_reports_only_the_source_commit(monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mac.http_routes.system import SystemRouteServices, build_system_router

    monkeypatch.setenv("MAC_SOURCE_COMMIT", "a" * 40)
    monkeypatch.setenv("MAC_HUB_GENERATION_ID", "legacy-aaaaaaaaaaaa")
    app = FastAPI()
    app.include_router(
        build_system_router(SystemRouteServices(None, None), get_principal=lambda: None)
    )

    response = TestClient(app).get("/startup-attestation")

    assert response.status_code == 200
    assert response.json() == {
        "schema": "mac.hub_startup_attestation.v1",
        "source_commit": "a" * 40,
    }


HUB_HOLD = "fleet-update hub %s"


def test_hub_drains_every_worker_before_stopping_and_resumes_them_once_healthy(fleet, hub) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(fleet)
    reason = HUB_HOLD % fleet.shas["b"][:12]
    order = [
        "mac agent hold agent_natasha --reason %s" % reason,
        "mac agent hold agent_bullwinkle --reason %s" % reason,
        "sudo -n launchctl bootout system/com.mac.control-plane",
        "sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.control-plane.plist",
        "mac agent resume agent_natasha",
        "mac agent resume agent_bullwinkle",
    ]
    assert [calls.index(c) for c in order] == sorted(calls.index(c) for c in order)
    assert json.loads(fleet.state.read_text())["dispatch_hold"] is False


def test_a_busy_worker_leaves_the_hub_untouched(fleet, hub) -> None:
    result = fleet.run(
        "--yes", "hub", fleet.shas["b"], FAKE_BUSY_AGENT="agent_bullwinkle", FAKE_BUSY_TASK="task_long"
    )
    assert result.returncode == 1
    assert "hub: still running bullwinkle (task_long); nothing changed" in result.stdout
    assert "--no-drain" in result.stdout
    calls = _calls(fleet)
    assert not any("launchctl" in c or c.startswith("mac-pg-backup") for c in calls)
    assert "mac agent resume agent_natasha" in calls and "mac agent resume agent_bullwinkle" in calls
    assert _git(fleet.src, "rev-parse", "HEAD") == fleet.shas["a"]


def test_the_hub_drain_never_releases_a_hold_it_did_not_set(fleet, hub) -> None:
    fleet.set_agent(held=True, reason="operator: investigating")
    result = fleet.run("--yes", "hub", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "natasha already held ('operator: investigating')" in result.stdout
    calls = _calls(fleet)
    assert not any(c.startswith("mac agent hold agent_natasha") for c in calls)
    assert "mac agent resume agent_natasha" not in calls
    assert "mac agent resume agent_bullwinkle" in calls
    assert json.loads(fleet.state.read_text())["dispatch_hold_reason"] == "operator: investigating"


def test_no_drain_restarts_the_hub_without_holding_anyone(fleet, hub) -> None:
    result = fleet.run("--yes", "--no-drain", "hub", fleet.shas["b"], FAKE_BUSY_AGENT="agent_natasha")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--no-drain: in-flight tasks lose the hub" in result.stdout
    assert not any(c.startswith("mac agent hold") for c in _calls(fleet))


def test_a_hub_stuck_after_migrating_keeps_its_workers_held(fleet, hub) -> None:
    result = fleet.run("--yes", "hub", fleet.shas["b"], FAKE_PENDING='["0002_more"]', FAKE_HEALTH="0")
    assert result.returncode == 1
    assert "NOT HEALTHY" in result.stdout
    assert "agent_natasha stays held. When the hub is healthy: mac agent resume agent_natasha" in result.stdout
    assert not any(c.startswith("mac agent resume") for c in _calls(fleet))
    assert json.loads(fleet.state.read_text())["dispatch_hold"] is True


def test_hub_dry_run_plans_the_drain_and_holds_no_one(fleet, hub) -> None:
    result = fleet.run("--dry-run", "hub", fleet.shas["b"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PLAN: hub: hold every worker not already held" in result.stdout
    assert not any(c.startswith("mac agent hold") for c in _calls(fleet))
