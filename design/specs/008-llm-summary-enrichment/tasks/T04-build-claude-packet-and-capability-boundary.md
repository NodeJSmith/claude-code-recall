---
task_id: "T04"
title: "Build Claude packet and capability boundary"
status: "planned"
depends_on: ["T02"]
implements: ["FR#7", "FR#8", "FR#13", "FR#14", "FR#15", "FR#16", "FR#18", "AC#4"]
---

## Summary

Create the background-only Claude integration boundary: validated source resolution, secure temporary branch packets, deterministic outlines, exact safe-mode invocation, stdout parsing, capability checks, and failure classification.
It must turn full branch evidence into a constrained Read-only task without logging it.

## Target Files

- create: `src/ccrecall/llm_summarizer.py`
- create: `tests/test_llm_summarizer.py`
- read: `src/ccrecall/parsing.py`
- read: `src/ccrecall/content.py`
- read: `src/ccrecall/hooks/sync_current.py`
- read: `src/ccrecall/import_log_ops.py`
- read: `src/ccrecall/config.py`

## Prompt

Implement the `Claude input packet`, `Claude invocation`, capability-sidecar, prompt, and source-validation sections of the design. Build only branch-linked source entries from validated regular, non-symlink transcript files; handle direct and subagent paths, historical import-log candidates, duplicate UUIDs, stat/hash mismatches, and complete UUID coverage exactly as specified. Normalize enough evidence to retain user requests, assistant prose, tool invocations, and tool result/error evidence. Write owner-only `branch-transcript.jsonl`, `branch-outline.json`, metadata, deterministic summary, and allowlist files under a dedicated owner-only temp parent; reap stale packets and always clean current packets.

Use `subprocess.run()` with an argv list, safe mode, Read-only tools, empty strict MCP config, isolated cwd, `--add-dir` only for the packet, `CLAUDE_RESPONSE_SCHEMA`, configured model/budget/effort, timeout, and `--no-session-persistence`. Never use a shell or log prompt/stdout/raw content. Build and parse the canonical factual prompt; classify expected process failures conservatively.

Implement the synthetic capability check and sidecar read/write contract. Its fingerprint covers only security/persistence flags; a missing sidecar, CLI-version mismatch, or fingerprint mismatch is `capability_unverified`. Keep capability-check `budget_exceeded` distinct from a full branch run's `budget_exceeded` status.

## Focus

`sync_current.get_session_file()` is a source-location hint, not enough for historical fidelity. `parsing.parse_all_with_uuids()` provides raw UUID-bearing entries, while `content.extract_text_content()` demonstrates the normalized message/tool split. Source resolving must never substitute DB rows for source proof. The real trial showed full packets can exceed the configured budget threshold before Claude stops, so pass the configured value unchanged and treat a process budget error as status, not as a hard accounting guarantee.

## Verify

- [ ] FR#7: Mocked invocation asserts an argv list and rejects shell execution.
- [ ] FR#8: Mocked argv includes safe mode, strict empty MCP, Read-only tools, `dontAsk`, and structured response schema only.
- [ ] FR#8: Synthetic capability tests persist the security/persistence flag fingerprint and reject a missing, CLI-version-stale, or fingerprint-mismatched sidecar before real packet invocation.
- [ ] FR#8: Synthetic capability tests snapshot the Claude projects directory before and after `--no-session-persistence` and fail when a newly created direct, subagent, or parser-resolvable JSONL transcript is importable.
- [ ] FR#8: Capability-sidecar tests prove model, effort, and budget changes do not invalidate the security/persistence fingerprint, while a capability-check `budget_exceeded` recovers only through a successful rerun of `--check-capability`.
- [ ] FR#13: Tests prove Claude receives a temporary packet directory and isolated cwd, never original transcript directories.
- [ ] FR#13: Source resolver tests reject non-regular and symlink paths, discover direct and subagent transcript paths, and classify import-log stat/hash mismatches as `source_changed` or `source_unverified`.
- [ ] FR#13: Packet lifecycle tests set owner-only modes, reap stale PID-dead packet directories, and remove current packets on success and failure without logging contents.
- [ ] FR#14: Packets contain only active-branch UUIDs and reject missing/duplicate-conflicting source evidence.
- [ ] FR#14: Multi-source tests deduplicate identical UUID entries, reject conflicting normalized duplicates, and require complete active-branch UUID coverage before writing a packet.
- [ ] FR#15: Packet outline and prompt direct the model to latest state, causal history, rationale, failures, and evidence-backed next steps.
- [ ] FR#15: Transcript-projection tests retain user requests, assistant prose, tool invocations, and available tool result/error evidence in the normalized packet.
- [ ] FR#16: Prompt and post-parse validation require allowlisted UUID provenance for every factual field.
- [ ] FR#18: Invocation tests pass configured model and budget unchanged and document threshold semantics through diagnostics.
- [ ] AC#4: Tests cover success, timeout, missing binary, unsupported flags, nonzero exit, malformed stdout, and capped diagnostics without raw-content logging.
