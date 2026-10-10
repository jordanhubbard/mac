"""The files a MAC node owns, and the one routine that installs them.

fleet-update used to copy a hand-kept list of wrappers on workers and a second
copy of that list on the hub, never removed anything, and left
``~/.mac/bin/mac-service`` to be replaced by hand. This module declares the
managed file set per role once, and installs it idempotently:

- every declared file is installed atomically (temp file + rename) with its
  declared mode, and reported ``installed``, ``updated`` or ``unchanged``;
- what was installed is recorded in ``$MAC_HOME/managed-files.json`` with its
  digest. A file recorded there that is no longer declared is removed, after a
  copy into ``$MAC_HOME/backups/node-files-<ts>/``. A recorded file that was
  changed by hand since is kept and reported, never silently deleted;
- files never recorded here are not this module's to remove;
- one installer runs per host at a time (an exclusive lock on
  ``$MAC_HOME/.node-files.lock``).

Running it twice is a no-op. It prints one JSON line.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCHEMA = "mac.node_files.v1"
STATE_NAME = "managed-files.json"
LOCK_NAME = ".node-files.lock"

# (source path in the checkout, path under $MAC_HOME, mode)
_AGENT_FILES: Tuple[Tuple[str, str, int], ...] = (
    ("deploy/bin/mac-agent-service", "bin/mac-agent-service", 0o700),
    ("deploy/bin/mac-agent-startup-self-test", "bin/mac-agent-startup-self-test", 0o700),
    ("deploy/bin/mac-task-executor", "bin/mac-task-executor", 0o700),
    ("deploy/bin/mac-task-executor.py", "bin/mac-task-executor.py", 0o600),
    ("deploy/mac-crash-observer.py", "bin/mac-crash-observer", 0o755),
)

#: The hub runs a MAC agent too, under the same wrappers, plus the control
#: plane's own wrapper (the LaunchDaemon runs ``bin/mac-service``).
MANAGED: Dict[str, Tuple[Tuple[str, str, int], ...]] = {
    "worker": _AGENT_FILES,
    "hub": _AGENT_FILES + (("deploy/bin/mac-service", "bin/mac-service", 0o755),),
}


class NodeFilesError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_state(home: Path) -> Dict[str, Dict[str, str]]:
    path = home / STATE_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise NodeFilesError("cannot read %s: %s" % (path, exc)) from exc
    files = value.get("files") if isinstance(value, dict) else None
    return dict(files) if isinstance(files, dict) else {}


def _write_atomic(target: Path, data: bytes, mode: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".%s." % target.name, dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def plan(source: Path, home: Path, role: str) -> Dict[str, object]:
    """What ``apply`` would do, without changing anything."""
    if role not in MANAGED:
        raise NodeFilesError("unknown role %r; expected one of %s" % (role, sorted(MANAGED)))
    previous = _load_state(home)
    actions: Dict[str, str] = {}
    for src_rel, dest_rel, mode in MANAGED[role]:
        origin = source / src_rel
        if not origin.is_file():
            raise NodeFilesError("the checkout has no %s" % src_rel)
        target = home / dest_rel
        if not target.exists():
            actions[dest_rel] = "install"
        elif _digest(target) != _digest(origin) or (target.stat().st_mode & 0o777) != mode:
            actions[dest_rel] = "update"
        else:
            actions[dest_rel] = "unchanged"
    declared = {dest for _, dest, _ in MANAGED[role]}
    for dest_rel, record in sorted(previous.items()):
        if dest_rel in declared:
            continue
        target = home / dest_rel
        if not target.exists():
            actions[dest_rel] = "already_removed"
        elif _digest(target) != record.get("sha256"):
            actions[dest_rel] = "kept_modified"
        else:
            actions[dest_rel] = "remove"
    return {"schema": SCHEMA, "role": role, "home": str(home), "actions": actions}


def apply(source: Path, home: Path, role: str) -> Dict[str, object]:
    """Converge the node's managed files on ``source`` for ``role``."""
    home.mkdir(parents=True, exist_ok=True)
    with open(home / LOCK_NAME, "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise NodeFilesError("another installer holds %s" % (home / LOCK_NAME)) from exc
        result = plan(source, home, role)
        actions: Dict[str, str] = dict(result["actions"])  # type: ignore[arg-type]
        previous = _load_state(home)
        state: Dict[str, Dict[str, str]] = {
            dest: record
            for dest, record in previous.items()
            if actions.get(dest) == "kept_modified"
        }
        backup: Optional[Path] = None
        for src_rel, dest_rel, mode in MANAGED[role]:
            data = (source / src_rel).read_bytes()
            if actions[dest_rel] != "unchanged":
                _write_atomic(home / dest_rel, data, mode)
            actions[dest_rel] = {"install": "installed", "update": "updated"}.get(
                actions[dest_rel], actions[dest_rel]
            )
            state[dest_rel] = {
                "source": src_rel,
                "sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                "mode": "%04o" % mode,
            }
        for dest_rel, action in sorted(actions.items()):
            if action != "remove":
                continue
            if backup is None:
                backup = home / "backups" / ("node-files-%s" % time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            saved = backup / dest_rel
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(home / dest_rel, saved)
            (home / dest_rel).unlink()
            actions[dest_rel] = "removed"
        _write_atomic(
            home / STATE_NAME,
            (json.dumps({"schema": SCHEMA, "role": role, "files": state}, indent=2, sort_keys=True) + "\n").encode(),
            0o600,
        )
        result["actions"] = actions
        result["changed"] = sorted(a for a, v in actions.items() if v in {"installed", "updated", "removed"})
        if backup is not None:
            result["backup"] = str(backup)
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.node_files")
    parser.add_argument("--source", type=Path, required=True, help="the MAC checkout")
    parser.add_argument("--role", required=True, choices=sorted(MANAGED))
    parser.add_argument("--home", type=Path, help="MAC home (default: mac_paths.mac_home())")
    parser.add_argument("--plan", action="store_true", help="report only; change nothing")
    args = parser.parse_args(argv)
    if args.home is None:
        from mac import mac_paths

        home = mac_paths.mac_home()
    else:
        home = args.home
    try:
        result = (plan if args.plan else apply)(args.source.resolve(), home.expanduser(), args.role)
    except NodeFilesError as exc:
        print(json.dumps({"schema": SCHEMA, "status": "error", "error": str(exc)}))
        return 2
    result["status"] = "ok"
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
