# Context: First-Pass Architecture Cleanup

## Problem & Motivation

`ccrecall` has accumulated unrelated responsibilities in the same runtime and storage boundaries. Token analytics are active code even though they are not used, semantic/vector dependencies are mandatory even though keyword search can work without them, and `db.py` mixes paths, config, logging, SQLite setup, PID files, vector storage, and coverage reporting. The goal is to make the codebase easier to change without redesigning the user-facing recall/resume/search workflow. The base install should run hooks and FTS search without native semantic packages; installing the semantic extra should restore fused/vector behavior.

## Visual Artifacts

None.

## Key Decisions

1. Remove active token analytics entirely: delete the command/skill/source/tests, but do not drop existing token tables.
2. Make semantic support optional through a `semantic` extra and structural import boundaries, not lazy imports.
3. Split `db.py` into narrow runtime modules for paths, settings, database setup, runtime files, logging, and semantic vector storage.
4. Add canonical `exchanges` rows independent of semantic support; `chunks` and `chunk_vec` become derived embedding state.
5. Add `jobs` as a small DB-backed queue/status table and migrate only import first; do not add a daemon.
6. Preserve hook stdout and direct hook entry points.

## Constraints & Anti-Patterns

- Do not add imports inside functions to handle optional semantic dependencies.
- Do not make `sqlite-vec`, `fastembed`, or `numpy` mandatory for base install paths.
- Do not drop token analytics tables from user databases.
- Do not redesign the recall/resume/search UX.
- Do not add exchange-level FTS fallback for `search-messages`.
- Do not migrate `sync-current` or embedding backfill to DB jobs first.
- Do not let hooks print diagnostics to stdout.

## Design Doc References

## Problem — describes coupling across product responsibilities, native dependencies, and `db.py`.
## Goals — lists the cleanup outcomes and preservation requirements.
## Functional Requirements — defines FR#1 through FR#12 for base install, semantic install, token removal, exchanges, jobs, and hooks.
## Architecture — specifies runtime extraction, optional semantic boundary, canonical exchanges, data upgrade, jobs, and token removal.
## Migration — requires additive one-way DB changes and preservation of token tables/core conversation rows.
## Test Strategy — names existing tests to adapt, new coverage, and token tests to remove.
## Impact — names target files, invariants, and gap-check additions.

## Convention Examples

### Thin CLI Wrappers

**Source:** `src/ccrecall/cli/commands.py`

```python
@app.command(name="recent")
def cmd_recent(..., ctx: CLIContextParam = DEFAULT_CLI_CONTEXT) -> None:
    """List recent conversation sessions."""
    recent_chats_mod.run(..., output_format=ctx.output_format, ...)
```

New CLI commands should stay as thin cyclopts wrappers over module-level `run()` logic.

### Hook JSON Stdout Discipline

**Source:** `src/ccrecall/hooks/memory_sync.py`

```python
try:
    subprocess.Popen(["ccrecall", "sync-current", "--input-file", tmp_path], **kwargs)
except Exception:
    log_hook_exception("memory-sync")

print(json.dumps({"continue": True}))
```

Hook adapters must catch failures best-effort and print only the hook JSON envelope.

### Vec Fixture Isolation

**Source:** `tests/conftest.py`

```python
def make_vec_conn(db_path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _ensure_vec_schema(conn)
    conn.commit()
    return conn
```

Semantic tests should isolate vec setup and skip/degrade when semantic support is absent.

### Boundary Validation

**Source:** `src/ccrecall/models.py`

```python
def is_valid(model: type[BaseModel], data: object, label: str) -> bool:
    try:
        model.model_validate(data)
    except ValidationError as e:
        _LOG.info("Skipping malformed %s: %s", label, e)
        return False
    return True
```

External Claude Code JSON remains untrusted input and should be validated/skipped at boundaries.
