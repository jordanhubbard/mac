from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

JsonDict = dict[str, Any]

CODEGRAPH_AUDIT_SCHEMA = "mac.codegraph_audit.v1"

_SOURCE_EXTENSIONS = frozenset(
    {
        ".bash",
        ".bazel",
        ".bzl",
        ".c",
        ".cc",
        ".clj",
        ".cljs",
        ".cpp",
        ".cs",
        ".css",
        ".cfg",
        ".dart",
        ".erl",
        ".ex",
        ".exs",
        ".fish",
        ".fs",
        ".fsx",
        ".go",
        ".h",
        ".hcl",
        ".hpp",
        ".hrl",
        ".html",
        ".ini",
        ".java",
        ".jl",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lock",
        ".lua",
        ".m",
        ".mjs",
        ".ml",
        ".mli",
        ".mm",
        ".php",
        ".py",
        ".r",
        ".rb",
        ".rs",
        ".scala",
        ".scss",
        ".sh",
        ".svelte",
        ".swift",
        ".tf",
        ".tfvars",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
        ".yaml",
        ".yml",
        ".zsh",
    }
)

_SOURCE_BASENAMES = frozenset(
    {
        "BUILD",
        "BUILD.bazel",
        "Cargo.toml",
        "CMakeLists.txt",
        "Containerfile",
        "Dockerfile",
        "Gemfile",
        "Gemfile.lock",
        "MODULE.bazel",
        "Makefile",
        "Pipfile",
        "Pipfile.lock",
        "Rakefile",
        "WORKSPACE",
        "WORKSPACE.bazel",
        "bun.lock",
        "bun.lockb",
        "compose.yaml",
        "compose.yml",
        "deno.json",
        "docker-compose.yaml",
        "docker-compose.yml",
        "go.mod",
        "go.sum",
        "go.work",
        "go.work.sum",
        "package-lock.json",
        "package.json",
        "poetry.lock",
        "pnpm-lock.yaml",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "tsconfig.json",
        "turbo.json",
        "uv.lock",
        "yarn.lock",
    }
)

_SOURCE_SUFFIXES = (
    ".csproj",
    ".fsproj",
    ".gradle",
    ".props",
    ".targets",
    ".vcxproj",
)


def normalize_repo_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def is_codegraph_relevant_path(path: Any) -> bool:
    normalized = normalize_repo_path(path)
    if not normalized:
        return False
    name = normalized.rsplit("/", 1)[-1]
    if name in _SOURCE_BASENAMES:
        return True
    if any(name.endswith(suffix) for suffix in _SOURCE_SUFFIXES):
        return True
    return Path(name).suffix.lower() in _SOURCE_EXTENSIONS


def codegraph_relevant_files(files: Iterable[Any]) -> list[str]:
    relevant: list[str] = []
    seen: set[str] = set()
    for item in files:
        path = normalize_repo_path(item)
        if path and path not in seen and is_codegraph_relevant_path(path):
            seen.add(path)
            relevant.append(path)
    return relevant


def codegraph_audit_required(files: Iterable[Any]) -> bool:
    return bool(codegraph_relevant_files(files))


