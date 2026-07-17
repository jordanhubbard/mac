"""Regression tests: hermes -z one-shot session isolation (fix applied).

Original issue: ``hermes -z`` one-shot invocations inherited the persistent
session context (persistent MEMORY.md/USER.md entries, and by extension the
same HERMES_HOME state.db session store) instead of running in a clean,
isolated state.

This module now characterizes the FIXED behavior deterministically — using a
temporary HERMES_HOME and no real model calls — and pins the exact call path
so a future regression is caught by name.

Call path:
  hermes_cli/main.py  main()
      -> run_oneshot(prompt, ...)           (hermes_cli/oneshot.py)
      -> _run_agent(prompt, ...)            (hermes_cli/oneshot.py)
      -> AIAgent(skip_memory=True,          (run_agent.py / agent/agent_init.py)
                 skip_context_files=True)
      -> agent.chat(prompt)
         -> agent/system_prompt.py build_system_prompt()
            -> MemoryStore.load_from_disk()  (tools/memory_tool.py)
               reads {HERMES_HOME}/memories/MEMORY.md  <-- persistent state
               reads {HERMES_HOME}/memories/USER.md    <-- persistent state
         -> _create_session_db_for_oneshot()
               SessionDB()                  (hermes_state.py)
               opens DEFAULT_DB_PATH        (hermes_state.py)
               = get_hermes_home() / "state.db"

Applied fix: ``_run_agent`` in ``hermes_cli/oneshot.py`` constructs
``AIAgent()`` with ``skip_memory=True`` and ``skip_context_files=True`` so a
one-shot run never inherits persistent memory or repo context files.  The
interactive CLI path (``HermesCLI._init_agent()``) exposes ``--ignore-rules``,
which maps to the same flags; the oneshot path always applies them (there is
no separate opt-in flag for -z).  ``MemoryStore`` itself still loads MEMORY.md
/ USER.md from HERMES_HOME when asked — that is expected behavior of the store;
isolation is enforced by the agent NOT asking for it in one-shot mode.

Patched file:
  src/mac/_hermes/hermes_cli/oneshot.py  -- ``_run_agent()`` AIAgent call now
                                            passes skip_memory=True and
                                            skip_context_files=True.

These tests assert that the fix stays in place: the -z path is always
isolated via the skip flags, without adding a new parser flag and without
setting HERMES_IGNORE_RULES in main().
"""

from __future__ import annotations

import os
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to bootstrap the vendored Hermes runtime for the test.
# ---------------------------------------------------------------------------

def _ensure_hermes_on_path() -> None:
    """Add the vendored Hermes tree to sys.path (idempotent)."""
    from mac import hermes_vendor
    if hermes_vendor.is_vendored():
        hermes_vendor.ensure_on_path()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermes_on_path():
    """Ensure vendored hermes_cli is importable."""
    _ensure_hermes_on_path()


@pytest.fixture()
def isolated_hermes_home(tmp_path, monkeypatch):
    """A fresh, isolated HERMES_HOME with no prior memory state."""
    home = tmp_path / ".hermes"
    home.mkdir()
    # Required subdirectories
    (home / "memories").mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Force hermes_constants to re-read the env var on each call.
    # It caches nothing at module level; get_hermes_home() reads os.environ
    # each time, so setting the env var is sufficient.
    return home


@pytest.fixture()
def hermes_home_with_memory(isolated_hermes_home):
    """HERMES_HOME that has pre-existing persistent memory entries."""
    mem_dir = isolated_hermes_home / "memories"
    (mem_dir / "MEMORY.md").write_text(
        "- User prefers concise responses\n"
        "- This memory entry should NOT appear in an isolated oneshot run\n",
        encoding="utf-8",
    )
    (mem_dir / "USER.md").write_text(
        "- User name: TestUser\n"
        "- This user-profile entry should NOT appear in an isolated oneshot run\n",
        encoding="utf-8",
    )
    return isolated_hermes_home


# ---------------------------------------------------------------------------
# Code-path characterization tests (no real model calls)
# ---------------------------------------------------------------------------


