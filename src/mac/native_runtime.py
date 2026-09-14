"""Install the native service closure from uv.lock without discarding agent tools.

The deployment transaction owns source/venv replacement and rollback. This module
only prepares that venv and the constraints used by subsequent pip installs.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib


CONSTRAINTS = "mac-runtime-constraints.txt"
MANIFEST = "mac-runtime-lock.json"
SCHEMA = "mac.native_runtime.v1"


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_private(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def install_lock(home: Path):
    import fcntl

    with (home / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def source_hashes(source: Path) -> dict[str, str]:
    return {name: digest(source / name) for name in ("uv.lock", "pyproject.toml")}


def pip_constraints(python: str, home: Path, installed: dict[str, str]) -> list[str]:
    """Fail closed for a managed venv; unrelated development venvs stay separate."""
    venv = home / "venv"
    if Path(python).absolute() != (venv / "bin" / "python").absolute():
        return []
    try:
        record = json.loads((venv / MANIFEST).read_text())
        valid = (
            record["schema"] == SCHEMA
            and record["python_version"] == (home / "src/mac/.python-version").read_text().strip()
            and isinstance(record["core_packages"], dict)
            and bool(record["core_packages"])
            and record["source_hashes"] == source_hashes(home / "src" / "mac")
            and record["constraints_sha256"] == digest(venv / CONSTRAINTS)
            and all(
                installed.get(name) == version for name, version in record["core_packages"].items()
            )
        )
        if valid:
            actual_python = subprocess.run(
                [python, "-c", "import platform; print(platform.python_version())"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            valid = actual_python == record["python_version"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
        valid = False
    if not valid:
        raise RuntimeError(
            "native runtime baseline is missing or changed; redeploy the locked runtime"
        )
    return ["--constraint", str(venv / CONSTRAINTS)]


def inventory(python: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(python), "-m", "pip", "list", "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {normalized(item["name"]): item["version"] for item in json.loads(result.stdout)}


def restored_requirement(package: dict) -> str:
    name = package["name"]
    direct = package.get("direct_url")
    if not direct:
        return f"{name}=={package['version']}"
    url = direct["url"]
    if "\n" in url or "\r" in url:
        raise RuntimeError("native runtime tool URL is invalid")
    vcs = direct.get("vcs_info")
    if vcs:
        url = f"{vcs['vcs']}+{url}@{vcs['commit_id']}"
    suffix = ""
    if direct.get("subdirectory"):
        suffix = "subdirectory=" + direct["subdirectory"]
    hashes = direct.get("archive_info", {}).get("hashes", {})
    if hashes.get("sha256"):
        suffix = (suffix + "&" if suffix else "") + "sha256=" + hashes["sha256"]
    if suffix:
        url += ("&" if "#" in url else "#") + suffix
    if direct.get("dir_info", {}).get("editable"):
        return "-e " + url
    return name + " @ " + url


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, text=True, capture_output=True, **kwargs)
    if result.returncode:
        # Resolver diagnostics may contain credential-bearing package URLs.
        # Keep the original in a private file, never in fleet telemetry.
        fd, log = tempfile.mkstemp(prefix="mac-native-runtime-failure-", suffix=".log")
        with os.fdopen(fd, "w") as stream:
            stream.write(result.stdout + result.stderr)
        raise RuntimeError(
            f"native runtime {Path(argv[0]).name} failed (exit {result.returncode}); private diagnostics: {log}"
        )
    return result


def install(source: Path, venv: Path, snapshot: Path, footprint: Path, uv: str) -> dict:
    source = source.resolve()
    home = venv.parent
    expected = (source / ".python-version").read_text().strip()
    if sys.version.split()[0] != expected:
        raise RuntimeError(f"native runtime requires Python {expected}")
    original_hashes = source_hashes(source)
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    saved = json.loads(snapshot.read_text())
    recorded = saved.get("footprint")
    if recorded is None:
        recorded = json.loads(footprint.read_text()) if footprint.exists() else {}
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(venv))
    for key in ("UV_NO_SYNC", "UV_FROZEN"):
        env.pop(key, None)
    with install_lock(home):
        # A local update can arrive after the deployment snapshot. Retain it,
        # together with hub-only requests captured before source replacement.
        if footprint.exists():
            local = json.loads(footprint.read_text())
            merged = {**recorded, **local}
            for manager in ("pip", "npm"):
                entries = {}
                for record in (recorded, local):
                    for entry in record.get(manager, []):
                        entries[entry.get("name") or entry["spec"]] = entry
                merged[manager] = list(entries.values())
            recorded = merged
        # A fresh deployment has a new venv; --inexact also makes repeat setup
        # preserve unrelated packages rather than silently removing them.
        _run([sys.executable, "-m", "venv", str(venv)])
        exported = _run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--extra",
                "relay",
                "--extra",
                "postgres",
                "--no-emit-project",
                "--no-hashes",
                "--format",
                "requirements-txt",
            ],
            cwd=str(source),
            env=env,
        ).stdout
        _run(
            [
                uv,
                "sync",
                "--locked",
                "--no-dev",
                "--extra",
                "relay",
                "--extra",
                "postgres",
                "--inexact",
                "--python",
                sys.executable,
            ],
            cwd=str(source),
            env=env,
        )
        python = venv / "bin" / "python"
        current = inventory(python)
        # Evaluate the universal export using the same marker implementation
        # and interpreter that will enforce it during pip installs. Intersecting
        # names alone would turn a retained tool into a core package on a repeat
        # install merely because another platform uses that name as a dependency.
        selected = _run(
            [
                str(python),
                "-c",
                """
