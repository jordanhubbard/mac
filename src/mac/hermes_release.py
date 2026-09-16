"""Prepare qualified external Hermes releases; the existing CLI selects one.

This module does not stop services or own deployment recovery. The fleet
transaction snapshots the CLI and service definition before selecting a release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import tempfile

from mac.hermes_patch import apply_reviewed_patches


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_runtime(launcher: Path) -> Path:
    launcher = launcher.resolve(strict=True)
    text = launcher.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("# mac-hermes-runtime: "):
            runtime = Path(json.loads(line.removeprefix("# mac-hermes-runtime: ")))
            if not runtime.is_absolute() or not (runtime / "agent/prompt_builder.py").is_file():
                raise ValueError("Selected Hermes runtime is unavailable")
            return runtime.resolve(strict=True)
    matches = re.findall(r"(/[^\s\"']+)/hermes(?:\s|[\"']|$)", text)
    runtime = Path(matches[-1]) if matches else launcher.parent
    if not (runtime / "agent/prompt_builder.py").is_file():
        raise ValueError("Hermes launcher does not select a source runtime")
    return runtime.resolve(strict=True)


def run(argv: list[str], *, cwd: Path | None = None) -> str:
    # A deployment's Python and uv variables must not redirect the candidate
    # into the running MAC environment. Never relay captured tool output: git
    # and package resolvers can include authenticated URLs in errors.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "UV_"))}
    env.pop("VIRTUAL_ENV", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode:
        raise RuntimeError(
            f"Hermes preparation failed: {Path(argv[0]).name} exited {result.returncode}"
        )
    return result.stdout.strip()


def recipe(manifests: list[Path]) -> dict:
    values = [json.loads(p.read_text()) for p in manifests]
    for path, value in zip(manifests, values):
        if digest(path.with_name(value["patch"])) != value["patch_sha256"]:
            raise ValueError("Reviewed Hermes patch digest mismatch")
    repositories = {v["upstream_repository"] for v in values}
    revisions = {v["upstream_commit"] for v in values}
    if len(repositories) != 1 or len(revisions) != 1:
        raise ValueError("Hermes manifests must name one upstream source")
    baseline = next(v for v in values if "python_version" in v)
    return {
        "schema": "mac.hermes_release.v1",
        "repository": repositories.pop(),
        "revision": revisions.pop(),
        "python": baseline["python_version"],
        "uv": baseline["uv_version"],
        "manifests": {p.name: digest(p) for p in manifests},
        "extras": ["slack", "mcp"],
    }


def source_identity(runtime: Path) -> dict[str, str]:
    names = run(["git", "ls-files", "-z"], cwd=runtime).split("\0")
    return {name: digest(runtime / name) for name in names if name}


PROBE = """
import importlib.metadata,json,os,platform,sys
from pathlib import Path
runtime=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(runtime))
home=Path(sys.argv[2]); markdown=Path(sys.argv[3])
os.environ['HERMES_HOME']=str(home)
os.environ['MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN']=str(markdown)
import slack_bolt,slack_sdk,aiohttp,mcp
from agent import prompt_builder
text=markdown.read_text().strip()
prompt=prompt_builder.build_context_files_prompt(cwd=str(home),home_override=home)
soul=home/'SOUL.md'
if (not text or text not in prompt
    or runtime not in Path(prompt_builder.__file__).resolve().parents
    or (soul.is_file() and soul.read_text().strip() not in prompt)):
    raise SystemExit('Hermes candidate omitted required prompt context')
print(json.dumps({'python':platform.python_version(),
 'packages':sorted((d.metadata['Name'],d.version) for d in importlib.metadata.distributions())}))
