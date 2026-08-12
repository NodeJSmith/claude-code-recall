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
