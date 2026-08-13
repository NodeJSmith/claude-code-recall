import json
from dataclasses import replace

import pytest

from ccrecall.recap_contract import ELIGIBILITY_POLICY_VERSION
from ccrecall.recap_eligibility import (
    BELOW_MEANINGFUL_THRESHOLD,
    BELOW_MIN_EXCHANGES,
    ELIGIBLE_SUBSTANTIVE_PROSE,
    ELIGIBLE_WORK_EVIDENCE,
    MISSING_ACTIVE_BRANCH,
    MISSING_CURRENT_SUMMARY,
    NO_ELIGIBLE_MESSAGES,
    EligibilityInput,
    evaluate_branch,
    evaluate_branches,
    evaluate_eligibility,
)
from ccrecall.summarizer import SUMMARY_VERSION


def input_for(*, active=True, summary_current=True, messages=(), files=None, commits=None, elapsed=120):
    return EligibilityInput(
        branch_id=1,
        active=active,
        summary_current=summary_current,
        started_at="2026-01-01T00:00:00Z",
        ended_at=f"2026-01-01T00:0{elapsed // 60}:00Z",
        files_modified=json.dumps(files or []),
        commits=json.dumps(commits or []),
        messages=tuple(messages),
    )


def turns(*contents, tool_content=None):
    tool_content = tool_content or [None] * len(contents)
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": content, "tool_content": tool_content[index]}
        for index, content in enumerate(contents)
    ]


@pytest.mark.parametrize(
    ("input", "reason"),
    [
        (input_for(active=False), MISSING_ACTIVE_BRANCH),
        (input_for(summary_current=False), MISSING_CURRENT_SUMMARY),
        (input_for(), NO_ELIGIBLE_MESSAGES),
        (input_for(messages=turns("one")), BELOW_MIN_EXCHANGES),
    ],
)
def test_prerequisite_reasons(input, reason):
    decision = evaluate_eligibility(input)
    assert decision.eligible is False
    assert decision.reason == reason
    assert decision.policy_version == ELIGIBILITY_POLICY_VERSION


def test_short_useful_session_qualifies_by_substantive_prose():
    decision = evaluate_eligibility(input_for(messages=turns("u" * 300, "a" * 300, "u" * 10)))
    assert decision.eligible is True
    assert decision.reason == ELIGIBLE_SUBSTANTIVE_PROSE
    assert decision.measures.exchange_count == 2
    assert decision.measures.substantive_prose_chars == 610


def test_corrobated_work_evidence_qualifies_with_prose_floor_and_duration():
    decision = evaluate_eligibility(input_for(messages=turns("u" * 240, "a", "u"), files=["src/a.py"]))
    assert decision.eligible is True
    assert decision.reason == ELIGIBLE_WORK_EVIDENCE
    assert decision.measures.file_count == 1


def test_long_repetitive_tool_activity_never_qualifies():
    decision = evaluate_eligibility(
        input_for(messages=turns("u", "", "u", tool_content=[None, "\n".join("[Read: x]" for _ in range(100)), None]))
    )
    assert decision.eligible is False
    assert decision.reason == BELOW_MEANINGFUL_THRESHOLD
    assert decision.measures.nonrepetitive_tool_actions == 1


def test_tool_actions_collapse_only_consecutive_same_tool_runs():
    decision = evaluate_eligibility(
        input_for(
            messages=turns(
                "u" * 240,
                "",
                "u",
                tool_content=[None, "[Read: a]\n[Read: b]\n[Bash: c]\n[Read: d]", None],
            )
        )
    )
    assert decision.eligible is True
    assert decision.reason == ELIGIBLE_WORK_EVIDENCE
    assert decision.measures.nonrepetitive_tool_actions == 3


@pytest.mark.parametrize("evidence", [{"files": ["a", "a"]}, {"commits": ["abc"]}])
def test_file_or_commit_evidence_qualifies_only_with_the_other_evidence_requirements(evidence):
    decision = evaluate_eligibility(input_for(messages=turns("u" * 240, "a", "u"), **evidence))
    assert decision.eligible is True
    assert decision.reason == ELIGIBLE_WORK_EVIDENCE


def test_harness_text_and_negative_elapsed_time_do_not_supply_evidence():
    input = input_for(
        messages=turns("<system-reminder> ignored", "a" * 240, "u"),
        files=["src/a.py"],
        elapsed=0,
    )
    decision = evaluate_eligibility(replace(input, started_at="2026-01-01T00:02:00Z", ended_at="2026-01-01T00:00:00Z"))
    assert decision.eligible is False
    assert decision.reason == BELOW_MEANINGFUL_THRESHOLD
    assert decision.measures.elapsed_seconds == 0
    assert decision.measures.substantive_prose_chars == 241


@pytest.fixture
def eligibility_db(memory_db):
    memory_db.execute("INSERT INTO sessions (uuid) VALUES ('eligible-session')")
    session_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    memory_db.execute(
        """INSERT INTO branches (session_id, leaf_uuid, is_active, context_summary, context_summary_json,
                                  summary_version, started_at, ended_at)
           VALUES (?, 'leaf', 1, 'summary', '{}', ?, '2026-01-01T00:00:00Z', '2026-01-01T00:02:00Z')""",
        (session_id, SUMMARY_VERSION),
    )
    branch_id = memory_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    for position, (role, content) in enumerate((("user", "u" * 300), ("assistant", "a" * 300), ("user", "next"))):
        memory_db.execute(
            "INSERT INTO messages (session_id, uuid, role, content) VALUES (?, ?, ?, ?)",
            (session_id, f"m-{position}", role, content),
        )
        memory_db.execute(
            "INSERT INTO branch_messages (branch_id, message_id, position) VALUES (?, ?, ?)",
            (branch_id, memory_db.execute("SELECT last_insert_rowid()").fetchone()[0], position),
        )
    memory_db.commit()
    return branch_id


def test_query_and_evaluator_have_identical_decisions(eligibility_db, memory_db):
    from_db = evaluate_branch(memory_db.cursor(), eligibility_db)
    all_branches = evaluate_branches(memory_db.cursor())
    assert from_db == all_branches[eligibility_db]
    assert from_db.reason == ELIGIBLE_SUBSTANTIVE_PROSE


def test_query_excludes_notification_only_messages(eligibility_db, memory_db):
    memory_db.execute(
        """UPDATE messages SET is_notification = 1
           WHERE id IN (SELECT message_id FROM branch_messages WHERE branch_id = ?)""",
        (eligibility_db,),
    )
    memory_db.commit()
    decision = evaluate_branch(memory_db.cursor(), eligibility_db)
    assert decision.reason == NO_ELIGIBLE_MESSAGES