"""


def qualify(runtime: Path, home: Path, markdown: Path, expected: dict) -> dict:
    observed = json.loads(
        run(
            [
                str(runtime / ".venv/bin/python"),
                "-c",
                PROBE,
                str(runtime),
                str(home),
                str(markdown),
            ]
        ).splitlines()[-1]
    )
    if observed["python"] != expected["python"]:
        raise ValueError("Hermes candidate Python differs from the reviewed baseline")
    return observed


def verify(runtime: Path, home: Path, markdown: Path, expected: dict) -> None:
    receipt = json.loads((runtime.parent / "qualification.json").read_text())
    if receipt["recipe"] != expected or receipt["runtime"] != str(runtime):
        raise ValueError("Hermes release does not match the selected recipe")
    if receipt["source"] != source_identity(runtime):
        raise ValueError("Qualified Hermes source changed")
    if receipt["environment"] != qualify(runtime, home, markdown, expected):
        raise ValueError("Qualified Hermes dependencies changed")


def prepare(
    root: Path,
    manifests: list[Path],
    home: Path,
    markdown: Path,
    uv: str,
    launcher: Path,
) -> Path:
    expected = recipe(manifests)
    if run([uv, "--version"]).split()[:2] != ["uv", expected["uv"]]:
        raise ValueError("Hermes preparation requires the reviewed uv version")
    active = None
    if launcher.exists():
        active = resolve_runtime(launcher)
        if (active.parent / "qualification.json").is_file():
            try:
                verify(active, home, markdown, expected)
            except (ValueError, RuntimeError, KeyError, OSError):
                pass  # Build a replacement; never resync the active runtime.
            else:
                return active
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    release = Path(tempfile.mkdtemp(prefix="release-", dir=root)).resolve()
    runtime = release / "runtime"
    # Reuse locally available Git objects, but never copy an active environment
    # or uncommitted source changes. Fetch only the manifest's exact revision.
    source = str(active) if active and (active / ".git").exists() else expected["repository"]
    run(["git", "init", str(runtime)])
    try:
        run(["git", "fetch", "--depth", "1", source, expected["revision"]], cwd=runtime)
    except RuntimeError:
        if source == expected["repository"]:
            raise
        run(
            ["git", "fetch", "--depth", "1", expected["repository"], expected["revision"]],
            cwd=runtime,
        )
    run(["git", "checkout", "--detach", expected["revision"]], cwd=runtime)
    apply_reviewed_patches(runtime, manifests)
    run(
        [
            uv,
            "sync",
            "--locked",
            "--no-install-project",
            "--no-default-groups",
            "--python",
            expected["python"],
            "--extra",
            "slack",
            "--extra",
            "mcp",
        ],
        cwd=runtime,
    )
    # Upstream service installation uses venv/, while uv owns .venv/.
    (runtime / "venv").symlink_to(".venv", target_is_directory=True)
    observed = qualify(runtime, home, markdown, expected)
    receipt = {
        "recipe": expected,
        "runtime": str(runtime),
        "source": source_identity(runtime),
        "environment": observed,
    }
    atomic_write(release / "qualification.json", json.dumps(receipt, sort_keys=True) + "\n", 0o600)
    return runtime


def atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def activate(
    runtime: Path, launcher: Path, home: Path, markdown: Path, manifests: list[Path]
) -> None:
    verify(runtime, home, markdown, recipe(manifests))
    atomic_write(launcher, launcher_text(runtime), 0o755)


def launcher_text(runtime: Path) -> str:
    return (
        "#!/bin/sh\n# mac-hermes-runtime: "
        + json.dumps(str(runtime))
        + "\nunset PYTHONPATH PYTHONHOME\nexec "
        + shlex.quote(str(runtime / ".venv/bin/python"))
        + " "
        + shlex.quote(str(runtime / "hermes"))
        + ' "$@"\n'
    )


def verify_service(runtime: Path, user_home: Path, profile: Path) -> None:
    """Prove upstream wrote the service for the selected runtime and profile."""
    plist = user_home / "Library/LaunchAgents/ai.hermes.gateway.plist"
    unit = user_home / ".config/systemd/user/hermes-gateway.service"
    if plist.exists():
        with plist.open("rb") as stream:
            service = plistlib.load(stream)
        argv = service.get("ProgramArguments", [])
        selected_profile = service.get("EnvironmentVariables", {}).get("HERMES_HOME")
    elif unit.exists():
        argv = []
        selected_profile = None
        for line in unit.read_text().splitlines():
            if line.startswith("ExecStart="):
                argv = shlex.split(line.removeprefix("ExecStart="))
            if line.startswith("Environment="):
                for entry in shlex.split(line.removeprefix("Environment=")):
                    if entry.startswith("HERMES_HOME="):
                        selected_profile = entry.removeprefix("HERMES_HOME=")
    else:
        raise ValueError("Selected Hermes runtime has no installed user service")
    if not argv or "hermes_cli.main" not in argv or selected_profile != str(profile):
        raise ValueError("Hermes service command or profile differs from selection")
    # Resolve the environment directory, not Python itself: different venvs
    # can have symlinks to the same base interpreter.
    executable = Path(argv[0])
    if (
        executable.name not in {"python", "python3"}
        or executable.parent.parent.resolve() != (runtime / ".venv").resolve()
    ):
        raise ValueError("Hermes service interpreter differs from selected environment")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("resolve", "prepare", "activate", "verify"))
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--manifest", type=Path, action="append")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args()
    if args.action == "resolve":
        print(resolve_runtime(args.launcher))
        return
    if not args.home or not args.markdown or not args.manifest:
        parser.error("home, markdown and manifests are required")
    if args.action == "prepare":
        if not args.root:
            parser.error("prepare requires a release root")
        print(prepare(args.root, args.manifest, args.home, args.markdown, args.uv, args.launcher))
    elif args.action == "activate":
        if not args.runtime:
            parser.error("activate requires a candidate runtime")
        activate(args.runtime, args.launcher, args.home, args.markdown, args.manifest)
    else:
        runtime = resolve_runtime(args.launcher)
        if args.launcher.read_text() != launcher_text(runtime):
            raise ValueError("Hermes launcher differs from the qualified runtime selection")
        verify(runtime, args.home, args.markdown, recipe(args.manifest))
        verify_service(runtime, Path.home(), args.home)


if __name__ == "__main__":
    main()
