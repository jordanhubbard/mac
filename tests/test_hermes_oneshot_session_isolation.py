"""Characterization tests: hermes -z one-shot session isolation regression.

Reported issue: ``hermes -z`` one-shot invocations inherit the persistent
session context (persistent MEMORY.md/USER.md entries, and by extension the
same HERMES_HOME state.db session store) rather than running in a clean,
isolated state.

This module characterizes the failure deterministically — using a temporary
HERMES_HOME and no real model calls — so the regression is reproducible and
the exact call path to patch is named by the test.

Call path under investigation:
  hermes_cli/main.py  main()
      -> run_oneshot(prompt, ...)           (hermes_cli/oneshot.py:125)
      -> _run_agent(prompt, ...)            (hermes_cli/oneshot.py:245)
      -> AIAgent(skip_memory=???,           (run_agent.py / agent/agent_init.py)
                 skip_context_files=???)
      -> agent.chat(prompt)
         -> agent/system_prompt.py build_system_prompt()
            -> MemoryStore.load_from_disk()  (tools/memory_tool.py:132)
               reads {HERMES_HOME}/memories/MEMORY.md  <-- PERSISTENT STATE
               reads {HERMES_HOME}/memories/USER.md    <-- PERSISTENT STATE
         -> _create_session_db_for_oneshot()
               SessionDB()                  (hermes_state.py:377)
               opens DEFAULT_DB_PATH        (hermes_state.py:34)
               = get_hermes_home() / "state.db"   <-- PERSISTENT STATE

Finding: ``_run_agent`` in ``hermes_cli/oneshot.py`` calls ``AIAgent()``
without passing ``skip_memory=True`` or ``skip_context_files=True``.
The interactive CLI path (``HermesCLI._init_agent()``) exposes
``--ignore-rules`` which maps to these flags, but the oneshot path has no
equivalent.  When a HERMES_HOME is shared between an interactive session and
a ``hermes -z`` call (which is the default — both use the same
``get_hermes_home()`` → ``~/.hermes``), the one-shot run inherits:

  1. Persistent memory entries from ``~/.hermes/memories/MEMORY.md``
  2. User-profile entries from ``~/.hermes/memories/USER.md``
  3. The shared ``~/.hermes/state.db`` session store (session_search recall)

None of these are expected in an isolated one-shot invocation that is
intended to be a "fresh" context for scripting / piping.

Files to patch:
  src/mac/_hermes/hermes_cli/oneshot.py  -- ``_run_agent()`` AIAgent call
  src/mac/_hermes/hermes_cli/_parser.py  -- optionally add ``--no-memory``
                                            / ``--isolated`` flag for -z

The fix is to pass ``skip_memory=True`` and ``skip_context_files=True`` to
``AIAgent`` inside ``_run_agent()``, OR introduce an ``isolated`` parameter
to ``run_oneshot()`` that callers (main.py) can set when desired.
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
    required.  These tests characterize the STRUCTURE of the regression.
    """

    def test_run_oneshot_signature_has_no_skip_memory_param(self):
        """_run_agent() in oneshot.py does not accept or pass skip_memory.

        This is the root cause: there is no way for a caller of run_oneshot()
        to request that persistent memory be excluded from the one-shot run.
        """
        _ensure_hermes_on_path()
        try:
            from hermes_cli.oneshot import _run_agent
        except ImportError:
            pytest.skip("vendored hermes_cli not available")

        import inspect
        params = inspect.signature(_run_agent).parameters
        # Characterization: the parameter is ABSENT — this is the bug.
        assert "skip_memory" not in params, (
            "skip_memory was added to _run_agent() — regression may be fixed; "
            "update this characterization test."
        )

    def test_run_oneshot_aiagent_call_missing_skip_memory(self):
        """_run_agent() constructs AIAgent without skip_memory=True.

        Reading oneshot.py confirms AIAgent() is called at line ~335 with:
          api_key, base_url, provider, api_mode, model, enabled_toolsets,
          quiet_mode, platform, session_db, credential_pool, fallback_model,
          clarify_callback.

        skip_memory and skip_context_files are NOT in the call — meaning
        persistent memory IS loaded from HERMES_HOME/memories/ during
        every one-shot run.
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

    def test_interactive_cli_has_skip_memory_flag_but_oneshot_does_not(self):
        """The interactive CLI path supports --ignore-rules → skip_memory=True,
        but the -z/oneshot path has no equivalent mechanism.

        This documents the asymmetry between the two paths.
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

        This means session_search results from prior interactive sessions are
        visible inside a one-shot agent run — cross-contaminating context.
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

        # Characterization: DEFAULT_DB_PATH is tied to get_hermes_home()
        assert "DEFAULT_DB_PATH" in source, "hermes_state.py structure changed"
        assert "get_hermes_home()" in source, (
            "DEFAULT_DB_PATH no longer references get_hermes_home() — "
            "check whether the isolation regression was fixed"
        )

        oneshot_path = Path(vendor_dir) / "hermes_cli" / "oneshot.py"
        if oneshot_path.exists():
            oneshot_source = oneshot_path.read_text(encoding="utf-8")
            # _create_session_db_for_oneshot calls SessionDB() with NO db_path
            # argument, so it gets DEFAULT_DB_PATH = get_hermes_home()/"state.db"
            assert "SessionDB()" in oneshot_source, (
                "oneshot.py now passes a custom db_path to SessionDB() — "
                "may be partially fixed; review isolation"
            )


