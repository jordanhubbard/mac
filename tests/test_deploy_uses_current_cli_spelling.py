"""Deploy scripts must invoke the CLI as it is actually spelled.

The administrative commands moved under `mac admin`. Every in-repo caller was
migrated -- three times, because each pass matched one invocation SHAPE and
missed the next:

    mac fleet ...                     the literal word
    mac --db "$DSN" init ...          options before the command
    "$MAC_HOME/venv/bin/mac" openshell ...   a quoted path, so the closing
                                             quote sat between `mac` and the
                                             subcommand

The third one shipped. It failed the fleet deploy at phase 2 on every node --
`bootstrap_enabled_openshell` called `mac openshell render-policy`, got the
redirect, and exited 2 -- leaving seven nodes under dispatch hold. A grep would
have caught it in a second; nothing was grepping.

This is that grep. It is deliberately shape-agnostic: it looks for a moved
command following ANY reference to the mac binary on the same line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mac.cli import build_parser
from mac.cli_surface import _MOVED_TO_ADMIN

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("deploy", "scripts")
SUFFIXES = {".sh", ".py", ".yaml", ".yml", ".service", ".plist"}


def _moved_names():
    build_parser()  # populates the moved set
    return sorted(_MOVED_TO_ADMIN)


def _offenders():
    names = "|".join(re.escape(n) for n in _moved_names())
    # Any way of naming the binary: bare word, a path, a quoted path, or a
    # variable holding one -- then optional global options, then the command.
    invocation = re.compile(
        r'(?:"[^"\n]*/mac"|\'[^\'\n]*/mac\'|[\w./${}-]*/mac|\bmac)'
        r'\s+(?:--[\w-]+(?:=\S+)?\s+|"\$[\w{}]+"\s+|\$[\w{}]+\s+)*'
        r'(%s)\b' % names
    )
    found = []
    for directory in SCANNED:
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if not path.is_file() or path.suffix not in SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                match = invocation.search(line)
                if not match:
                    continue
                # `mac admin <cmd>` is the correct spelling.
                if re.search(r'\badmin\s+%s\b' % re.escape(match.group(1)), line):
                    continue
                found.append(
                    "%s:%d: %s" % (path.relative_to(REPO_ROOT), number, line.strip()[:100])
                )
    return found


def test_no_deploy_script_uses_a_pre_admin_cli_spelling():
    offenders = _offenders()

    assert offenders == [], (
        "these invoke a command that now lives under `mac admin`; on a fleet "
        "node they get the redirect and exit 2:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_would_actually_catch_the_shape_that_shipped(tmp_path):
    """A guard that cannot detect the bug it was written for is decoration.

    The exact line from bootstrap-openshell.sh that failed the deploy.
    """
    names = "|".join(re.escape(n) for n in _moved_names())
    invocation = re.compile(
        r'(?:"[^"\n]*/mac"|\'[^\'\n]*/mac\'|[\w./${}-]*/mac|\bmac)'
        r'\s+(?:--[\w-]+(?:=\S+)?\s+|"\$[\w{}]+"\s+|\$[\w{}]+\s+)*'
        r'(%s)\b' % names
    )

    assert invocation.search('"$MAC_HOME/venv/bin/mac" openshell render-policy \\')
    assert invocation.search('mac fleet doctor')
    assert invocation.search('mac --db "$DOCS_DB" init')
    # And does not fire on the corrected spelling.
    line = '"$MAC_HOME/venv/bin/mac" admin openshell render-policy'
    match = invocation.search(line)
    assert match is None or re.search(r'\badmin\s+%s\b' % match.group(1), line)
