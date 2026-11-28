from pathlib import Path


DOCUMENT = (
    Path(__file__).parents[1]
    / "docs"
    / "investigations"
    / "condition-6-convergence-protocol.md"
)


def test_condition_6_defines_detection_and_node_contract():
    text = DOCUMENT.read_text(encoding="utf-8")

    for required in (
        "Distinct authors",
        "Independent paths",
        "Same testable claim",
        "No prior merge",
        "`input_ids`",
        "`input_authors`",
        "`residuals`",
        "`next_question`",
        "converges_with",
    ):
        assert required in text


def test_condition_6_records_cross_agent_instance_and_limits():
    text = DOCUMENT.read_text(encoding="utf-8")

    for required in (
        "id: convergence:sos-bootstrap:five-six-node",
        "- bc661e2d",
        "repo:docs/investigations/fleet-sos-bootstrap.md",
        "agent-research-a",
        "agent-research-b",
        "exact body of bc661e2d is unavailable",
        "would invalidate the independence classification",
    ):
        assert required in text