def _resolve_codegraph_binary() -> str:
    configured = os.environ.get("MAC_CODEGRAPH_BIN", "").strip()
    candidates = [
        configured,
        shutil.which("codegraph") or "",
        "/usr/local/bin/codegraph",
        "/opt/homebrew/bin/codegraph",
        str(Path.home() / ".local" / "bin" / "codegraph"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def _git_exclude_path(repo_path: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is not None and completed.returncode == 0:
        raw = (completed.stdout or "").strip()
        if raw:
            candidate = Path(raw)
            return candidate if candidate.is_absolute() else repo_path / candidate
    git_dir = repo_path / ".git"
    if git_dir.is_dir():
        return git_dir / "info" / "exclude"
    return None


def ensure_codegraph_git_exclude(repo_path: Path) -> None:
    exclude = _git_exclude_path(repo_path)
    if exclude is None:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = {line.strip() for line in current.splitlines()}
    if ".codegraph/" not in lines and ".codegraph" not in lines:
        suffix = "" if current.endswith("\n") or not current else "\n"
        exclude.write_text(current + suffix + ".codegraph/\n", encoding="utf-8")


def _truncate(value: str, limit: int = 8000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _command_record(
    argv: list[str],
    *,
    returncode: int,
    stdout: str = "",
    stderr: str = "",
    duration_ms: int = 0,
) -> JsonDict:
    display = ["codegraph", *argv[1:]] if argv else []
    return {
        "argv": display,
        "returncode": int(returncode),
        "status": "pass" if int(returncode) == 0 else "fail",
        "stdout": _truncate(stdout),
        "stderr": _truncate(stderr),
        "duration_ms": int(duration_ms),
    }


def _run_codegraph_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    input_text: str | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> JsonDict:
    started = time.monotonic()
    try:
        completed = runner(
            argv,
            cwd=str(cwd),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return _command_record(
            argv,
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return _command_record(
            argv,
            returncode=124,
            stdout=stdout,
            stderr=stderr or "codegraph command timed out",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except OSError as exc:
        return _command_record(
            argv,
            returncode=1,
            stderr=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def run_codegraph_audit(
    repo_path: Path,
    files_changed: Iterable[Any],
    *,
    timeout: float = 180.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> JsonDict:
    deadline = time.monotonic() + max(0.001, float(timeout))

    def remaining(*, cap: float | None = None) -> float:
        value = max(0.001, deadline - time.monotonic())
        return min(value, cap) if cap is not None else value

    relevant = codegraph_relevant_files(files_changed)
    audit: JsonDict = {
        "schema": CODEGRAPH_AUDIT_SCHEMA,
        "status": "skipped",
        "reason": "non_code_change",
        "relevant_files": relevant,
        "commands": [],
    }
    if not relevant:
        return audit

    binary = _resolve_codegraph_binary()
    if not binary:
        audit.update(
            {
                "status": "fail",
                "reason": "codegraph_not_available",
                "error": "codegraph is required for source/build changes but was not found on PATH",
            }
        )
        return audit

    try:
        ensure_codegraph_git_exclude(repo_path)
    except OSError as exc:
        audit.setdefault("warnings", []).append("could not update .git/info/exclude: %s" % exc)

    index_cmd = [binary, "sync", str(repo_path)] if (repo_path / ".codegraph").exists() else [binary, "init", str(repo_path)]
    index_result = _run_codegraph_command(
        index_cmd, cwd=repo_path, timeout=remaining(), runner=runner
    )
    audit["commands"].append(index_result)
    if index_result["returncode"] != 0:
        combined = "%s\n%s" % (index_result.get("stdout", ""), index_result.get("stderr", ""))
        if "lock" in combined.lower():
            unlock = _run_codegraph_command(
                [binary, "unlock", str(repo_path)],
                cwd=repo_path,
                timeout=remaining(cap=30.0),
                runner=runner,
            )
            audit["commands"].append(unlock)
            index_result = _run_codegraph_command(
                index_cmd, cwd=repo_path, timeout=remaining(), runner=runner
            )
            audit["commands"].append(index_result)
    if index_result["returncode"] != 0:
        audit.update({"status": "fail", "reason": "index_failed"})
        return audit

    affected = _run_codegraph_command(
        [binary, "affected", "--path", str(repo_path), "--stdin", "--json"],
        cwd=repo_path,
        input_text="\n".join(relevant) + "\n",
        timeout=remaining(),
        runner=runner,
    )
    audit["commands"].append(affected)
    if affected["returncode"] != 0:
        audit.update({"status": "fail", "reason": "affected_failed"})
        return audit

    audit.update({"status": "pass", "reason": "affected_computed"})
    return audit


def codegraph_audit_passed(audit: Mapping[str, Any] | None) -> bool:
    if not isinstance(audit, Mapping):
        return False
    return str(audit.get("status") or "").strip().lower() in {"pass", "skipped"}


def codegraph_audit_check(audit: Mapping[str, Any]) -> JsonDict:
    passed = codegraph_audit_passed(audit)
    commands = audit.get("commands") if isinstance(audit.get("commands"), list) else []
    command_text = " && ".join(
        " ".join(str(part) for part in command.get("argv", []))
        for command in commands
        if isinstance(command, dict)
    )
    return {
        "name": "codegraph_audit",
        "status": "pass" if passed else "fail",
        "returncode": 0 if passed else 1,
        "command": command_text,
        "reason": audit.get("reason"),
        "relevant_files": list(audit.get("relevant_files") or []),
    }


def codegraph_audit_manifest_problems(manifest: Mapping[str, Any]) -> list[str]:
    repo = manifest.get("repo")
    if not isinstance(repo, Mapping):
        return []
    relevant = codegraph_relevant_files(repo.get("files_changed") or [])
    if not relevant:
        return []
    audit = manifest.get("codegraph")
    if not isinstance(audit, Mapping):
        return ["repo source/build changes require codegraph audit evidence"]
    problems: list[str] = []
    if str(audit.get("schema") or "").strip() != CODEGRAPH_AUDIT_SCHEMA:
        problems.append("codegraph audit evidence must use schema %s" % CODEGRAPH_AUDIT_SCHEMA)
    if str(audit.get("status") or "").strip().lower() != "pass":
        problems.append("codegraph audit must pass for source/build changes")
    commands = audit.get("commands")
    if not isinstance(commands, list):
        commands = []
    has_index = False
    has_affected = False
    for command in commands:
        if not isinstance(command, Mapping):
            continue
        argv = command.get("argv")
        argv_items = [str(item) for item in argv] if isinstance(argv, list) else []
        if len(argv_items) < 2:
            continue
        try:
            passed = int(command.get("returncode")) == 0
        except (TypeError, ValueError):
            passed = False
        subcommand = argv_items[1]
        if subcommand in {"init", "sync", "index"} and passed:
            has_index = True
        if subcommand == "affected" and passed:
            has_affected = True
    if not has_index:
        problems.append("codegraph audit requires a successful init/sync/index command")
    if not has_affected:
        problems.append("codegraph audit requires a successful affected command")
    audited_files = set(codegraph_relevant_files(audit.get("relevant_files") or []))
    missing = [path for path in relevant if path not in audited_files]
    if missing:
        problems.append("codegraph audit missing changed source/build files: %s" % ", ".join(missing))
    return problems
