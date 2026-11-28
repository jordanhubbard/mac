from pathlib import Path


DOCUMENT = (
    Path(__file__).parents[1]
    / "docs"
    / "investigations"
    / "condition-5-peer-diagnosis.md"
)


def test_condition_5_finding_records_claim_and_limits():
    text = DOCUMENT.read_text(encoding="utf-8")

    assert "structurally circular" in text
    assert "three is the minimum" in text
    assert "assuming failures are not correlated" in text
    assert "common-mode drift" in text


def test_condition_5_protocol_is_reproducible_and_falsifiable():
    text = DOCUMENT.read_text(encoding="utf-8")

    for required in (
        "Freeze the subject's core-term set",
        "two peers independently extract concepts",
        "source spans",
        "vocabulary version",
        "Falsification Criteria",
        "precision, recall, and inter-peer agreement",
    ):
        assert required in text