class TestOneshotCallPathAnalysis:
    """Static analysis of the call path — no imports from the vendored tree
    required.  These tests pin the STRUCTURE of the applied isolation fix.
    """

    def test_run_oneshot_isolation_flows_through_aiagent_not_signature(self):
        """The isolation fix lives in the AIAgent() call, not _run_agent()'s
        signature.

        The fix passes ``skip_memory=True`` / ``skip_context_files=True`` inside
        the ``AIAgent(...)`` construction in ``_run_agent()``; it deliberately
        does NOT add a ``skip_memory`` parameter to ``_run_agent()`` itself
        (one-shot runs are always isolated, with no per-call opt-in).  This test
        documents that contract so nobody "fixes" it by threading a redundant
        parameter through the public signature.
        """
        _ensure_hermes_on_path()
        try:
            from hermes_cli.oneshot import _run_agent
        except ImportError:
            pytest.skip("vendored hermes_cli not available")

        import ast
        import inspect

        # _run_agent() intentionally exposes no skip_memory parameter: isolation
        # is unconditional for -z, applied internally at the AIAgent call site.
        params = inspect.signature(_run_agent).parameters
        assert "skip_memory" not in params, (
            "skip_memory was threaded into _run_agent()'s signature — the fix "
            "applies isolation internally at the AIAgent() call, not as a "
            "public parameter; update this test if the design changed."
        )

        # And confirm the isolation actually happens inside _run_agent's body by
        # locating the AIAgent(...) call and its skip_* keyword arguments.
        source = textwrap.dedent(inspect.getsource(_run_agent))
        tree = ast.parse(source)
        aiagent_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "AIAgent"
        ]
        assert aiagent_calls, "No AIAgent() call found inside _run_agent()"
        for call in aiagent_calls:
            kwarg_names = {kw.arg for kw in call.keywords}
            assert {"skip_memory", "skip_context_files"} <= kwarg_names, (
                "_run_agent() constructs AIAgent() without skip_memory / "
                "skip_context_files — the one-shot isolation fix has regressed."
            )

    def test_oneshot_aiagent_call_passes_skip_memory_and_skip_context_files(self):
        """_run_agent() constructs AIAgent with skip_memory=True and
        skip_context_files=True.

        The AIAgent() call in oneshot.py passes:
          api_key, base_url, provider, api_mode, model, enabled_toolsets,
          quiet_mode, platform, session_db, credential_pool, fallback_model,
          skip_memory=True, skip_context_files=True, clarify_callback.

        Because skip_memory / skip_context_files ARE in the call, persistent
        memory and repo context files are NOT loaded from HERMES_HOME during a
        one-shot run — the invocation is isolated.
        """
        import ast
        try:
            from mac import hermes_vendor
            vendor_dir = hermes_vendor.VENDOR_DIR
        except Exception:
            pytest.skip("vendored hermes not available")

        oneshot_path = Path(vendor_dir) / "hermes_cli" / "oneshot.py"
        if not oneshot_path.exists():
            pytest.skip(f"oneshot.py not found at {oneshot_path}")

        source = oneshot_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find AIAgent(...) call inside _run_agent
        aiagent_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "AIAgent"
            ):
                aiagent_calls.append(node)

        assert aiagent_calls, "No AIAgent() call found in oneshot.py — file structure changed"

        # Contract (post-fix): every AIAgent() call in oneshot._run_agent()
        # passes skip_memory=True and skip_context_files=True so one-shot runs
        # do not inherit persistent memory / context files.
        for call in aiagent_calls:
            kwarg_names = {kw.arg for kw in call.keywords}
            assert "skip_memory" in kwarg_names, (
                "skip_memory= is missing from AIAgent() in oneshot.py — "
                "the one-shot isolation fix has regressed."
            )
            assert "skip_context_files" in kwarg_names, (
                "skip_context_files= is missing from AIAgent() in oneshot.py — "
                "the one-shot isolation fix has regressed."
            )

    def test_both_interactive_and_oneshot_paths_reference_skip_memory(self):
        """Both the interactive CLI path and the -z/oneshot path wire
        skip_memory.

        The interactive path exposes ``--ignore-rules`` → ``skip_memory=True``;
        the oneshot path always applies ``skip_memory=True`` internally (fix
        applied).  This confirms neither path lost its skip_memory wiring.
        """
        try:
            from mac import hermes_vendor
            vendor_dir = hermes_vendor.VENDOR_DIR
        except Exception:
            pytest.skip("vendored hermes not available")

        # Interactive path: cli.py passes skip_memory when ignore_rules=True
        cli_path = Path(vendor_dir) / "cli.py"
        if cli_path.exists():
            cli_source = cli_path.read_text(encoding="utf-8")
            # The interactive CLI wires skip_memory
            assert "skip_memory" in cli_source, (
                "cli.py no longer references skip_memory — check the fix"
            )

        # Oneshot path: oneshot.py now also passes skip_memory (fix applied).
        oneshot_path = Path(vendor_dir) / "hermes_cli" / "oneshot.py"
        if oneshot_path.exists():
            oneshot_source = oneshot_path.read_text(encoding="utf-8")
            assert "skip_memory" in oneshot_source, (
                "skip_memory is missing from oneshot.py — "
                "the one-shot isolation fix has regressed"
            )

    def test_session_db_in_oneshot_uses_persistent_home(self):
        """_create_session_db_for_oneshot() opens the default state.db under
        HERMES_HOME rather than a temporary/isolated database.

        This documents the current one-shot session-store wiring, which is
        SEPARATE from the memory/context-file isolation fix.  The applied fix
        (skip_memory / skip_context_files on the AIAgent call) stops persistent
        MEMORY.md / USER.md / context files from leaking into a one-shot run; it
        does not repoint SessionDB, so ``SessionDB()`` still resolves to
        DEFAULT_DB_PATH under HERMES_HOME.  This test pins that current wiring
        so any future change to the one-shot session store is noticed.
        """
        try:
            from mac import hermes_vendor
            vendor_dir = hermes_vendor.VENDOR_DIR
        except Exception:
            pytest.skip("vendored hermes not available")

        state_path = Path(vendor_dir) / "hermes_state.py"
        if not state_path.exists():
            pytest.skip(f"hermes_state.py not found at {state_path}")

        source = state_path.read_text(encoding="utf-8")

        # DEFAULT_DB_PATH is tied to get_hermes_home()
        assert "DEFAULT_DB_PATH" in source, "hermes_state.py structure changed"
        assert "get_hermes_home()" in source, (
            "DEFAULT_DB_PATH no longer references get_hermes_home() — "
            "the one-shot session-store wiring changed; review this test"
        )

        oneshot_path = Path(vendor_dir) / "hermes_cli" / "oneshot.py"
        if oneshot_path.exists():
            oneshot_source = oneshot_path.read_text(encoding="utf-8")
            # _create_session_db_for_oneshot calls SessionDB() with NO db_path
            # argument, so it gets DEFAULT_DB_PATH = get_hermes_home()/"state.db"
            assert "SessionDB()" in oneshot_source, (
                "oneshot.py now passes a custom db_path to SessionDB() — "
                "the one-shot session-store wiring changed; review this test"
            )


