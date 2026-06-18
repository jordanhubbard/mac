from __future__ import annotations

import importlib.util
import subprocess
import textwrap
from pathlib import Path


def test_mac_runtime_context_patch_makes_hermes_prompt_load_runtime_markdown(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "hermes-agent"
    prompt_builder = repo / "agent" / "prompt_builder.py"
    prompt_builder.parent.mkdir(parents=True)
    prompt_source = (
        "# representative upstream prompt_builder filler\n" * 1306
        + textwrap.dedent(
            '''
            from __future__ import annotations

            import logging
            import os
            from pathlib import Path
            from typing import Optional

            logger = logging.getLogger(__name__)


            def get_hermes_home() -> Path:
                return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


            def _scan_context_content(content: str, name: str) -> str:
                return content


            def _truncate_content(content: str, name: str) -> str:
                return content[:20000]


            def _find_hermes_md(cwd_path: Path):
                return None


            def _load_project_context(cwd_path: Path) -> str:
                return "## Project Context\\n\\nProject instructions"


            def load_soul_md() -> Optional[str]:
                try:
                    return None
                except Exception:
                    return None


            def _load_hermes_md(cwd_path: Path) -> str:
                """.hermes.md / HERMES.md — walk to git root."""
                hermes_md_path = _find_hermes_md(cwd_path)
                return "" if hermes_md_path is None else hermes_md_path.read_text()


            '''
        ).lstrip()
        + ("# representative upstream prompt helper filler\n" * 82)
        + textwrap.dedent(
            '''
            def build_context_files_prompt(cwd: Optional[str] = None, skip_soul: bool = False) -> str:
                """Build prompt from context files.

                  4. .cursorrules / .cursor/rules/*.mdc  (cwd only)

                SOUL.md from HERMES_HOME is independent and always included when present.
                Each context source is capped at 20,000 chars.

                When *skip_soul* is True, SOUL.md is not included here (it was already
                loaded by the caller).
                """
                cwd_path = Path(cwd or os.getcwd())
                sections = []
                project_context = _load_project_context(cwd_path)
                if project_context:
                    sections.append(project_context)

                # SOUL.md from HERMES_HOME only — skip when already loaded as identity
                if not skip_soul:
                    soul_content = load_soul_md()
                    if soul_content:
                        sections.append("## SOUL.md\\n\\n" + soul_content)
                return "\\n\\n".join(sections)
            '''
        ).lstrip()
    )
    prompt_builder.write_text(prompt_source, encoding="utf-8")

    root = repo
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    patch = Path(__file__).resolve().parents[1] / "deploy" / "hermes" / "mac-runtime-context-prompt.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=root, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=root, check=True)

    hermes_home = tmp_path / ".hermes"
    runtime_markdown = hermes_home / "mac-runtime-context.md"
    runtime_markdown.parent.mkdir()
    runtime_markdown.write_text(
        "\n".join(
            [
                "# MAC Task and Project Runtime",
                "",
                "## First-Class Objects",
                "- `fleets`: authority `mac`; source `MAC fleet records`",
                "- `tasks`: authority `mac`; source `mac task records and task history`",
                "- `projects`: authority `mac`; source `MAC project summaries`",
                "- `agents`: authority `mac`; source `MAC agent registry`",
                "",
                "## Project Bridge",
                "- `mac-hermes project-items`",
                "",
                "## Agent View",
                "- `mac-hermes agents`",
                "- `mac-hermes agent-identity agent_rocky`",
                "",
                "## Direct Session Parity",
                "- `bd prime`",
                "- `mac-hermes work-context hermes_rocky --active-only`",
                "- `hermes_oneshot_executor`: `mac-hermes-task-executor`",
                "- `hgmac agents list`",
                "- `hgmac fleets list`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(runtime_markdown))

    spec = importlib.util.spec_from_file_location("patched_prompt_builder", prompt_builder)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    runtime_context = module._load_mac_runtime_context()
    prompt = module.build_context_files_prompt(cwd=str(tmp_path), skip_soul=True)

    for rendered in (runtime_context, prompt):
        assert "MAC Task and Project Runtime" in rendered
        assert "First-Class Objects" in rendered
        assert "`fleets`: authority `mac`" in rendered
        assert "`tasks`: authority `mac`" in rendered
        assert "`projects`: authority `mac`" in rendered
        assert "`agents`: authority `mac`" in rendered
        assert "Project Bridge" in rendered
        assert "mac-hermes project-items" in rendered
        assert "Agent View" in rendered
        assert "mac-hermes agents" in rendered
        assert "Direct Session Parity" in rendered
        assert "bd prime" in rendered
        assert "mac-hermes work-context hermes_rocky --active-only" in rendered
        assert "hermes_oneshot_executor" in rendered
        assert "mac-hermes-task-executor" in rendered
        assert "hgmac agents list" in rendered
        assert "hgmac fleets list" in rendered
    assert "SOUL.md" not in prompt
