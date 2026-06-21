from __future__ import annotations

from pathlib import Path
import subprocess

from mac.codegraph_audit import codegraph_relevant_files, run_codegraph_audit


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
                '    echo \'{"affected":[]}\'',
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
        ["README.md", "src/app.py", "package.json", "docs/diagram.png", ".github/workflows/ci.yml", "BUILD"]
    ) == ["src/app.py", "package.json", ".github/workflows/ci.yml", "BUILD"]


def test_codegraph_audit_skips_docs_only_changes(tmp_path):
    audit = run_codegraph_audit(tmp_path, ["README.md", "docs/guide.txt"])

    assert audit["status"] == "skipped"
    assert audit["reason"] == "non_code_change"
    assert audit["commands"] == []


def test_codegraph_audit_runs_init_and_affected_for_source_changes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git" / "info").mkdir(parents=True)
    log = _fake_codegraph(tmp_path, monkeypatch)

    audit = run_codegraph_audit(repo, ["src/app.py"])

    assert audit["status"] == "pass"
    assert audit["relevant_files"] == ["src/app.py"]
    assert [command["argv"][1] for command in audit["commands"]] == ["init", "affected"]
    assert ".codegraph/" in (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert "affected --path" in log.read_text(encoding="utf-8")


def test_codegraph_audit_excludes_generated_state_in_linked_worktree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "tests"], check=True)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", str(linked), "-b", "task"], check=True, capture_output=True)
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
    assert "init" in log.read_text(encoding="utf-8")