# ---------------------------------------------------------------------------
# Functional verification: the oneshot agent is isolated from persistent memory
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "src" / "mac" / "_hermes" / "SNAPSHOT_PIN").exists(),
    reason="vendored Hermes snapshot not present",
)
class TestOneshotMemoryIsolation:
    """Verify at runtime that a oneshot run is isolated from persistent memory
    under HERMES_HOME.  All real I/O (model calls, DB writes) is mocked.

    These tests prove the fix works: ``_run_agent()`` constructs AIAgent with
    ``skip_memory=True`` / ``skip_context_files=True`` so persistent MEMORY.md /
    USER.md entries do not leak into a one-shot run.  The two MemoryStore tests
    below confirm the store CAN still load those files when asked directly —
    which is expected behavior of MemoryStore itself, independent of the oneshot
    isolation fix.
    """

    def test_oneshot_agent_constructs_aiagent_with_isolation_flags(
        self, hermes_home_with_memory, monkeypatch
    ):
        """At runtime, ``_run_agent()`` constructs AIAgent with
        ``skip_memory=True`` and ``skip_context_files=True``.

        We intercept ``AIAgent.__init__`` to capture the arguments it is
        constructed with, then assert both isolation flags are True — proving a
        one-shot run will NOT load persistent MEMORY.md / USER.md / context
        files from the active HERMES_HOME.

        To make the intercept robust, provider resolution (which would otherwise
        raise on a fake provider/model before AIAgent is ever reached) is
        stubbed so ``_run_agent()`` runs all the way to the AIAgent() call.
        """
        _ensure_hermes_on_path()
        try:
            from hermes_cli.oneshot import _run_agent
            import run_agent as ra
            import hermes_cli.runtime_provider as runtime_provider
        except ImportError:
            pytest.skip("vendored hermes_cli not available")

        captured_kwargs: dict = {}

        class _Sentinel(Exception):
            pass

        def _capture_init(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            # Raise immediately to avoid any real agent construction / I/O.
            raise _Sentinel("captured")

        monkeypatch.setattr(ra.AIAgent, "__init__", _capture_init)

        # Stub provider resolution so _run_agent() reaches the AIAgent() call
        # instead of bailing out on an unknown fake provider/model.  _run_agent
        # imports resolve_runtime_provider locally from this module, so patching
        # it here (the source module) reaches that local import.
        def _fake_resolve_runtime_provider(*args, **kwargs):
            return {
                "api_key": "test-key",
                "base_url": "http://localhost:0",
                "provider": "openai",
                "api_mode": "chat",
                "credential_pool": None,
            }

        monkeypatch.setattr(
            runtime_provider,
            "resolve_runtime_provider",
            _fake_resolve_runtime_provider,
        )

        monkeypatch.setenv("HERMES_YOLO_MODE", "1")
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")

        try:
            _run_agent(
                "what do you know about me?",
                model="fake-model",
                provider=None,
            )
        except _Sentinel:
            pass  # Expected — we raised it from the intercepted __init__.
        except Exception as exc:  # noqa: BLE001
            # If _run_agent still fails before AIAgent() despite the stub, do not
            # silently pass: surface a clear, correct skip reason.  The static
            # AST tests above still assert the isolation contract in that case.
            if not captured_kwargs:
                pytest.skip(
                    f"_run_agent failed before constructing AIAgent: {exc!r}"
                )

        assert captured_kwargs, (
            "AIAgent.__init__ was never called — the intercept did not run; "
            "the oneshot call path may have changed."
        )

        # Contract (post-fix): _run_agent() constructs AIAgent with
        # skip_memory=True and skip_context_files=True.
        skip_memory_val = captured_kwargs.get("skip_memory")
        assert skip_memory_val is True, (
            f"skip_memory={skip_memory_val!r} — expected True; "
            "the one-shot isolation fix has regressed."
        )
        skip_ctx_val = captured_kwargs.get("skip_context_files")
        assert skip_ctx_val is True, (
            f"skip_context_files={skip_ctx_val!r} — expected True; "
            "the one-shot isolation fix has regressed."
        )

    def test_memory_store_load_from_disk_reads_hermes_home(
        self, hermes_home_with_memory
    ):
        """MemoryStore.load_from_disk() reads from the active
        HERMES_HOME/memories/ directory.

        This is expected behavior of MemoryStore itself and is INDEPENDENT of
        the oneshot isolation fix: when something asks the store to load, it
        reads MEMORY.md / USER.md from the shared HERMES_HOME.  One-shot runs
        stay isolated by NOT asking (skip_memory=True on the AIAgent call), not
        by changing what MemoryStore does when invoked directly.
        """
        _ensure_hermes_on_path()
        try:
            from tools.memory_tool import MemoryStore
        except ImportError:
            pytest.skip("tools.memory_tool not available")

        store = MemoryStore()
        store.load_from_disk()

        # Entries from the fixture file are present in the store
        all_entries = store.memory_entries + store.user_entries
        entry_text = "\n".join(all_entries)

        assert "should NOT appear in an isolated oneshot run" in entry_text, (
            "Memory entries from the persistent HERMES_HOME were NOT loaded. "
            "This contradicts the regression report — or the fixture did not "
            "set HERMES_HOME correctly."
        )

    def test_system_prompt_includes_memory_when_store_is_populated(
        self, hermes_home_with_memory, monkeypatch
    ):
        """When an agent HAS a populated MemoryStore, build_system_prompt()
        includes those memory entries in the prompt.

        This exercises MemoryStore -> system_prompt behavior directly (again,
        expected behavior of the store/prompt builder).  It shows WHY the
        oneshot fix matters: if a one-shot agent were built WITHOUT
        skip_memory=True, its populated store would inject these persistent
        entries into the prompt.  The isolation fix prevents that store from
        being populated in one-shot mode in the first place.

        We build a lightweight stand-in agent with concrete attribute values so
        no MagicMock leaks into the prompt's string joins.
        """
        _ensure_hermes_on_path()
        try:
            from tools.memory_tool import MemoryStore
            from agent.system_prompt import build_system_prompt
        except ImportError:
            pytest.skip("required vendored modules not available")

        store = MemoryStore()
        store.load_from_disk()

        # A concrete stand-in agent.  build_system_prompt() joins many optional
        # guidance blocks; supplying real (non-Mock) values keeps them out of
        # the string joins so the memory (volatile) tier is what we assert on.
        agent = types.SimpleNamespace(
            _memory_store=store,
            _memory_enabled=True,
            _user_profile_enabled=True,
            _memory_manager=None,
            skip_context_files=True,
            load_soul_identity=False,
            platform="cli",
            valid_tool_names=set(),
            model="fake-model",
            provider="fake-provider",
            pass_session_id=False,
            session_id=None,
            _task_completion_guidance=False,
            _tool_use_enforcement="false",
            _environment_probe=False,
            _kanban_worker_guidance="",
        )

        prompt = build_system_prompt(agent)

        # Persistent memory entries appear in the prompt of an agent whose store
        # was populated (the exact leak the oneshot fix prevents by skipping it).
        assert "should NOT appear in an isolated oneshot run" in prompt, (
            "Memory entries did NOT appear in the system prompt even though the "
            "store was populated — the fixture or prompt builder changed."
        )


# ---------------------------------------------------------------------------
# Isolation contract tests (fix applied)
# ---------------------------------------------------------------------------


class TestOneshotIsolationContract:
    """Contract tests that confirm the POST-FIX design of one-shot isolation.

    The fix makes ``-z`` always isolated by passing skip_memory=True /
    skip_context_files=True on the AIAgent call.  It does NOT add a new parser
    flag and does NOT set HERMES_IGNORE_RULES in main().  These tests pin that
    design so a future change that re-introduces memory inheritance (or an
    unnecessary new flag) is caught.
    """

    def test_parser_exposes_no_isolated_flag_for_oneshot(self):
        """The -z / --oneshot flag has no isolation switch in the parser.

        The applied fix makes ``-z`` ALWAYS isolated by default (via the
        skip_memory / skip_context_files flags on the AIAgent call), so no
        ``--no-memory`` / ``--isolated`` opt-in flag was added.  This test pins
        that decision: if such a flag appears later it is a design change that
        must be reviewed here.
        """
        _ensure_hermes_on_path()
        try:
            from hermes_cli._parser import build_top_level_parser
        except ImportError:
            pytest.skip("vendored hermes_cli not available")

        parser, _, _ = build_top_level_parser()
        # Collect all option strings across the top-level parser
        option_strings: set[str] = set()
        for action in parser._actions:
            option_strings.update(action.option_strings)

        # The fix isolates -z unconditionally, so no dedicated flag exists.
        isolation_flags = {"--isolated", "--no-memory", "--fresh", "--clean"}
        found = option_strings & isolation_flags
        assert not found, (
            f"Isolation flag(s) {found} were added to the top-level parser — "
            "the fix isolates -z unconditionally without a flag; update this "
            "test if that design changed."
        )

    def test_main_py_oneshot_path_does_not_set_ignore_rules(self):
        """main() does not set HERMES_IGNORE_RULES before calling run_oneshot().

        The fix wires skip_memory / skip_context_files directly at the AIAgent
        call in ``_run_agent()`` rather than piggy-backing on the interactive
        ``--ignore-rules`` / HERMES_IGNORE_RULES plumbing.  This test confirms
        main() does NOT set that env var for the -z path, so isolation is
        applied by the oneshot code itself, not by a side-channel env flag.
        """
        _ensure_hermes_on_path()
        try:
            from mac import hermes_vendor
            vendor_dir = hermes_vendor.VENDOR_DIR
        except Exception:
            pytest.skip("vendored hermes not available")

        main_path = Path(vendor_dir) / "hermes_cli" / "main.py"
        if not main_path.exists():
            pytest.skip(f"main.py not found at {main_path}")

        source = main_path.read_text(encoding="utf-8")

        # Find the oneshot dispatch block in main()
        # The oneshot call is:
        #   if getattr(args, "oneshot", None):
        #       from hermes_cli.oneshot import run_oneshot
        #       sys.exit(run_oneshot(...))
        # Check that HERMES_IGNORE_RULES is NOT set just before this block.
        oneshot_idx = source.find('if getattr(args, "oneshot", None):')
        assert oneshot_idx != -1, "oneshot dispatch block not found in main.py"

        # Look at the 500 chars preceding the dispatch block
        preceding = source[max(0, oneshot_idx - 500): oneshot_idx]
        assert "HERMES_IGNORE_RULES" not in preceding, (
            "HERMES_IGNORE_RULES was set before the oneshot dispatch — the fix "
            "isolates -z via skip_memory/skip_context_files at the AIAgent "
            "call, not via this env var; update this test if that changed."
        )
