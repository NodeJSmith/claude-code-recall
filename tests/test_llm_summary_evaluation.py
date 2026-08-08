import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "llm_summary_evaluation"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
REQUIRED_SCENARIOS = {
    "bug-investigation",
    "implementation-refactor",
    "planning-discovery",
}
REQUIRED_CATEGORIES = {
    "latest_state",
    "causal_history",
    "decision_rationale",
    "attempted_path",
    "unresolved_work",
    "handoff",
}


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_manifest_covers_required_scenarios_and_categories():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["version"] == 1
    assert set(manifest["required_fact_categories"]) == REQUIRED_CATEGORIES
    assert {scenario["id"] for scenario in manifest["scenarios"]} == REQUIRED_SCENARIOS

    for scenario in manifest["scenarios"]:
        assert set(scenario["facts"]) == REQUIRED_CATEGORIES
        for category in REQUIRED_CATEGORIES:
            assert scenario["facts"][category], f"{scenario['id']} is missing {category} facts"


def test_every_manifest_uuid_exists_in_its_fixture_and_fixtures_are_deidentified_long_branches():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    for scenario in manifest["scenarios"]:
        fixture_path = FIXTURE_DIR / scenario["fixture"]
        assert fixture_path.exists(), f"missing fixture {fixture_path.name}"

        rows = _load_jsonl(fixture_path)
        message_rows = [row for row in rows if row.get("type") in {"user", "assistant"}]
        uuids = {row["uuid"] for row in message_rows}
        session_ids = {row["sessionId"] for row in message_rows}
        user_turns = [row for row in message_rows if row["type"] == "user"]
        assistant_turns = [row for row in message_rows if row["type"] == "assistant"]

        assert len(session_ids) == 1
        assert len(message_rows) >= 16, f"{scenario['id']} should stay long enough for review"
        assert len(user_turns) >= 8, f"{scenario['id']} lost too many user turns"
        assert len(assistant_turns) >= 8, f"{scenario['id']} lost too many assistant turns"
        assert all(str(row.get("cwd", "")).startswith("/synthetic/") for row in message_rows)
        serialized_fixture = fixture_path.read_text(encoding="utf-8")
        assert "/home/" not in serialized_fixture
        assert "/Users/" not in serialized_fixture
        assert "C:\\Users\\" not in serialized_fixture

        for facts in scenario["facts"].values():
            for fact in facts:
                assert fact["claim"].strip()
                assert fact["source_uuids"], f"{fact['id']} needs supporting UUIDs"
                assert set(fact["source_uuids"]).issubset(uuids), (
                    f"{fact['id']} references UUIDs missing from {scenario['fixture']}"
                )
