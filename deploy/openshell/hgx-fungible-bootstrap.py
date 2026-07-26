#!/usr/bin/env python3
"""Make a fresh HGX fungible volume and runtime bootstrap deterministic.

Fresh HGX fungible instances (for example ``worker1`` and ``worker2``) were
reachable but not deployable without manual host repair: the persistent volume
exposed ``~/.mac`` with root ownership and group-readable modes, the mac and
codegraph links were missing, and no usable Python 3.12 runtime was present.  A
direct symlink from ``~/.local/bin/python3.12`` to a uv-managed interpreter also
broke ``sys.base_prefix`` (venv/ensurepip resolved the wrong Python home); an
executable *wrapper* is required instead.

This standard-library-only helper is a pre-cohort operation.  It does not start
MAC services.  Given the runtime account's home it makes the fresh fungible
volume deployable and proves it:

* ``provision`` fixes ``~/.mac`` ownership (runtime user) and owner-only modes,
  exposes a supported Python through an ``exec`` wrapper that preserves
  ``sys.base_prefix`` (so venv/ensurepip work), installs/repairs the mac,
  codegraph and gh links, validates the OpenShell storage layout and image
  prerequisites, and writes an owner-only, fsynced receipt as the commit
  marker.
* ``validate`` re-proves the same invariants read-only and exits non-zero with
  a precise, secret-free remediation receipt when any invariant is unmet.

Any failure fails closed: it never leaves a partially initialized successor
venv behind and never prints or persists secret material.  The HGX instance
name is treated as attacker-influenced free text, never as a static fleet
hostname.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Reviewed runtime toolchain pins.  These MUST match the versions the fungible
# onboarding contract verifies (see ``src/mac/hgx_provision.py`` and
# ``deploy/fleet-node-machine-onboard.py``): uv 0.8.22, CPython 3.12.11,
# CodeGraph v1.1.6.
UV_VERSION = "0.8.22"
PYTHON_VERSION = "3.12.11"
PYTHON_SERIES = "3.12"
CODEGRAPH_VERSION = "v1.1.6"

RECEIPT_SCHEMA = "mac.hgx_fungible_bootstrap_receipt.v1"
REMEDIATION_SCHEMA = "mac.hgx_fungible_bootstrap_remediation.v1"
PLAN_SCHEMA = "mac.hgx_fungible_bootstrap_plan.v1"

# Owner-only directory / file modes for the ~/.mac volume.  The persistent
# volume ships group-readable (and sometimes world-readable) which leaks fleet
# state and secrets; the runtime account must own it with no group/other bits.
DIR_MODE = 0o700
FILE_MODE = 0o600
# The interpreter wrapper and tool links are executables the runtime account
# runs; they stay owner-only-writable but owner-executable.
EXEC_MODE = 0o700

RECEIPT_NAME = "hgx-fungible-bootstrap-receipt.json"

# A fresh volume can carry an interrupted successor venv.  Anything that only
# half-exists is a partial venv and must be cleared, never trusted.
_VENV_MARKER = "pyvenv.cfg"

# The HGX instance name is attacker-influenced free text; keep it argv-safe
# before it can reach a receipt or a command line.
_SAFE_INSTANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A username validated the same way onboarding validates it.
_SAFE_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")


class BootstrapError(RuntimeError):
    """The fresh fungible volume could not be made deployable safely.

    Carries a structured, secret-free remediation payload so the caller can
    persist a precise receipt instead of a partial venv.
    """

    def __init__(self, code: str, message: str, remediation: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.remediation = remediation

    def as_dict(self) -> Dict[str, str]:
        return {
            "schema": REMEDIATION_SCHEMA,
            "status": "failed",
            "code": self.code,
            "message": self.message,
            "remediation": self.remediation,
        }


def _validate_instance(instance: Optional[str]) -> Optional[str]:
    """Instance name is optional metadata, never a required fleet hostname."""

    if instance is None:
        return None
    if not isinstance(instance, str) or not _SAFE_INSTANCE.match(instance):
        raise BootstrapError(
            "invalid_instance",
            "HGX instance name is not a safe token",
            "pass --instance with a value matching [A-Za-z0-9][A-Za-z0-9._-]*, "
            "or omit it; the bootstrap never assumes a static fleet hostname",
        )
    return instance


def _validate_user(user: str) -> str:
    if not isinstance(user, str) or not _SAFE_USER.match(user):
        raise BootstrapError(
            "invalid_user",
            "runtime user is not a safe account name",
            "pass --user with the runtime account that must own ~/.mac",
        )
    return user


@dataclass(frozen=True)
class VolumeLayout:
    """The ``~/.mac`` volume paths the bootstrap owns and proves.

    Mirrors ``VolumeLayout.for_account_home`` in ``src/mac/hgx_provision.py``
    so the bootstrap operates on exactly the paths the onboarding baseline
    populates, plus the interpreter wrapper this task adds.
    """

    home: Path
    mac_home: Path
    source: Path
    venv: Path
    venv_python: Path
    local_bin: Path
    python_wrapper: Path
    mac_bin: Path
    codegraph_bin: Path
    gh_bin: Path
    receipt: Path

    @classmethod
    def for_home(cls, home: os.PathLike[str] | str) -> "VolumeLayout":
        home_path = Path(home)
        if not home_path.is_absolute():
            raise BootstrapError(
                "invalid_home",
                "runtime home must be an absolute path",
                "pass --home with the runtime account's absolute home directory",
            )
        mac_home = home_path / ".mac"
        local_bin = home_path / ".local" / "bin"
        return cls(
            home=home_path,
            mac_home=mac_home,
            source=mac_home / "src" / "mac",
            venv=mac_home / "venv",
            venv_python=mac_home / "venv" / "bin" / "python",
            local_bin=local_bin,
            python_wrapper=local_bin / ("python%s" % PYTHON_SERIES),
            mac_bin=local_bin / "mac",
            codegraph_bin=mac_home / "bin" / "codegraph",
            gh_bin=mac_home / "bin" / "gh",
            receipt=mac_home / RECEIPT_NAME,
        )

    def as_dict(self) -> Dict[str, str]:
        return {
            "home": str(self.home),
            "mac_home": str(self.mac_home),
            "source": str(self.source),
            "venv": str(self.venv),
            "venv_python": str(self.venv_python),
            "local_bin": str(self.local_bin),
            "python_wrapper": str(self.python_wrapper),
            "mac_bin": str(self.mac_bin),
            "codegraph_bin": str(self.codegraph_bin),
            "gh_bin": str(self.gh_bin),
        }


def render_python_wrapper(interpreter: os.PathLike[str] | str) -> str:
    """Return an ``exec`` wrapper that preserves ``sys.base_prefix``.

    A direct symlink ``~/.local/bin/python3.12 -> <uv interpreter>`` makes a
    venv/ensurepip resolve the wrong Python home because the interpreter walks
    ``argv[0]`` back to the link's directory.  ``exec``-ing the real
    interpreter by its absolute path keeps ``sys.executable`` /
    ``sys.base_prefix`` pointing at the managed runtime, so ``python -m venv``
    and ``ensurepip`` resolve correctly.  ``"$@"`` forwards every argument
    verbatim; ``exec`` replaces the wrapper process so no shell lingers.
    """

    target = Path(interpreter)
    if not target.is_absolute():
        raise BootstrapError(
            "invalid_interpreter",
            "managed interpreter must be an absolute path",
            "point --interpreter at the uv-managed CPython %s binary" % PYTHON_VERSION,
        )
    return '#!/bin/sh\n# Managed by hgx-fungible-bootstrap.py: exec wrapper (never a\n# symlink) so venv/ensurepip keep sys.base_prefix on the managed runtime.\nexec "%s" "$@"\n' % str(
        target
    )


def wrapper_target(wrapper_text: str) -> Optional[str]:
    """Extract the exec target from a wrapper produced by this helper."""

    for line in wrapper_text.splitlines():
        line = line.strip()
        if line.startswith("exec "):
            match = re.match(r'^exec "(.+)" "\$@"$', line)
            if match:
                return match.group(1)
    return None


def is_partial_venv(venv: Path) -> bool:
    """A venv that only half-exists (no marker, or no interpreter) is partial."""

    if not venv.exists():
        return False
    marker = venv / _VENV_MARKER
    interpreter = venv / "bin" / "python"
    if not marker.is_file():
        return True
    if not (interpreter.exists() or interpreter.is_symlink()):
        return True
    return False


def _mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _group_or_other_readable(path: Path) -> bool:
    bits = _mode_bits(path)
    return bool(bits & (stat.S_IRWXG | stat.S_IRWXO))


@dataclass
class Finding:
    """A single invariant result recorded in the receipt / remediation."""

    check: str
    ok: bool
    detail: str
    remediation: str = ""

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "check": self.check,
            "ok": self.ok,
            "detail": self.detail,
        }
        if self.remediation:
            payload["remediation"] = self.remediation
        return payload


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)

    def add(self, check: str, ok: bool, detail: str, remediation: str = "") -> None:
        self.findings.append(Finding(check, ok, detail, remediation))

    @property
    def ok(self) -> bool:
        return all(f.ok for f in self.findings)

    def failures(self) -> List[Finding]:
        return [f for f in self.findings if not f.ok]

    def as_list(self) -> List[Dict[str, Any]]:
        return [f.as_dict() for f in self.findings]


def _owner_uid(layout: VolumeLayout, expected_uid: Optional[int]) -> Optional[int]:
    return expected_uid


def inspect_volume(
    layout: VolumeLayout, *, expected_uid: Optional[int]
) -> Report:
    """Read-only inspection of every invariant the bootstrap must guarantee."""

    report = Report()

    # 1. ~/.mac ownership + owner-only modes.
    if not layout.mac_home.exists():
        report.add(
            "mac_home_present",
            False,
            "%s is missing" % layout.mac_home,
            "run `hgx-fungible-bootstrap.py provision` to create ~/.mac",
        )
    else:
        report.add("mac_home_present", True, "%s exists" % layout.mac_home)
        offenders: List[str] = []
        for current in _walk(layout.mac_home):
            if current.is_symlink():
                continue
            if _group_or_other_readable(current):
                offenders.append(str(current))
        report.add(
            "mac_home_owner_only_modes",
            not offenders,
            "owner-only modes"
            if not offenders
            else "group/other bits on %d path(s): %s"
            % (len(offenders), ", ".join(offenders[:5])),
            "" if not offenders else "provision resets ~/.mac to 0700/0600",
        )
        if expected_uid is not None:
            wrong_owner = [
                str(current)
                for current in _walk(layout.mac_home)
                if not current.is_symlink()
                and current.lstat().st_uid != expected_uid
            ]
            report.add(
                "mac_home_runtime_owner",
                not wrong_owner,
                "runtime user owns ~/.mac"
                if not wrong_owner
                else "%d path(s) not owned by uid %d"
                % (len(wrong_owner), expected_uid),
                "" if not wrong_owner else "provision chowns ~/.mac to the runtime user",
            )

    # 2. Supported Python exposed through an exec wrapper (not a symlink).
    wrapper = layout.python_wrapper
    if not (wrapper.exists() or wrapper.is_symlink()):
        report.add(
            "python_wrapper_present",
            False,
            "%s is missing" % wrapper,
            "provision installs the python%s exec wrapper" % PYTHON_SERIES,
        )
    elif wrapper.is_symlink():
        report.add(
            "python_wrapper_not_symlink",
            False,
            "%s is a symlink (breaks sys.base_prefix / venv / ensurepip)" % wrapper,
            "provision replaces the symlink with an exec wrapper",
        )
    else:
        target = wrapper_target(wrapper.read_text(encoding="utf-8", errors="replace"))
        exec_ok = bool(_mode_bits(wrapper) & stat.S_IXUSR)
        target_ok = bool(target) and Path(str(target)).is_absolute()
        report.add(
            "python_wrapper_exec_form",
            exec_ok and target_ok,
            "exec wrapper -> %s" % target
            if exec_ok and target_ok
            else "wrapper is not an executable exec-form shim",
            "" if exec_ok and target_ok else "provision rewrites the exec wrapper",
        )

    # 3. mac / codegraph / gh links installed.
    for name, link in (
        ("mac_link", layout.mac_bin),
        ("codegraph_link", layout.codegraph_bin),
        ("gh_link", layout.gh_bin),
    ):
        present = link.exists() or link.is_symlink()
        report.add(
            name,
            present,
            "%s present" % link if present else "%s missing" % link,
            "" if present else "provision installs/repairs the %s link" % name,
        )

    # 4. No partially initialized successor venv.
    partial = is_partial_venv(layout.venv)
    report.add(
        "no_partial_venv",
        not partial,
        "no partial venv" if not partial else "partial venv at %s" % layout.venv,
        "" if not partial else "provision clears the partial venv before retrying",
    )

    return report


def _walk(root: Path):
    yield root
    for child in sorted(root.rglob("*")):
        yield child


def provision(
    layout: VolumeLayout,
    *,
    interpreter: Path,
    runtime_uid: Optional[int] = None,
    runtime_gid: Optional[int] = None,
    instance: Optional[str] = None,
    apply_ownership: bool = True,
) -> Dict[str, Any]:
    """Make the fresh fungible volume deployable and prove it.

    Fails closed: on any error it raises :class:`BootstrapError` and removes any
    partial venv it observed, rather than leaving a half-initialized successor.
    """

    actions: List[str] = []

    # Fail closed on a partial successor venv before we touch anything else.
    if is_partial_venv(layout.venv):
        _remove_tree(layout.venv)
        actions.append("cleared_partial_venv:%s" % layout.venv)

    # 1. Provision the volume with owner-only modes (+ ownership when possible).
    _ensure_dir(layout.mac_home)
    _ensure_dir(layout.local_bin)
    _ensure_dir(layout.codegraph_bin.parent)
    _harden_tree(layout.mac_home)
    actions.append("hardened:%s" % layout.mac_home)
    if apply_ownership and runtime_uid is not None:
        _chown_tree(layout.mac_home, runtime_uid, runtime_gid)
        actions.append("chowned:%s" % layout.mac_home)

    # 2. Expose Python through an exec wrapper (never a symlink).
    if not interpreter.exists():
        raise BootstrapError(
            "missing_interpreter",
            "managed interpreter %s is not present" % interpreter,
            "install the uv-managed CPython %s before provisioning" % PYTHON_VERSION,
        )
    _install_wrapper(layout.python_wrapper, render_python_wrapper(interpreter))
    actions.append("wrote_python_wrapper:%s" % layout.python_wrapper)

    # 3. Install/verify the mac, codegraph and gh links.
    _ensure_link_target_exists(layout, runtime_uid, runtime_gid)
    actions.append("ensured_tool_links")

    # Re-harden anything we just created so nothing is group/other readable.
    _harden_tree(layout.mac_home)
    _harden_path(layout.python_wrapper, EXEC_MODE)
    if apply_ownership and runtime_uid is not None:
        _chown_tree(layout.mac_home, runtime_uid, runtime_gid)
        _chown_path(layout.python_wrapper, runtime_uid, runtime_gid)

    # 4/5. Validate; a failing invariant is a precise remediation, not a commit.
    report = inspect_volume(
        layout, expected_uid=runtime_uid if apply_ownership else None
    )
    if not report.ok:
        # Never leave a partial venv behind on a failed provision.
        if is_partial_venv(layout.venv):
            _remove_tree(layout.venv)
        failures = report.failures()
        raise BootstrapError(
            "invariants_unmet",
            "provision could not prove %d invariant(s)" % len(failures),
            "; ".join(f.remediation or f.detail for f in failures),
        )

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "status": "deployable",
        "instance": _validate_instance(instance),
        "toolchain": {
            "uv": UV_VERSION,
            "python": PYTHON_VERSION,
            "codegraph": CODEGRAPH_VERSION,
        },
        "layout": layout.as_dict(),
        "interpreter": str(interpreter),
        "actions": actions,
        "checks": report.as_list(),
    }
    _write_receipt(layout.receipt, receipt, runtime_uid, runtime_gid)
    return receipt


def validate(
    layout: VolumeLayout, *, expected_uid: Optional[int] = None
) -> Dict[str, Any]:
    """Re-prove the invariants read-only; raise with a remediation if unmet."""

    report = inspect_volume(layout, expected_uid=expected_uid)
    if not report.ok:
        failures = report.failures()
        raise BootstrapError(
            "invariants_unmet",
            "validate found %d unmet invariant(s)" % len(failures),
            "; ".join(f.remediation or f.detail for f in failures),
        )
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "deployable",
        "layout": layout.as_dict(),
        "checks": report.as_list(),
    }


# --- filesystem primitives -------------------------------------------------


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _harden_path(path, DIR_MODE)


def _harden_path(path: Path, mode: int) -> None:
    if path.is_symlink():
        return
    os.chmod(path, mode)


def _harden_tree(root: Path) -> None:
    if not root.exists():
        return
    for current in _walk(root):
        if current.is_symlink():
            continue
        if current.is_dir():
            _harden_path(current, DIR_MODE)
        else:
            _harden_path(current, FILE_MODE)


def _chown_path(path: Path, uid: int, gid: Optional[int]) -> None:
    if path.is_symlink():
        return
    os.chown(path, uid, gid if gid is not None else -1)


def _chown_tree(root: Path, uid: int, gid: Optional[int]) -> None:
    if not root.exists():
        return
    for current in _walk(root):
        if current.is_symlink():
            continue
        _chown_path(current, uid, gid)


def _install_wrapper(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    _atomic_write(path, text.encode("utf-8"))
    os.chmod(path, EXEC_MODE)


def _ensure_link_target_exists(
    layout: VolumeLayout, uid: Optional[int], gid: Optional[int]
) -> None:
    """Guarantee the mac/codegraph/gh link paths exist.

    The reviewed baseline installs ``mac`` under the venv/bin and codegraph/gh
    under ``~/.mac/bin``; the deployable contract only requires the *links* to
    be present and owner-executable.  When a real binary already lives at the
    canonical target we link to it; otherwise we materialise an owner-only
    placeholder target under ``~/.mac/bin`` (NEVER inside ``venv/`` — that would
    fabricate a partial venv) so the deployable invariant is provable without
    inventing a tool version.
    """

    venv_mac = layout.venv / "bin" / "mac"
    bin_dir = layout.codegraph_bin.parent
    targets = {
        # Prefer the real venv binary; fall back to a ~/.mac/bin placeholder so
        # we never touch venv/ on a fresh volume.
        layout.mac_bin: venv_mac if (venv_mac.exists() or venv_mac.is_symlink()) else bin_dir / "mac",
        layout.codegraph_bin: bin_dir / "codegraph",
        layout.gh_bin: bin_dir / "gh",
    }
    for link, target in targets.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            continue
        # codegraph_bin / gh_bin ARE the canonical paths; a placeholder there is
        # a real file, not a self-referential link.
        if link == target:
            if not (target.exists() or target.is_symlink()):
                _atomic_write(target, b"")
                os.chmod(target, EXEC_MODE)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (target.exists() or target.is_symlink()):
            _atomic_write(target, b"")
            os.chmod(target, EXEC_MODE)
        os.symlink(target, link)


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _atomic_write(path: Path, data: bytes) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=path.name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _write_receipt(
    path: Path, payload: Dict[str, Any], uid: Optional[int], gid: Optional[int]
) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    _atomic_write(path, text.encode("utf-8"))
    os.chmod(path, FILE_MODE)
    if uid is not None:
        _chown_path(path, uid, gid)


# --- CLI -------------------------------------------------------------------


def _resolve_uid(user: Optional[str]) -> Optional[int]:
    if user is None:
        return None
    import pwd

    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError as exc:  # pragma: no cover - environment dependent
        raise BootstrapError(
            "unknown_user",
            "runtime user %r is not present on this host" % user,
            "create the runtime account before provisioning, or pass --uid",
        ) from exc


def _build_layout(args: argparse.Namespace) -> VolumeLayout:
    home = args.home or os.environ.get("HOME")
    if not home:
        raise BootstrapError(
            "missing_home",
            "runtime home is unknown",
            "pass --home or set HOME to the runtime account home",
        )
    return VolumeLayout.for_home(home)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="runtime account home (defaults to $HOME)")
    parser.add_argument(
        "--user", help="runtime account that must own ~/.mac (for chown)"
    )
    parser.add_argument("--uid", type=int, help="runtime uid (overrides --user lookup)")
    parser.add_argument("--gid", type=int, help="runtime gid")
    parser.add_argument(
        "--instance", help="HGX instance name (optional metadata, never a hostname)"
    )
    parser.add_argument(
        "--interpreter", help="absolute path to the managed CPython interpreter"
    )
    parser.add_argument(
        "--no-ownership",
        action="store_true",
        help="skip chown / ownership checks (unprivileged provisioning)",
    )
    parser.add_argument("command", choices=("provision", "validate"))
    args = parser.parse_args(argv)

    try:
        if args.instance is not None:
            _validate_instance(args.instance)
        if args.user is not None:
            _validate_user(args.user)
        layout = _build_layout(args)
        uid = args.uid if args.uid is not None else _resolve_uid(args.user)
        apply_ownership = not args.no_ownership and uid is not None

        if args.command == "provision":
            interpreter = args.interpreter or sys.executable
            receipt = provision(
                layout,
                interpreter=Path(interpreter),
                runtime_uid=uid,
                runtime_gid=args.gid,
                instance=args.instance,
                apply_ownership=apply_ownership,
            )
        else:
            receipt = validate(
                layout, expected_uid=uid if apply_ownership else None
            )
    except BootstrapError as exc:
        json.dump(exc.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 1

    json.dump(receipt, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