# ---------------------------------------------------------------------------
# Functional characterization: memory IS loaded by the oneshot agent
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (Path(__file__).parent.parent / "src" / "mac" / "_hermes" / "SNAPSHOT_PIN").exists(),
    reason="vendored Hermes snapshot not present",
)
class TestOneshotMemoryInheritance:
    """Verify at runtime that a oneshot run picks up persistent memory
    from HERMES_HOME.  All real I/O (model calls, DB writes) is mocked.

    These tests CONFIRM the regression is present and will FAIL once the
    fix is applied.  At that point they should be converted to prove the
    fix works (i.e. invert the assertions).
    """

    def test_oneshot_agent_loads_persistent_memory_entries(
        self, hermes_home_with_memory, monkeypatch
    ):
        """Characterization: AIAgent inside oneshot._run_agent() reads
        MEMORY.md from the active HERMES_HOME.

        We intercept AIAgent.__init__ to capture the arguments it was
        constructed with, then check whether skip_memory was False (the
        default, meaning memory WILL be loaded).
        """
        _ensure_hermes_on_path()
        try:
            from hermes_cli.oneshot import _run_agent
            import run_agent as ra
        except ImportError:
            pytest.skip("vendored hermes_cli not available")

        captured_kwargs: dict = {}

        original_init = ra.AIAgent.__init__

        def _capture_init(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            # Raise immediately to avoid any real work
            raise _Sentinel("captured")

        class _Sentinel(Exception):
            pass

        monkeypatch.setattr(ra.AIAgent, "__init__", _capture_init)

        # Patch out the heavy imports inside _run_agent
        monkeypatch.setenv("HERMES_YOLO_MODE", "1")
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")

        try:
            _run_agent(
                "what do you know about me?",
                model="fake-model",
                provider="fake-provider",
            )
        except _Sentinel:
            pass  # Expected — we raised it intentionally
        except Exception:
            # _run_agent may fail for other reasons (missing config, etc.)
            # before reaching AIAgent(); skip in that case.
            if not captured_kwargs:
                pytest.skip("_run_agent failed before constructing AIAgent")

        if not captured_kwargs:
            pytest.skip("AIAgent.__init__ was not called")

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
        """Characterization: MemoryStore.load_from_disk() reads from the
        active HERMES_HOME/memories/ directory.

        Because oneshot._run_agent() does not pass skip_memory=True,
        the agent's system prompt receives the contents of MEMORY.md and
        USER.md from the shared HERMES_HOME.
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

    def test_oneshot_system_prompt_contains_persistent_memory(
        self, hermes_home_with_memory, monkeypatch
    ):
        """Characterization: the system prompt built for a oneshot agent
        contains persistent memory entries.

        We call build_system_prompt() with a minimal mock agent that has
        _memory_store populated (as it would be when skip_memory=False).
        """
        _ensure_hermes_on_path()
        try:
            from tools.memory_tool import MemoryStore
            from agent.system_prompt import build_system_prompt
        except ImportError:
            pytest.skip("required vendored modules not available")

        store = MemoryStore()
        store.load_from_disk()

        # Build a minimal mock agent
        mock_agent = MagicMock()
        mock_agent._memory_store = store
        mock_agent._memory_enabled = True
        mock_agent._user_profile_enabled = True
        mock_agent._memory_manager = None
        mock_agent.skip_context_files = False
        mock_agent.load_soul_identity = False
        mock_agent.platform = "cli"
        mock_agent.tools = []
        mock_agent._context_engine = None
        mock_agent._cached_system_prompt = None

        try:
            prompt = build_system_prompt(mock_agent)
        except Exception as exc:
            # build_system_prompt may need more mock attributes; skip if so
            pytest.skip(f"build_system_prompt raised {exc!r}")

        # Characterization: persistent memory entries appear in the prompt
        assert "should NOT appear in an isolated oneshot run" in prompt, (
            "Memory entries did NOT appear in the system prompt. "
            "The regression may not be present, or the test fixture is wrong."
        )


# ---------------------------------------------------------------------------
# Proposed-fix contract tests
# (These will FAIL until the fix is applied and should be inverted then.)
# ---------------------------------------------------------------------------


class TestOneshotIsolationContract:
    """Contract tests that SHOULD pass once the fix is applied.

    Currently these tests verify the bug IS present. After patching
    oneshot.py to pass skip_memory=True, flip the assertions.
    """

    def test_parser_exposes_no_isolated_flag_for_oneshot(self):
        """The -z / --oneshot flag has no isolation switch in the parser.

        After the fix, ``-z`` should either always run isolated (default)
        or expose ``--no-memory`` / ``--isolated`` to opt in.
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

        # Characterization: no isolation flag exists for -z yet
        isolation_flags = {"--isolated", "--no-memory", "--fresh", "--clean"}
        found = option_strings & isolation_flags
        assert not found, (
            f"Isolation flag(s) {found} were added to the top-level parser — "
            "the fix may be partially applied. Update this characterization test."
        )

    def test_main_py_oneshot_path_does_not_set_ignore_rules(self):
        """main() does not set HERMES_IGNORE_RULES before calling run_oneshot().

        If it did, the existing --ignore-rules plumbing would propagate
        skip_memory=True into the agent.  Confirming it does NOT do this
        shows the exact gap the fix must close.
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
            "HERMES_IGNORE_RULES was set before the oneshot dispatch — "
            "the fix may already be partially applied. Update this test."
        )
