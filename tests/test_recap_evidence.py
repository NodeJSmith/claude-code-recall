import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).parents[1] / "tools" / "check_recap_evidence.py"


def run_checker(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_checker_accepts_complete_deidentified_audit(tmp_path: Path) -> None:
    audit = tmp_path / "audit.md"
    audit.write_text(
        """# Eligibility Audit

## Sample strata

| Stratum | Sample count |
| --- | ---: |
| Short | 4 |

## Measures

| Measure | Definition |
| --- | --- |
| Prose | Imported prose |

## Labels

| Label | Count |
| --- | ---: |
| Meaningful | 3 |

## Candidate-rule comparison

| Rule | Eligible | Useful-session recall | Precision |
| --- | ---: | ---: | ---: |
| Rule A | 3 | 3/3 | 1 |

## Selected policy

`ELIGIBILITY_POLICY_VERSION = 1`

| Threshold | Value |
| --- | ---: |
| Substantive prose characters | 240 |

## Reason codes

| Reason code | Meaning |
| --- | --- |
| eligible_meaningful | Eligible |
"""
    )

    result = run_checker(audit)

    assert result.returncode == 0, result.stderr


def test_checker_accepts_aggregate_session_recap_evaluation(tmp_path: Path) -> None:
    evaluation = tmp_path / "evaluation.md"
    evaluation.write_text(
        """# Session Recap Evaluation

## Method

Aggregate review compared normalized imported SQLite input with source input.

## Aggregate input results

| Input authority | Completed recaps | Recognition | Work arc | Outcome | Relative packet size |
| --- | ---: | ---: | ---: | ---: | --- |
| Imported SQLite | 6/6 | 6/6 | 6/6 | 6/6 | Smaller |

## Aggregate model results

| Model | Completed DB-input calls | Recognition | Work arc | Outcome |
| --- | ---: | ---: | ---: | ---: |
| Sonnet | 6/6 | 6/6 | 6/6 | 6/6 |

## Decision

Imported SQLite is canonical and Sonnet is the default.

## Privacy boundary

Only aggregate evidence is committed.
"""
    )

    result = run_checker(evaluation)

    assert result.returncode == 0, result.stderr


def test_checker_rejects_private_rows_and_identifiers(tmp_path: Path) -> None:
    audit = tmp_path / "audit.md"
    audit.write_text(
        """# Eligibility Audit

## Sample strata

| Stratum | Sample count |
| --- | ---: |
| Short | 4 |

## Measures

| Measure | Definition |
| --- | --- |
| Prose | Imported prose |

## Labels

| Label | Count |
| --- | ---: |
| Meaningful | 3 |

## Candidate-rule comparison

| Rule | Eligible | Useful-session recall | Precision |
| --- | ---: | ---: |
| Rule A | 3 | 3/3 | 1 |

## Selected policy

`ELIGIBILITY_POLICY_VERSION = 1`

| Threshold | Value |
| --- | ---: |
| Substantive prose characters | 240 |

## Reason codes

| Reason code | Meaning |
| --- | --- |
| eligible_meaningful | Eligible |

| Session | Label |
| --- | --- |
| 123e4567-e89b-12d3-a456-426614174000 | meaningful |

Excerpt: "the user asked for a specific change"
Path: src/private.py
"""
    )

    result = run_checker(audit)

    assert result.returncode != 0
    assert "UUID-shaped identifier" in result.stderr
    assert "private label row" in result.stderr
    assert "transcript excerpt" in result.stderr
    assert "path-like content" in result.stderr


def test_checker_rejects_incomplete_required_evidence(tmp_path: Path) -> None:
    audit = tmp_path / "audit.md"
    audit.write_text(
        """# Eligibility Audit

## Sample strata

| Stratum | Sample count |
| --- | ---: |

## Measures

| Measure | Definition |
| --- | --- |
| Prose | Imported prose |

## Labels

| Label | Count |
| --- | ---: |
| Meaningful | 1 |

## Candidate-rule comparison

| Rule | Eligible | Useful-session recall | Precision |
| --- | ---: | ---: | ---: |

## Selected policy

`ELIGIBILITY_POLICY_VERSION = draft`

| Threshold | Value |
| --- | ---: |
| Minimum exchanges | none |

## Reason codes

| Reason code | Meaning |
| --- | --- |
| eligible | Eligible |
"""
    )

    result = run_checker(audit)

    assert result.returncode != 0
    assert "missing sample strata rows" in result.stderr
    assert "missing candidate-rule comparison rows" in result.stderr
    assert "missing ELIGIBILITY_POLICY_VERSION" in result.stderr
    assert "missing selected threshold values" in result.stderr


def test_checker_rejects_alternate_private_label_tables_and_reviewer_notes(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "audit.md"
    audit.write_text(
        """# Eligibility Audit

## Sample strata

| Stratum | Sample count |
| --- | ---: |
| Short | 4 |

## Measures

| Measure | Definition |
| --- | --- |
| Prose | Imported prose |

## Labels

| Branch | Label |
| --- | --- |
| local-branch | meaningful |

## Candidate-rule comparison

| Rule | Eligible | Useful-session recall | Precision |
| --- | ---: | ---: | ---: |
| Rule A | 3 | 3/3 | 1 |

## Selected policy

`ELIGIBILITY_POLICY_VERSION = 1`

| Threshold | Value |
| --- | ---: |
| Minimum exchanges | 2 |

## Reason codes

| Reason code | Meaning |
| --- | --- |
| eligible | Eligible |

Reviewer notes: the participant asked for a private change.
"""
    )

    result = run_checker(audit)

    assert result.returncode != 0
    assert "invalid labels table" in result.stderr
    assert "reviewer notes" in result.stderr


def test_checker_rejects_inline_paths_and_multicolumn_private_label_rows(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "audit.md"
    audit.write_text(
        """# Eligibility Audit

## Sample strata

| Stratum | Sample count |
| --- | ---: |
| Short | 4 |

## Measures

| Measure | Definition |
| --- | --- |
| Prose | Imported prose |

## Labels

| Label | Count |
| --- | ---: |
| Meaningful | 3 |

## Candidate-rule comparison

| Rule | Eligible | Useful-session recall | Precision |
| --- | ---: | ---: | ---: |
| Rule A | 3 | 3/3 | 1 |

## Selected policy

`ELIGIBILITY_POLICY_VERSION = 1`

| Threshold | Value |
| --- | ---: |
| Minimum exchanges | 2 |

## Reason codes

| Reason code | Meaning |
| --- | --- |
| eligible | Eligible |

| Session UUID | Exchange count | Label |
| --- | ---: | --- |
| local-session | 2 | meaningful |

Read `src/private.py` and [the private file](private/notes.md).
"""
    )

    result = run_checker(audit)

    assert result.returncode != 0
    assert "private label row" in result.stderr
    assert "path-like content" in result.stderr


def test_checker_rejects_recap_private_material(tmp_path: Path) -> None:
    evaluation = tmp_path / "evaluation.md"
    evaluation.write_text(
        """# Session Recap Evaluation

## Method

Aggregate review.

## Aggregate input results

| Input authority | Completed recaps | Recognition | Work arc | Outcome | Relative packet size |
| --- | ---: | ---: | ---: | ---: | --- |
| Imported SQLite | 6/6 | 6/6 | 6/6 | 6/6 | Smaller |

## Aggregate model results

| Model | Completed DB-input calls | Recognition | Work arc | Outcome |
| --- | ---: | ---: | ---: | ---: |
| Sonnet | 6/6 | 6/6 | 6/6 | 6/6 |

## Decision

Recap: private text
Prompt: private instruction
Private mapping: sample-one
Reviewer notes: private observation
Path: local/private.md
Identifier: 123e4567-e89b-12d3-a456-426614174000
An inline Transcript excerpt: private conversation content.
**User:** private turn
*Assistant:* private response
`Recap`: private summary
[User](#): linked private turn
**[Recap](#):** linked private summary
[User](https://example.test/a_(b)): linked private turn
[User][private-turn]
[Outer [User]](https://example.test/private): nested linked private turn

[private-turn]: #

## Privacy boundary

Only aggregate evidence is committed.
"""
    )

    result = run_checker(evaluation)

    assert result.returncode != 0
    assert "transcript excerpt" in result.stderr
    assert "prompt or raw output" in result.stderr
    assert "private mapping" in result.stderr
    assert "reviewer notes" in result.stderr
    assert "Markdown link" in result.stderr
    assert "path-like content" in result.stderr
    assert "UUID-shaped identifier" in result.stderr
