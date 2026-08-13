# Session Recap Evaluation

## Scope

This is aggregate evidence from a private local evaluation. The underlying
review materials remain outside the repository. It evaluates recognition,
work-arc, and outcome usefulness, not continuation correctness or task
completion.

## Method

The evaluation compared recap input assembled from normalized imported SQLite
records with input assembled from the corresponding local JSONL source representation.
The same bounded recap contract was used for both input forms. Reviewers scored
whether each successful recap made the session recognizable, captured its work
arc, and represented its evidenced outcome. Model completion and the three
quality dimensions were recorded as aggregate counts only.

## Aggregate input results

| Input authority | Completed recaps | Recognition | Work arc | Outcome | Relative packet size |
| --- | ---: | ---: | ---: | ---: | --- |
| Imported SQLite | 6/6 | 6/6 | 6/6 | 6/6 | Smaller |
| JSONL source representation | 6/6 | 6/6 | 6/6 | 6/6 | Larger |

The successful input forms were equivalent on the evaluated recognition,
work-arc, and outcome dimensions. The imported SQLite projection was smaller
and removes a second parse and readiness boundary.

## Aggregate model results

| Model | Completed DB-input calls | Recognition | Work arc | Outcome |
| --- | ---: | ---: | ---: | ---: |
| Sonnet | 6/6 | 6/6 | 6/6 | 6/6 |
| Haiku | 4/6 | 4/4 | 4/4 | 4/4 |

The sample is directional rather than a provider benchmark. Sonnet had the
stronger observed completion reliability; completed outputs from both models
met the reviewed quality dimensions.

## Decision

Session Recaps use normalized imported SQLite records as their sole canonical
input authority. Sonnet is the default model, while per-run controls retain a
user choice of model, budget, and timeout. A 120-second provisional timeout
leaves cleanup margin above the longest successful DB-plus-Sonnet completion,
which was below 90 seconds in this evaluation.

The provider budget is an upstream stop threshold, not a guaranteed maximum
charge. A provider can cross the threshold before stopping.

## Privacy boundary

No transcripts, recap text, prompts, raw outputs, identifiers, local paths,
per-sample mappings, or reviewer notes are committed with this evidence.
