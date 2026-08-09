# LLM Summary Enrichment Evaluation

Run this manual gate before you accept prompt, schema, packet-shape, or renderer changes for Branch Resume Briefs.

## What you verify

For each synthetic fixture, you verify both outputs:

- the **rendered brief** that SessionStart would show
- the **stored enrichment envelope** that keeps source UUID citations

You compare both outputs against `tests/fixtures/llm_summary_evaluation/manifest.json`.

For every applicable gold fact, record four verdicts:

- **Coverage** — the output includes the fact
- **Unsupported claim** — the output adds no extra factual claim that the manifest does not support
- **UUID membership** — every cited UUID belongs to the active branch
- **Citation entailment** — the cited UUIDs actually support the claim they accompany

Do not paste transcript text, raw model output, local paths, credentials, or packet contents into the repository.

## Prerequisites

- `claude` is installed locally and already authenticated by the evaluator
- you can run `uv run ccrecall ...` from this repo
- you are willing to run a local, non-CI provider check

## Step 1: Create an isolated evaluation workspace

After this step, you'll have a scratch home directory and a Claude-projects tree built from the synthetic fixtures.

```bash
export CCR_EVAL_ROOT="$(mktemp -d)"
export CCR_EVAL_HOME="$CCR_EVAL_ROOT/home"
mkdir -p "$CCR_EVAL_HOME"
export HOME="$CCR_EVAL_HOME"
export CCR_EVAL_PROJECTS="$CCR_EVAL_ROOT/projects"
mkdir -p "$CCR_EVAL_PROJECTS"
```

Expected outcome: the commands print nothing, and `~/.ccrecall/` will resolve inside the scratch home.

If your Claude CLI auth does not follow `HOME`, point `CLAUDE_CONFIG_DIR` at your existing local Claude configuration before the next step.

## Step 2: Materialize importable synthetic transcripts

After this step, each scenario fixture exists under its own synthetic Claude project directory with a filename that contains its session UUID. The synthetic directory name stays aligned with later DB lookup and review-bundle filenames, while the manifest scenario id remains the review label.

```bash
uv run python - <<'PY'
import json
from pathlib import Path

repo = Path.cwd()
fixtures = repo / "tests/fixtures/llm_summary_evaluation"
manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
projects = Path.home().parent / "projects"
projects.mkdir(parents=True, exist_ok=True)

for scenario in manifest["scenarios"]:
    fixture = fixtures / scenario["fixture"]
    lines = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines() if line.strip()]
    session_id = next(row["sessionId"] for row in lines if "sessionId" in row)
    project_key = fixture.stem
    project_dir = projects / project_key
    project_dir.mkdir(parents=True, exist_ok=True)
    target = project_dir / f"{session_id}.jsonl"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"prepared {scenario['id']} -> {project_key}: {target.name}")
PY
```

Expected outcome: one `prepared ...` line per scenario.

## Step 3: Import, summarize, and capability-check

After this step, the scratch DB contains the three scenarios, deterministic summaries exist, and the no-session-persistence gate is recorded for this local Claude CLI.

```bash
uv run ccrecall import --projects-dir "$CCR_EVAL_PROJECTS"
uv run ccrecall backfill summaries
uv run ccrecall backfill llm-summaries --check-capability
```

Expected outcome:

- import completes without printing transcript content
- summary backfill completes without errors
- capability check reports pass/fail and updates the local sidecar

If the capability check fails, stop here. Fix the reported Claude CLI/auth issue first. Do not bypass the gate.

## Step 4: Run the completed worker on all three fixtures

After this step, each eligible branch has a fresh enrichment attempt in the scratch DB.

```bash
uv run ccrecall backfill llm-summaries --limit 3 --force
```

Expected outcome: progress/completion output only. No transcript text or raw Claude responses are printed.

## Step 5: Export a private review bundle

After this step, you'll have local-only markdown and JSON files to inspect while filling in `evaluation-results.md`. The DB lookup uses the same synthetic project directory stem created in Step 2, while the output filenames stay keyed by manifest scenario id.

