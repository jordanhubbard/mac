"""`mac admin` must justify itself the way the top level already does.

The object-model layer collapsed 55 top-level commands to six, and five test
modules keep them that way. But that layer was deliberately additive --
"nothing is renamed and nothing is removed" -- so the operational surface did
not shrink, it moved. `mac admin` now carries 53 groups and roughly 245 leaf
commands: about two thirds of the whole CLI.

COMMAND_GROUPS already describes every command in one line, and
test_every_grouped_command_actually_exists rejects a catalogue entry naming a
command that is gone. Only that direction was ever checked. The reverse -- a
NEW `admin` group that nobody catalogued -- passed silently, which is how the
surface grew unremarked while the object model above it stayed at six
deliberate commands.

This does not judge whether a group deserves to exist; that is a product
decision. It makes the decision visible: adding operational surface now costs
one line saying what it is for.
"""

from __future__ import annotations

import pytest

from mac.cli import build_parser
from mac.cli_surface import admin_group_names, command_descriptions

# `help` is installed by the surface layer at every level rather than
# registered as a group, so it has no catalogue entry of its own and needs
# none -- its purpose is the same wherever it appears.
_INSTALLED_BY_THE_SURFACE_LAYER = {"help"}


@pytest.fixture(scope="module")
def parser():
    return build_parser()


def test_every_admin_group_is_catalogued(parser):
    """An uncatalogued group is surface nobody has said the purpose of. It is
    also invisible in `mac admin --help`, which renders from the catalogue --
    so it is reachable, undocumented, and unmentioned."""
    groups = set(admin_group_names(parser)) - _INSTALLED_BY_THE_SURFACE_LAYER

    uncatalogued = sorted(groups - set(command_descriptions()))

    assert not uncatalogued, (
        "these `mac admin` groups exist but COMMAND_GROUPS does not describe "
        "them: %s\nAdd a one-line description in src/mac/cli_surface.py, or "
        "remove the group." % ", ".join(uncatalogued)
    )


def test_an_alias_is_not_counted_as_a_second_group(parser):
    """`comm` and `communication` share one parser object. Counting both would
    demand two catalogue entries for one surface, and the next reader would
    "fix" the duplication by deleting a working alias."""
    groups = admin_group_names(parser)

    assert "communication" in groups
    assert "comm" not in groups
    assert len(groups) == len(set(groups))
