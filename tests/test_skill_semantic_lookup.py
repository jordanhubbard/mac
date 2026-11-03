from mac.hermes_config_surface import search_skill_records


def test_skill_lookup_ranks_declared_description_not_label():
    skills = [
        {"name": "opaque-a", "description": "Inspect PostgreSQL query plans and indexes"},
        {"name": "database", "description": "Format markdown release notes"},
    ]

    matches = search_skill_records("slow postgres index", skills)

    assert [item["name"] for item in matches] == ["opaque-a"]
    assert matches[0]["semantic_score"] > 0
