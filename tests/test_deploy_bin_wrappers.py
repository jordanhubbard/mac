"""deploy/bin holds, as real files, the wrappers fleet-node-install.sh still
generates from quoted heredocs.  scripts/fleet-update installs from deploy/bin,
so until the installer is deleted the two copies must not drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"

# file -> pattern capturing that file's heredoc body in the installer
WRAPPERS = {
    "mac-service": r"cat > \"\$wrapper\" <<'EOF'\n(#!/usr/bin/env bash\nset -euo pipefail\n# macOS gives.*?)\nEOF\n",
    "mac-agent-service": (
        r"cat > \"\$wrapper\" <<'EOF'\n(#!/usr/bin/env bash\nset -euo pipefail\n"
        r"ulimit -n \"\$\{MAC_SERVICE_NOFILE_LIMIT:-4096\}\" 2>/dev/null \|\| true\nulimit -c.*?)\nEOF\n"
    ),
    "mac-agent-startup-self-test": r"cat > \"\$selftest\" <<'EOF'\n(.*?)\nEOF\n",
    "mac-task-executor": r"cat > \"\$executor\" <<'EOF'\n(.*?)\nEOF\n",
    "mac-task-executor.py": r"cat > \"\$executor_py\" <<'PY'\n(.*?)\nPY\n",
}


@pytest.mark.parametrize("name", sorted(WRAPPERS))
def test_deploy_bin_wrapper_matches_installer_heredoc(name: str) -> None:
    bodies = re.findall(WRAPPERS[name], INSTALLER.read_text(encoding="utf-8"), re.S)
    assert len(bodies) == 1, "expected exactly one %s heredoc in the installer" % name
    assert (ROOT / "deploy" / "bin" / name).read_text(encoding="utf-8") == bodies[0] + "\n"


def test_deploy_bin_holds_exactly_the_extracted_wrappers() -> None:
    assert sorted(p.name for p in (ROOT / "deploy" / "bin").iterdir()) == sorted(WRAPPERS)
