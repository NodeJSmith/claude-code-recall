# Context: Wave 1 — Logging & Test Infrastructure

## Problem & Motivation

25 of 34 core library modules have zero logging. The three worst dark-operation modules (`search_vector.py`, `embeddings.py`, `session_tail.py`) silently swallow errors, making failures invisible. Separately, test infrastructure has minor duplication that adds maintenance drag.

## Key Decisions

1. Library modules use `log = logging.getLogger(LOGGER_NAME)` importing `LOGGER_NAME` from `ccrecall.models`. No handlers, no `basicConfig`, no `setLevel`.
2. Level selection: `log.exception()` for operation failures the caller sees as degraded results, `log.warning()` for availability checks that degrade silently, `log.debug()` for per-line parse skips.
3. `db.py:vec_available()` is included even though the issue (#145) only names 3 modules — it's the same pattern (silent `except Exception: return False`) and is one line to fix.
4. `_make_conn()` replacement uses the existing `memory_db` conftest fixture, not a new helper.

## Constraints

- Do NOT add handlers, `basicConfig`, or `setLevel` — these are library modules.
- Do NOT change error handling behavior (don't convert `return []` to `raise`, etc.).
- Do NOT touch `test_llm_summary_evaluation.py` — its `FIXTURE_DIR` points to a different subdirectory.
- Do NOT refactor any module structure — only add logging declarations and log calls.
