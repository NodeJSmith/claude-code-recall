# Design: Wave 1 — Logging & Test Infrastructure

**Date:** 2026-08-17
**Status:** draft
**Mode:** sketch

## Problem

25 of 34 core library modules have zero logging. The three worst — `search_vector.py`, `embeddings.py`, `session_tail.py` — silently swallow errors, making failed searches indistinguishable from empty results, embedding load failures invisible, and corrupt transcripts undetectable. Separately, test infrastructure has minor duplication (`FIXTURE_DIR` defined in 5 files, `_make_conn()` reimplemented in 2 files) that adds maintenance drag.

## Goals

- Make the three highest-risk dark-operation modules emit diagnostic signals on failure paths.
- Consolidate duplicated test infrastructure into conftest so future test changes don't diverge.

## Non-Goals

- Rolling out logging to all 25 zero-logging modules (that's wave 4 / #145 remainder).
- Refactoring any module's error handling behavior — only adding visibility to existing paths.

## Functional Requirements

- **FR#1** `search_vector.py`: every `except sqlite3.Error: return []` block logs the exception before returning, at ERROR level via `logging.getLogger(LOGGER_NAME)`.
- **FR#2** `embeddings.py:model_available()`: logs at WARNING when the model fails to load (the `except Exception` path).
- **FR#3** `embeddings.py:is_model_cached_on_disk()`: logs at DEBUG when the cache check fails (the `except Exception` path).
- **FR#4** `db.py:vec_available()`: logs at WARNING when the vec extension fails to load (the `except Exception` path).
- **FR#5** `session_tail.py:_last_event_timestamp()`: logs at DEBUG when a JSON line fails to parse (the `except json.JSONDecodeError` path).
- **FR#6** `session_tail.py:_extract_branch()`: logs at DEBUG when a JSON line fails to parse, and at WARNING when an `OSError` prevents reading the file.
- **FR#7** All test files importing `FIXTURE_DIR` use the definition from `conftest.py` instead of redefining it locally.
- **FR#8** `_make_conn()` in `test_context_alerts.py` and `test_backfill_tool_content.py` is replaced with the `memory_db` conftest fixture.

## Acceptance Criteria

- **AC#1** `uv run pytest` passes with zero failures (no regressions).
- **AC#2** `uvx prek run --all-files` passes (lint/format clean).
- **AC#3** `grep -rn 'logging.getLogger' src/ccrecall/search_vector.py src/ccrecall/embeddings.py src/ccrecall/session_tail.py src/ccrecall/db.py` shows logger declarations in all four files.
- **AC#4** `grep -rn 'FIXTURE_DIR = Path' tests/` returns only `tests/conftest.py` (no local redefinitions).
- **AC#5** `grep -rn 'def _make_conn(' tests/` returns only `test_summarizer.py` (which has the unrelated `_make_conn_for_sync_branch`).

## Approach

**Logging convention:** These are library modules (rule 1 from the project's logging rules). Each gets `log = logging.getLogger(LOGGER_NAME)` at module level, importing `LOGGER_NAME` from `ccrecall.models`. No handlers, no `basicConfig`, no `setLevel`.

**Level selection:**
- `log.exception()` inside `except` blocks for ERROR-level paths where an operation failed and the caller sees degraded results (search_vector's silent `return []` paths).
- `log.warning()` for availability checks that degrade silently (model_available, vec_available) — these are expected on some machines but should be visible.
- `log.debug()` for per-line parse skips in transcript tail reading — these fire on every corrupt line and shouldn't spam at INFO.

**search_vector.py** has 5 `except sqlite3.Error: return []` blocks in `execute_chunk_knn` (lines 137, 147, 163, 179, 196) plus the serialize call. Each gets `log.exception("...")` before the return. The existing docstring at line 129 already documents this degradation behavior — logging makes it observable.

**embeddings.py** has `model_available()` (line 170 `except Exception: return False`) and `is_model_cached_on_disk()` (line 153 `except Exception: return False`). The former gets `log.warning()`, the latter `log.debug()` (cache misses are routine).

**db.py** has `vec_available()` (line 97 `except Exception:`) — gets `log.warning()`. This is in `db.py` not `db_base.py`, so it already has access to the import chain; it just needs the logger declaration and the log call.

**session_tail.py** has `_last_event_timestamp()` (line 321 `except json.JSONDecodeError: continue`) and `_extract_branch()` (line 527 `except json.JSONDecodeError: continue`, line 532 `except OSError: return None`). JSONDecodeError paths get `log.debug()`, OSError gets `log.warning()`.

**Test infrastructure:**
- `conftest.py` already defines `FIXTURE_DIR` at line 14. The 5 test files that redefine it (`test_project_ops.py`, `test_integration.py`, `test_sync_hook.py`, `test_import_pipeline.py`, `test_session_ops.py`) simply replace their local definition with `from conftest import FIXTURE_DIR`. Note: `test_llm_summary_evaluation.py` uses a *different* fixtures subdir (`fixtures/llm_summary_evaluation`) — leave it alone.
- `_make_conn()` in `test_context_alerts.py` and `test_backfill_tool_content.py` creates an in-memory connection with schema applied — functionally identical to the `memory_db` fixture in conftest (both use `SCHEMA`, not `SCHEMA_CORE` alone). Replace all call sites with the fixture. This requires converting the affected test methods to accept `memory_db` as a parameter instead of calling `_make_conn()` inline.

## Changed Files

- modify: `src/ccrecall/search_vector.py` — add logger, log in 5 except blocks
- modify: `src/ccrecall/embeddings.py` — add logger, log in model_available and is_model_cached_on_disk
- modify: `src/ccrecall/db.py` — add logger, log in vec_available
- modify: `src/ccrecall/session_tail.py` — add logger, log in _last_event_timestamp and _extract_branch
- modify: `tests/test_project_ops.py` — import FIXTURE_DIR from conftest
- modify: `tests/test_integration.py` — import FIXTURE_DIR from conftest
- modify: `tests/test_sync_hook.py` — import FIXTURE_DIR from conftest
- modify: `tests/test_import_pipeline.py` — import FIXTURE_DIR from conftest
- modify: `tests/test_session_ops.py` — import FIXTURE_DIR from conftest
- modify: `tests/test_context_alerts.py` — replace _make_conn with memory_db fixture
- modify: `tests/test_backfill_tool_content.py` — replace _make_conn with memory_db fixture