```bash
uv run python - <<'PY'
import json
import sqlite3
from pathlib import Path

from ccrecall.summary_enrichment import render_enriched_context_summary

repo = Path.cwd()
manifest = json.loads((repo / "tests/fixtures/llm_summary_evaluation/manifest.json").read_text(encoding="utf-8"))
review_dir = Path.home().parent / "review-bundle"
review_dir.mkdir(parents=True, exist_ok=True)
db_path = Path.home() / ".ccrecall/conversations.db"

with sqlite3.connect(db_path) as conn:
    conn.row_factory = sqlite3.Row
    for scenario in manifest["scenarios"]:
        project_key = Path(scenario["fixture"]).stem
        row = conn.execute(
            """
             SELECT b.context_summary,
                    b.summary_enrichment_json,
                    b.summary_enrichment_status,
                   b.summary_enrichment_source_hash,
                   b.summary_source_hash,
                   b.summary_enrichment_version
             FROM branches b
             JOIN sessions s ON s.id = b.session_id
             JOIN projects p ON p.id = s.project_id
             WHERE p.name = ? AND b.is_active = 1
             """,
            (project_key,),
        ).fetchone()
        if row is None:
            raise SystemExit(f"missing imported branch for {scenario['id']} ({project_key})")

        enrichment = json.loads(row["summary_enrichment_json"]) if row["summary_enrichment_json"] else None
        rendered = render_enriched_context_summary(
            row["context_summary"] or "",
            enrichment,
            is_primary_session=True,
            status=row["summary_enrichment_status"],
            stored_source_hash=row["summary_enrichment_source_hash"],
            current_source_hash=row["summary_source_hash"],
            stored_enrichment_version=row["summary_enrichment_version"],
        )
        (review_dir / f"{scenario['id']}.rendered.md").write_text(rendered, encoding="utf-8")
        (review_dir / f"{scenario['id']}.stored.json").write_text(
            json.dumps(enrichment, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"wrote {scenario['id']} review files")
PY
```

Expected outcome: one `wrote ... review files` line per scenario.

Do not commit the review bundle.

## Step 6: Compare each output against the manifest

Open these local-only files side by side:

- `tests/fixtures/llm_summary_evaluation/manifest.json`
- `$CCR_EVAL_ROOT/review-bundle/<scenario>.rendered.md`
- `$CCR_EVAL_ROOT/review-bundle/<scenario>.stored.json`

For each fact in each scenario:

1. check whether the rendered brief surfaces it clearly enough to resume work
2. check whether the stored JSON keeps the claim with valid `source_uuids`
3. confirm every cited UUID is listed in the scenario's manifest fact
4. confirm the cited messages actually entail the claim instead of merely belonging to the branch
5. note any extra factual claim that the manifest does not support

## Step 7: Update `evaluation-results.md`

Record only de-identified verdicts and remediation notes:

- pass/fail per fact category
- whether unsupported claims appeared
- whether UUID membership passed
- whether citation entailment passed
- whether the rendered brief stayed useful and bounded
- any follow-up needed before accepting the change

Do not paste transcript text, raw model output, session UUIDs, or local file paths.

## Optional Step 8: Refresh the Sonnet-vs.-Haiku motivation note

Run this step only when you want to refresh the de-identified model-choice note in `evaluation-results.md`. This is design motivation only, not the release gate.

After this step, you'll have a second local-only review bundle for the same synthetic corpus using `haiku` instead of `sonnet`.

```bash
uv run python - <<'PY'
import json
from pathlib import Path

config_path = Path.home() / ".ccrecall/config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config["llm_summary_model"] = "haiku"
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
print("set llm_summary_model=haiku")
PY

uv run ccrecall backfill llm-summaries --limit 3 --force
```

Expected outcome: the command rewrites the scratch DB's enrichment rows using `haiku` and still prints no transcript text or raw Claude responses.

Re-export the private review bundle from Step 5 before you compare models. Record only the de-identified takeaway in `evaluation-results.md` — for example, whether one model retained decision/rationale or handoff specificity better on a given synthetic scenario. Do not turn this into a benchmark table, and do not commit the review bundle.
