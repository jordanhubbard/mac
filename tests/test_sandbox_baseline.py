"""The sandbox has to be able to name the commit the agent started from.

Every code task ran the whole-repo contract gate, no matter how small its
diff. The reason was not the selector: it was that the base SHA never
resolved inside the sandbox, so the impact-scoped path was unreachable.

The sandbox replaces the uploaded `.git` with `git init` plus its own
"MAC OpenShell sandbox baseline" commit, whose SHA is newly generated. The
host's base SHA therefore cannot exist there, `cat-file -e` always missed,
the base was cleared, and run-sanity-tests.sh answered the empty base with
mode=full -- escalating "run the tests this task touched" into "run
everything" on every single task.

These tests exercise the resolver as it actually ships, by extracting it
from the generated sandbox script, so the two cannot drift apart.
"""

import subprocess

from mac import executor_sandbox

_BEGIN = "# --- baseline-resolver (extracted verbatim by tests/test_sandbox_baseline.py) ---"
_END = "# --- end baseline-resolver ---"


def _shipped_resolver():
    """Return the resolver function exactly as the sandbox will run it."""
    script = executor_sandbox._sandbox_repository_verification_shell({})
    assert _BEGIN in script and _END in script, (
        "the sandbox script no longer carries the baseline resolver sentinels; "
        "this test extracts the shipped source and cannot silently pass"
    )
    source = script[script.index(_BEGIN) + len(_BEGIN) : script.index(_END)]
    namespace: dict = {}
    exec(source, namespace)  # noqa: S102 - executing our own shipped source
    return namespace["_resolve_baseline_sha"]


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _sandbox_style_repo(tmp_path):
    """A worktree shaped the way the sandbox shapes one: fresh init, baseline
    commit, then the agent's own work committed on top."""
    repo = tmp_path / "worktree"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "mac-sandbox@invalid")
    _git(repo, "config", "user.name", "MAC OpenShell sandbox")
    (repo / "shipped.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "MAC OpenShell sandbox baseline")
    baseline = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "shipped.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the agent's work")
    return repo, baseline


def test_an_absent_host_base_falls_back_to_the_sandbox_baseline(tmp_path):
    """This is the live case: the host SHA is real but names a commit that
    does not exist in the sandbox. Returning "" here is what escalated every
    task to the whole-repo gate."""
    repo, baseline = _sandbox_style_repo(tmp_path)
    resolve = _shipped_resolver()

    resolved = resolve(subprocess, str(repo), "0" * 40)

    assert resolved == baseline


def test_a_resolvable_host_base_is_preferred(tmp_path):
    """When the host's base does exist -- a preserved .git -- it is the more
    accurate answer and must win over our own baseline commit."""
    repo, baseline = _sandbox_style_repo(tmp_path)
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    resolve = _shipped_resolver()

    assert resolve(subprocess, str(repo), head) == head
    assert head != baseline


def test_a_repo_without_a_sandbox_baseline_resolves_to_nothing(tmp_path):
    """A preserved .git carries no baseline commit of ours. Inventing one
    would diff against an unrelated commit; "" restores the old behaviour,
    which is the whole-repo gate -- slow, but not wrong."""
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "someone@invalid")
    _git(repo, "config", "user.name", "Someone")
    (repo / "f.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ordinary history")
    resolve = _shipped_resolver()

    assert resolve(subprocess, str(repo), "") == ""


def test_a_git_failure_is_not_an_exception(tmp_path):
    """The resolver runs at the head of verification. Raising here would
    replace a reported gate result with an unreported crash."""
    resolve = _shipped_resolver()

    assert resolve(subprocess, str(tmp_path / "does-not-exist"), "") == ""
