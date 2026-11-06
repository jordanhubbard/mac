"""Models for the fleet deliberation datatype.

A Deliberation is a structured coordination record opened when an agent
encounters an architectural conflict it cannot resolve unilaterally.  Three
resolution paths exist:

  - authority   : an agent listed in authorized_resolvers calls resolve()
  - unanimity   : every participant's final_position converges
  - expiry       : expires_at passes; deliberation auto-closes as withdrawn

Schema v3 (exploration run 20): authorized_resolvers derived at CREATE time
from domain ownership, not from creator identity at resolution time.  This
closes the self-referential authority defect in v2.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from mac.models import JsonDict, StrEnum


class DeliberationStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    WITHDRAWN = "withdrawn"


class DeliberationScope(StrEnum):
    DESIGN = "design"
    OPERATIONS = "operations"
    POLICY = "policy"


class ParticipantPosition(StrEnum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    ABSTAIN = "abstain"
    UNKNOWN = "unknown"


class ArgumentType(StrEnum):
    EMPIRICAL = "empirical"
    PRECEDENT = "precedent"
    PRINCIPLE = "principle"
    OBJECTION = "objection"


@dataclass
class DeliberationArgument:
    """A single argument submitted during a deliberation."""

    id: str
    deliberation_id: str
    agent_id: str
    claim: str
    argument_type: str  # ArgumentType value
    created_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class DeliberationParticipant:
    """Position-provenance record for one agent in a deliberation."""

    deliberation_id: str
    agent_id: str
    initial_position: str  # ParticipantPosition value
    final_position: str  # ParticipantPosition value
    vote: str  # ParticipantPosition value
    moved_by: Optional[str]  # argument_id that changed their position, if any
    joined_at: str

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class Deliberation:
    """Fleet deliberation record."""

    id: str
    task_id: Optional[str]
    topic: str
    scope: str  # DeliberationScope value
    body: str
    conclusion: Optional[str]
    status: str  # DeliberationStatus value
    created_by: str
    authorized_resolvers: List[str]  # agent_ids allowed to resolve via authority path
    quorum_needed: int
    participants: List[DeliberationParticipant]
    arguments: List[DeliberationArgument]
    resolved_by: Optional[str]
    resolution_path: Optional[str]  # "authority" | "unanimity" | "expiry"
    expires_at: Optional[str]
    created_at: str
    updated_at: str

    def to_dict(self) -> JsonDict:
        d = asdict(self)
        return d
