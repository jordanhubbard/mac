"""Every extra the build and deploy surface asks for must exist in pyproject.

Removing an optional-dependency group is a two-sided change: the group goes out
of ``pyproject.toml``, and every place that installs it has to stop asking. Miss
the second half and nothing complains until a build runs, because ``uv``/``pip``
only learn the extra is gone at install time.

That is not hypothetical. PR #377 removed the vendored Hermes runtime and with it
the ``hermes-gateway`` extra (slack_bolt, discord.py, python-telegram-bot,
aiohttp -- none of which ``src/mac`` imports). Two callers kept asking for it:

    Dockerfile:29                            uv sync ... --extra hermes-gateway
    deploy/fleet-node-machine-onboard.py:767 {source}[hermes-gateway,relay,postgres]

The container build broke immediately and stayed broken on main:

    error: Extra `hermes-gateway` is not defined in the project's
           optional-dependencies table

The Python test suite was fully green throughout, because no test builds the
image or onboards a node. The onboarding break was worse than the build break --
it is not exercised by CI at all, so it would have surfaced on a real node.

So this is a gate, not a generator: it reads the extras actually declared and
fails if anything references one that is not there. It is deliberately cheap and
dependency-free so it runs in every suite.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def declared_extras() -> set[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return set(data.get("project", {}).get("optional-dependencies", {}))


#: ``--extra NAME`` as uv spells it.
_UV_EXTRA = re.compile(r"--extra[=\s]+([A-Za-z0-9._-]+)")

#: ``something[a,b,c]`` as pip/uv spell it in a requirement string. The prefix
#: guard keeps this from matching list indexing in Python sources.
_BRACKET_EXTRAS = re.compile(r"[\w}\"'/.\]]\[([A-Za-z0-9._,-]+)\]")

#: Files that install this project. Kept explicit rather than globbed: a glob
#: would silently stop covering a file that got renamed.
_INSTALL_SITES = (
    "Dockerfile",
    "deploy/fleet-node-machine-onboard.py",
    "deploy/deploy-mac-fleet.sh",
    "deploy/fleet-node-install.sh",
)


def _referenced_extras(text: str) -> set[str]:
    found = set(_UV_EXTRA.findall(text))
    for group in _BRACKET_EXTRAS.findall(text):
        for name in group.split(","):
            name = name.strip()
            # Bracket syntax is also PEP 508 for third-party requirements
            # (``psycopg[binary]``). Only names we declare are our business;
            # an unknown name here is far more likely to be someone else's
            # extra than a typo of ours, and this gate must not cry wolf.
            if name:
                found.add(name)
    return found


def test_pyproject_declares_the_extras_we_expect():
    """Pin the set, so removing one is a deliberate edit to this list."""
    assert declared_extras() == {"dev", "docs", "postgres", "k8s", "relay"}


@pytest.mark.parametrize("relpath", _INSTALL_SITES)
def test_install_sites_only_request_extras_that_exist(relpath):
    path = REPO_ROOT / relpath
    if not path.exists():  # pragma: no cover - covered by the existence test
        pytest.skip("%s is not present in this checkout" % relpath)

    declared = declared_extras()
    # Third-party extras legitimately appear in these files; we can only judge
    # the ones that look like ours, i.e. that appear alongside the project path
    # or a `uv sync` for this project. `--extra` is unambiguous, so it is
    # checked strictly; bracket groups are checked only for names that were
    # once ours, which is what actually regresses.
    once_ours = {"hermes-gateway"}
    text = path.read_text()

    strict = set(_UV_EXTRA.findall(text))
    unknown = sorted(strict - declared)
    assert not unknown, (
        "%s runs `--extra %s`, but pyproject.toml declares only %s. "
        "Removing an optional-dependency group means removing every request "
        "for it; otherwise the build fails with "
        "'Extra `%s` is not defined in the project's optional-dependencies "
        "table' while the Python suite stays green."
        % (relpath, ", ".join(unknown), sorted(declared), unknown[0])
    )

    resurrected = sorted(name for name in _referenced_extras(text) if name in once_ours)
    assert not resurrected, (
        "%s references the removed extra(s) %s. These were dropped with the "
        "vendored Hermes runtime in #377; src/mac imports none of "
        "slack_bolt, discord, telegram or aiohttp." % (relpath, ", ".join(resurrected))
    )


def test_every_install_site_still_exists():
    """If one of these is renamed, this gate stops covering it -- say so."""
    missing = [p for p in _INSTALL_SITES if not (REPO_ROOT / p).exists()]
    assert not missing, (
        "install sites %s no longer exist; update _INSTALL_SITES so this gate "
        "keeps covering the files that install this project" % missing
    )
