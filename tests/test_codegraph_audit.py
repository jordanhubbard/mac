from __future__ import annotations

from pathlib import Path
import subprocess

from mac.codegraph_audit import codegraph_relevant_files, run_codegraph_audit

# imports relocated from test_codegraph_audit_edges.py
from types import SimpleNamespace
from typing import Any
import pytest
from mac import codegraph_audit as audit


def _fake_codegraph(tmp_path: Path, monkeypatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "codegraph"
    log = tmp_path / "codegraph.log"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                f'echo "$@" >> {log}',
                'case "${1:-}" in',
                "  init)",
                '    mkdir -p "$2/.codegraph"',
                "    ;;",
                "  sync)",
                "    ;;",
                "  affected)",
                "    cat >/dev/null",
                "    echo '{\"affected\":[]}'",
                "    ;;",
                "  unlock)",
                "    ;;",
                "  *) exit 2 ;;",
                "esac",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("MAC_CODEGRAPH_BIN", str(script))
    return log


def test_codegraph_relevant_files_excludes_docs_and_media():
    assert codegraph_relevant_files(
        [
            "README.md",
            "src/app.py",
            "package.json",
            "docs/diagram.png",
            ".github/workflows/ci.yml",
            "BUILD",
        ]
    ) == ["src/app.py", "package.json", ".github/workflows/ci.yml", "BUILD"]


def test_codegraph_audit_skips_docs_only_changes(tmp_path):
    audit = run_codegraph_audit(tmp_path, ["README.md", "docs/guide.txt"])

    assert audit["status"] == "skipped"
    assert audit["reason"] == "non_code_change"
    assert audit["commands"] == []


def test_codegraph_audit_runs_init_and_affected_for_source_changes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".codegraph").mkdir()
    (repo / ".git" / "info").mkdir(parents=True)
    log = _fake_codegraph(tmp_path, monkeypatch)

    audit = run_codegraph_audit(repo, ["src/app.py"])

    assert audit["status"] == "pass"
    assert audit["relevant_files"] == ["src/app.py"]
    assert [command["argv"][1] for command in audit["commands"]] == ["sync", "affected"]
    assert ".codegraph/" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "affected --path" in log.read_text(encoding="utf-8")


def test_codegraph_audit_excludes_generated_state_in_linked_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tests"], check=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(linked), "-b", "task"],
        check=True,
        capture_output=True,
    )
    (linked / ".codegraph").mkdir()
    log = _fake_codegraph(tmp_path, monkeypatch)

    audit = run_codegraph_audit(linked, ["src/app.py"])

    assert audit["status"] == "pass"
    status = subprocess.run(
        ["git", "-C", str(linked), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".codegraph" not in status
    assert "sync" in log.read_text(encoding="utf-8")


# --- relocated from test_codegraph_audit_edges.py (coverage companion folded in) ---


def test_path_normalization_relevance_and_required() -> None:
    assert audit.normalize_repo_path(" ././src\\app.py/ ") == "src/app.py"
    assert not audit.is_codegraph_relevant_path("")
    assert audit.is_codegraph_relevant_path("project.csproj")
    assert audit.codegraph_audit_required(["Makefile"])
    assert not audit.codegraph_audit_required(["README.md"])


def test_resolve_binary_and_git_exclude_fallbacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    monkeypatch.setenv("MAC_CODEGRAPH_BIN", str(missing))
    monkeypatch.setattr(audit.shutil, "which", lambda _name: None)
    monkeypatch.setattr(audit.Path, "home", lambda: tmp_path / "home")
    original_exists = audit.Path.exists
    monkeypatch.setattr(audit.Path, "exists", lambda _path: False)
    assert audit._resolve_codegraph_binary() == ""
    monkeypatch.setattr(audit.Path, "exists", original_exists)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        audit.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("git missing")),
    )
    assert audit._git_exclude_path(repo) == repo / ".git" / "info" / "exclude"
    assert audit._git_exclude_path(tmp_path / "plain") is None


def test_ensure_exclude_handles_absent_and_existing_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(audit, "_git_exclude_path", lambda _repo: None)
    audit.ensure_codegraph_git_exclude(tmp_path)
    exclude = tmp_path / "exclude"
    exclude.write_text("local-only", encoding="utf-8")
    monkeypatch.setattr(audit, "_git_exclude_path", lambda _repo: exclude)
    audit.ensure_codegraph_git_exclude(tmp_path)
    audit.ensure_codegraph_git_exclude(tmp_path)
    assert exclude.read_text(encoding="utf-8") == "local-only\n.codegraph/\n"


