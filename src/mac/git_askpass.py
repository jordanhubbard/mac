"""Narrow GitHub HTTPS askpass helper for controller-owned Git subprocesses.

The helper is deliberately a package console entry point instead of a generated
shell script.  The controller passes the executable path and ``GH_TOKEN`` only
in the child environment; the credential never enters argv, repository config,
the task ledger, or a persistent credential store.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Sequence


_GITHUB_PROMPT_RE = re.compile(
    r"https://(?:x-access-token@)?github\.com(?=[:/'\s]|$)", re.IGNORECASE
)


def _credential_for_prompt(prompt: str, token: str) -> str | None:
    if _GITHUB_PROMPT_RE.search(prompt) is None:
        return None
    normalized = prompt.casefold()
    if "username" in normalized:
        return "x-access-token"
    if "password" in normalized:
        return token
    return None


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        return 1
    token = os.environ.get("GH_TOKEN", "")
    if (
        not token
        or token != token.strip()
        or any(character in token for character in ("\x00", "\r", "\n"))
    ):
        return 1
    credential = _credential_for_prompt(arguments[0], token)
    if credential is None:
        return 1
    sys.stdout.write(credential + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