import json, sys
from pip._vendor.packaging.requirements import Requirement
names = []
for line in sys.stdin:
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    req = Requirement(line)
    if req.marker is None or req.marker.evaluate():
        names.append(req.name)
print(json.dumps(names))
""",
            ],
            input=exported,
        )
        names = {normalized(name) for name in json.loads(selected.stdout)}
        core = {name: version for name, version in current.items() if name in names}
        own_name = normalized(project["name"])
        if own_name in current:
            core[own_name] = current[own_name]
            exported += f"\n{own_name}=={current[own_name]}\n"
        write_private(venv / CONSTRAINTS, exported)
        manifest = {
            "schema": SCHEMA,
            "python_version": expected,
            "source_hashes": original_hashes,
            "constraints_sha256": digest(venv / CONSTRAINTS),
            "core_packages": core,
        }
        write_private(venv / MANIFEST, json.dumps(manifest, indent=2))
        extras = [
            p
            for p in saved.get("packages", [])
            if normalized(p["name"]) not in core and normalized(p["name"]) != own_name
        ]
        if extras:
            requests = venv / "mac-runtime-restored-tools.txt"
            write_private(requests, "\n".join(restored_requirement(p) for p in extras) + "\n")
            try:
                _run(
                    [
                        str(python),
                        "-m",
                        "pip",
                        "install",
                        "--constraint",
                        str(venv / CONSTRAINTS),
                        "--requirement",
                        str(requests),
                    ]
                )
            finally:
                requests.unlink(missing_ok=True)
        for entry in recorded.get("pip", []):
            spec = entry.get("spec") or entry.get("name")
            if not spec or str(spec).lstrip().startswith("-"):
                raise RuntimeError("native runtime footprint contains an invalid package request")
            argv = [
                str(python),
                "-m",
                "pip",
                "install",
                "--constraint",
                str(venv / CONSTRAINTS),
                spec,
            ]
            if entry.get("index_url"):
                argv += ["--index-url", entry["index_url"]]
            _run(argv)
        _run([str(python), "-m", "pip", "check"])
        if source_hashes(source) != original_hashes:
            raise RuntimeError("native runtime source changed during installation")
        installed = inventory(python)
        pip_constraints(str(python), home, installed)
        # Subsequent worker reports must retain requests recovered from the hub.
        # The deployer snapshots this file in its existing rollback journal.
        write_private(footprint, json.dumps(recorded, indent=2))
        return {
            **manifest,
            "installed_packages": installed,
            "restored_package_names": sorted(normalized(p["name"]) for p in extras),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "venv", "snapshot", "footprint", "record"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--uv", required=True)
    args = parser.parse_args()
    result = install(args.source, args.venv, args.snapshot, args.footprint, args.uv)
    write_private(args.record, json.dumps(result, indent=2))
    print(
        f"native runtime locked: Python {result['python_version']}, {len(result['core_packages'])} core packages"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