def test_truncate_and_command_error_records(tmp_path: Path) -> None:
    assert audit._truncate("abcdef", 3) == "def"

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], 1, output="partial", stderr=None)

    timed = audit._run_codegraph_command(
        ["/bin/codegraph", "sync"], cwd=tmp_path, timeout=1, runner=timeout
    )
    assert timed["returncode"] == 124
    assert timed["stdout"] == "partial"
    assert "timed out" in timed["stderr"]

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise OSError("not executable")

    failed = audit._run_codegraph_command(
        ["/bin/codegraph", "sync"], cwd=tmp_path, timeout=1, runner=missing
    )
    assert failed["returncode"] == 1
    assert failed["argv"] == ["codegraph", "sync"]


def test_run_audit_unavailable_and_exclude_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(audit, "_resolve_codegraph_binary", lambda: "")
    unavailable = audit.run_codegraph_audit(tmp_path, ["src/app.py"])
    assert unavailable["reason"] == "hint_unavailable"
    (tmp_path / ".codegraph").mkdir()
    binary = tmp_path / "codegraph"
    binary.touch()
    monkeypatch.setattr(audit, "_resolve_codegraph_binary", lambda: str(binary))
    monkeypatch.setattr(
        audit,
        "ensure_codegraph_git_exclude",
        lambda _repo: (_ for _ in ()).throw(OSError("read-only")),
    )
    calls: list[str] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv[1])
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    result = audit.run_codegraph_audit(tmp_path, ["src/app.py"], runner=runner)
    assert result["status"] == "pass"
    assert "read-only" in result["warnings"][0]
    assert calls == ["sync", "affected"]


def test_run_audit_unlock_retry_and_terminal_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "codegraph"
    binary.touch()
    (tmp_path / ".codegraph").mkdir()
    monkeypatch.setattr(audit, "_resolve_codegraph_binary", lambda: str(binary))
    monkeypatch.setattr(audit, "ensure_codegraph_git_exclude", lambda _repo: None)
    replies = iter(
        [
            SimpleNamespace(returncode=1, stdout="index LOCK held", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=1, stdout="", stderr="affected broke"),
        ]
    )
    result = audit.run_codegraph_audit(
        tmp_path, ["src/app.py"], runner=lambda *args, **kwargs: next(replies)
    )
    assert [item["argv"][1] for item in result["commands"]] == [
        "sync",
        "unlock",
        "sync",
        "affected",
    ]
    assert result["reason"] == "hint_untrusted"
    result = audit.run_codegraph_audit(
        tmp_path,
        ["src/app.py"],
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=2, stdout="ordinary error", stderr=""
        ),
    )
    assert result["reason"] == "hint_untrusted"


def test_pass_check_and_manifest_problem_variants() -> None:
    assert not audit.codegraph_audit_passed(None)
    assert audit.codegraph_audit_passed({"status": " SKIPPED "})
    check = audit.codegraph_audit_check(
        {
            "status": "fail",
            "reason": "bad",
            "commands": [{"argv": ["codegraph", "sync"]}, "ignore"],
            "relevant_files": ["src/a.py"],
        }
    )
    assert check["returncode"] == 1
    assert check["command"] == "codegraph sync"
    assert audit.codegraph_audit_manifest_problems({}) == []
    assert audit.codegraph_audit_manifest_problems({"repo": {"files_changed": ["README.md"]}}) == []
    assert audit.codegraph_audit_manifest_problems({"repo": {"files_changed": ["src/a.py"]}}) == []
    assert (
        audit.codegraph_audit_manifest_problems(
            {"repo": {"files_changed": ["src/a.py"]}, "codegraph": {"status": "fail"}}
        )
        == []
    )
    problems = audit.codegraph_audit_diagnostics(
        {
            "repo": {"files_changed": ["src/a.py", "src/b.py"]},
            "codegraph": {
                "schema": "wrong",
                "status": "fail",
                "relevant_files": ["src/a.py"],
                "commands": [
                    None,
                    {"argv": ["codegraph"]},
                    {"argv": ["codegraph", "sync"], "returncode": "bad"},
                    {"argv": ["codegraph", "affected"], "returncode": 0},
                ],
            },
        }
    )
    assert any(("schema" in item for item in problems))
    assert any(("must pass" in item for item in problems))
    assert any(("successful init" in item for item in problems))
    assert any(("src/b.py" in item for item in problems))
