import copy
import sqlite3
import subprocess
import sys
import uuid

import pytest

from ccrecall.schema import SCHEMA
from ccrecall.summary_enrichment import (
    CLAUDE_RESPONSE_SCHEMA,
    PRIMARY_RENDER_BUDGET,
    STATUS_OK,
    SUMMARY_ENRICHMENT_VERSION,
    SummaryEnrichmentValidationError,
    build_stored_enrichment_envelope,
    compute_branch_summary_source_hash,
    compute_summary_source_hash,
    normalize_project_file_reference,
    render_enriched_context_summary,
    render_llm_block,
    valid_current_enrichment,
    validate_claude_response_body,
    validate_stored_enrichment_envelope,
)


def _uuid(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, value))


ACTIVE_UUIDS = {_uuid("u1"), _uuid("u2"), _uuid("u3"), _uuid("u4")}
VALID_FILE_PATHS = {"src/main.py", "tests/test_main.py", "docs/notes.md"}


def _valid_response_body() -> dict:
    uuids = sorted(ACTIVE_UUIDS)
    return {
        "title": {"text": "Resume the summary enrichment renderer", "source_uuids": [uuids[0]]},
        "where_we_left_off": {
            "text": "The branch now needs the lightweight renderer and source-hash contract wired in.",
            "source_uuids": [uuids[0], uuids[1]],
        },
        "how_we_got_here": {
            "text": "We split the enrichment boundary away from worker code so SessionStart can import only the validator and renderer.",
            "source_uuids": [uuids[1]],
        },
        "key_decisions": [
            {
                "decision": "Keep worker metadata out of Claude output",
                "rationale": "Model timestamps and model identifiers are worker-owned and must be recorded after validation.",
                "source_uuids": [uuids[1], uuids[2]],
            }
        ],
        "attempted_paths": [
            {
                "text": "Rely on raw deterministic markdown alone",
                "outcome": "abandoned",
                "why_stopped": "It loses the causal middle and continuation hints from long branches.",
                "source_uuids": [uuids[2]],
            }
        ],
        "open_questions": [
            {
                "text": "Whether search hydration should prefer LLM titles is deferred to later tasks.",
                "source_uuids": [uuids[2]],
            }
        ],
        "files_and_reasons": [
            {
                "path": "src/main.py",
                "reason": "The renderer contract lives with the summary composition helpers.",
                "source_uuids": [uuids[3]],
            }
        ],
        "continuation_hints": [
            {
                "text": "Hook the renderer into SessionStart after the lightweight contract is proven current-only.",
                "source_uuids": [uuids[0], uuids[3]],
            }
        ],
        "confidence": "high",
    }


def _valid_envelope() -> dict:
    return build_stored_enrichment_envelope(
        _valid_response_body(),
        model="sonnet",
        generated_at="2026-08-07T12:34:56Z",
        active_branch_uuids=ACTIVE_UUIDS,
        valid_file_paths=VALID_FILE_PATHS,
    )


class TestSchemaContract:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("src/main.py", "src/main.py"),
            (" README.md ", "README.md"),
            ("/tmp/absolute.py", None),
            ("./local.py", None),
            ("../escape.py", None),
            ("~/home.py", None),
            ("C:/worktree/file.py", None),
            ("src/../escape.py", None),
        ],
    )
    def test_normalize_project_file_reference(self, value, expected):
        assert normalize_project_file_reference(value) == expected

    def test_claude_response_schema_matches_top_level_contract(self):
        assert CLAUDE_RESPONSE_SCHEMA["type"] == "object"
        assert CLAUDE_RESPONSE_SCHEMA["additionalProperties"] is False
        assert set(CLAUDE_RESPONSE_SCHEMA["required"]) == {
            "title",
            "where_we_left_off",
            "how_we_got_here",
            "key_decisions",
            "attempted_paths",
            "open_questions",
            "files_and_reasons",
            "continuation_hints",
            "confidence",
        }
        assert "version" not in CLAUDE_RESPONSE_SCHEMA["properties"]
        assert "model" not in CLAUDE_RESPONSE_SCHEMA["properties"]
        assert "generated_at" not in CLAUDE_RESPONSE_SCHEMA["properties"]

    def test_validate_claude_response_body_accepts_valid_body(self):
        body = _valid_response_body()

        validated = validate_claude_response_body(
            body,
            active_branch_uuids=ACTIVE_UUIDS,
            valid_file_paths=VALID_FILE_PATHS,
        )

        assert validated == body

    @pytest.mark.parametrize(
        ("mutator", "message"),
        [
            (lambda body: body.__setitem__("version", 1), "worker metadata"),
            (lambda body: body.__setitem__("model", "sonnet"), "worker metadata"),
            (lambda body: body.__setitem__("generated_at", "2026-08-07T12:34:56Z"), "worker metadata"),
            (lambda body: body.__setitem__("extra", {}), "unknown field"),
            (lambda body: body["title"].__setitem__("text", "x" * 121), "120 characters"),
            (lambda body: body["title"].__setitem__("source_uuids", []), "non-empty"),
            (lambda body: body["title"].__setitem__("source_uuids", ["not-a-uuid"]), "uuid"),
            (lambda body: body["title"].__setitem__("source_uuids", [str(uuid.uuid4())]), "active branch"),
            (lambda body: body.__setitem__("confidence", "certain"), "confidence"),
            (lambda body: body["attempted_paths"][0].__setitem__("outcome", "failed-ish"), "outcome"),
            (lambda body: body["files_and_reasons"][0].__setitem__("path", "src/other.py"), "file reference"),
        ],
    )
    def test_validate_claude_response_body_rejects_invalid_body(self, mutator, message):
        body = copy.deepcopy(_valid_response_body())
        mutator(body)

        with pytest.raises(SummaryEnrichmentValidationError, match=message):
            validate_claude_response_body(
                body,
                active_branch_uuids=ACTIVE_UUIDS,
                valid_file_paths=VALID_FILE_PATHS,
            )

    def test_build_stored_enrichment_envelope_adds_worker_metadata(self):
        envelope = build_stored_enrichment_envelope(
            _valid_response_body(),
            model="sonnet",
            generated_at="2026-08-07T12:34:56Z",
            active_branch_uuids=ACTIVE_UUIDS,
            valid_file_paths=VALID_FILE_PATHS,
        )

        assert envelope["version"] == SUMMARY_ENRICHMENT_VERSION
        assert envelope["model"] == "sonnet"
        assert envelope["generated_at"] == "2026-08-07T12:34:56Z"
        assert envelope["title"]["text"] == _valid_response_body()["title"]["text"]

    @pytest.mark.parametrize("unsafe_path", ["/tmp/absolute.py", "./local.py", "../escape.py", "~/home.py"])
    def test_build_stored_enrichment_envelope_rejects_unsafe_file_references(self, unsafe_path):
        body = copy.deepcopy(_valid_response_body())
        body["files_and_reasons"][0]["path"] = unsafe_path

        with pytest.raises(SummaryEnrichmentValidationError, match="invalid file reference"):
            build_stored_enrichment_envelope(
                body,
                model="sonnet",
                generated_at="2026-08-07T12:34:56Z",
                active_branch_uuids=ACTIVE_UUIDS,
                valid_file_paths={unsafe_path},
            )

    def test_validate_stored_enrichment_envelope_rejects_persisted_unsafe_file_references_without_membership_context(
        self,
    ):
        envelope = _valid_envelope()
        envelope["files_and_reasons"][0]["path"] = "/tmp/absolute.py"

        with pytest.raises(SummaryEnrichmentValidationError, match="invalid file reference"):
            validate_stored_enrichment_envelope(envelope)


class TestSummarySourceHash:
    def test_compute_summary_source_hash_normalizes_json_preserves_list_order_and_ignores_provenance(self):
        summary_inputs = {
            "leaf_uuid": _uuid("leaf"),
            "summary_version": 6,
            "context_summary_json": '{"metadata":{"tool_counts":{"Read":2,"Edit":1}},"topic":"Resume"}',
            "aggregated_content": "alpha beta gamma",
            "exchange_count": 12,
            "started_at": "2026-08-07T10:00:00Z",
            "ended_at": "2026-08-07T11:00:00Z",
            "files_modified": '["src/main.py","tests/test_main.py"]',
            "tool_counts": '{"Read":2,"Edit":1}',
            "commits": '["feat: add renderer"]',
            "git_branch": "main",
            "source_transcript_paths": ["/tmp/a.jsonl"],
        }
        reordered_json = {
            **summary_inputs,
            "context_summary_json": '{"topic":"Resume","metadata":{"tool_counts":{"Edit":1,"Read":2}}}',
            "tool_counts": '{"Edit":1,"Read":2}',
            "source_transcript_paths": ["/tmp/b.jsonl"],
        }
        reordered_list = {**summary_inputs, "files_modified": '["tests/test_main.py","src/main.py"]'}

        first_hash = compute_summary_source_hash(summary_inputs)
        second_hash = compute_summary_source_hash(reordered_json)
        third_hash = compute_summary_source_hash(reordered_list)

        assert first_hash == second_hash
        assert first_hash != third_hash

    def test_compute_summary_source_hash_preserves_string_literals_in_non_json_fields(self):
        summary_inputs = {
            "leaf_uuid": _uuid("leaf"),
            "summary_version": 6,
            "context_summary_json": '{"topic":"Resume"}',
            "aggregated_content": "alpha beta gamma",
            "exchange_count": 12,
            "started_at": "2026-08-07T10:00:00Z",
            "ended_at": "2026-08-07T11:00:00Z",
            "files_modified": '["src/main.py"]',
            "tool_counts": '{"Read":2}',
            "commits": '["feat: add renderer"]',
            "git_branch": "null",
        }

        assert compute_summary_source_hash(summary_inputs) != compute_summary_source_hash(
            {**summary_inputs, "git_branch": None}
        )
        assert compute_summary_source_hash({**summary_inputs, "git_branch": "true"}) != compute_summary_source_hash(
            {**summary_inputs, "git_branch": True}
        )
        assert compute_summary_source_hash({**summary_inputs, "git_branch": "123"}) != compute_summary_source_hash(
            {**summary_inputs, "git_branch": 123}
        )

    def test_compute_branch_summary_source_hash_ignores_inactive_branch(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO projects (path, key, name) VALUES (?, ?, ?)", ("/test/proj", "proj", "proj"))
        cursor.execute("INSERT INTO sessions (uuid, project_id, git_branch) VALUES (?, ?, ?)", ("sess-1", 1, "main"))
        cursor.execute(
            """
            INSERT INTO branches (
                session_id, leaf_uuid, is_active, summary_version, context_summary_json,
                aggregated_content, exchange_count, started_at, ended_at, files_modified,
                tool_counts, commits
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                _uuid("leaf"),
                6,
                '{"version":6,"topic":"Resume"}',
                "alpha beta gamma",
                12,
                "2026-08-07T10:00:00Z",
                "2026-08-07T11:00:00Z",
                '["src/main.py"]',
                '{"Read":2}',
                '["feat: add renderer"]',
            ),
        )

        assert compute_branch_summary_source_hash(cursor, 1) is None
        conn.close()


class TestRendering:
    def test_valid_current_enrichment_requires_current_ok_status_and_hash_match(self):
        envelope = _valid_envelope()

        assert valid_current_enrichment(
            envelope,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )
        assert not valid_current_enrichment(
            envelope,
            status="invalid_output",
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )
        assert not valid_current_enrichment(
            envelope,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="def",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )
        assert not valid_current_enrichment(
            envelope,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION - 1,
        )

    def test_render_enriched_context_summary_missing_enrichment_falls_back_to_deterministic_markdown(self):
        base_markdown = "### Session: deterministic\n\nBase summary"

        rendered = render_enriched_context_summary(
            base_markdown,
            None,
            is_primary_session=True,
            status=None,
            stored_source_hash=None,
            current_source_hash="abc",
            stored_enrichment_version=None,
        )

        assert rendered == base_markdown

    def test_render_enriched_context_summary_falls_back_to_deterministic_markdown(self):
        base_markdown = "### Session: deterministic\n\nBase summary"

        rendered = render_enriched_context_summary(
            base_markdown,
            {"title": {"text": "bad", "source_uuids": []}},
            is_primary_session=True,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )

        assert rendered == base_markdown

    @pytest.mark.parametrize(
        ("mutator"),
        [
            lambda envelope: envelope["title"].__setitem__("source_uuids", None),
            lambda envelope: envelope.__setitem__("files_and_reasons", 5),
        ],
    )
    def test_render_enriched_context_summary_malformed_stored_envelope_falls_back(self, mutator):
        base_markdown = "### Session: deterministic\n\nBase summary"
        envelope = _valid_envelope()
        mutator(envelope)

        rendered = render_enriched_context_summary(
            base_markdown,
            envelope,
            is_primary_session=True,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )

        assert rendered == base_markdown

    @pytest.mark.parametrize(
        ("status", "stored_source_hash", "current_source_hash", "stored_enrichment_version"),
        [
            ("invalid_output", "abc", "abc", SUMMARY_ENRICHMENT_VERSION),
            (STATUS_OK, "abc", "def", SUMMARY_ENRICHMENT_VERSION),
            (STATUS_OK, "abc", "abc", SUMMARY_ENRICHMENT_VERSION - 1),
        ],
    )
    def test_render_enriched_context_summary_returns_deterministic_markdown_for_stale_or_failed_enrichment(
        self,
        status,
        stored_source_hash,
        current_source_hash,
        stored_enrichment_version,
    ):
        base_markdown = "### Session: deterministic\n\nBase summary"

        rendered = render_enriched_context_summary(
            base_markdown,
            _valid_envelope(),
            is_primary_session=True,
            status=status,
            stored_source_hash=stored_source_hash,
            current_source_hash=current_source_hash,
            stored_enrichment_version=stored_enrichment_version,
        )

        assert rendered == base_markdown

    def test_render_enriched_context_summary_renders_evidenced_sections_when_they_fit(self):
        base_markdown = "### Session: deterministic\n\nBase summary"

        rendered = render_enriched_context_summary(
            base_markdown,
            _valid_envelope(),
            is_primary_session=True,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )

        assert "**Title:** Resume the summary enrichment renderer" in rendered
        assert "**Where we left off:**" in rendered
        assert "**How we got here:**" in rendered
        assert "**Key decisions:**" in rendered
        assert "**Attempted paths:**" in rendered
        assert "**Open questions:**" in rendered
        assert "**Files touched:**" in rendered
        assert "**Continuation hints:**" in rendered
        assert rendered.endswith(base_markdown)

    def test_render_enriched_context_summary_primary_budget_prioritizes_continuation_hints(self):
        base_markdown = "### Session: deterministic\n\nBase summary"
        envelope = _valid_envelope()
        envelope["where_we_left_off"]["text"] = "L" * 600
        envelope["how_we_got_here"]["text"] = "H" * 600
        envelope["key_decisions"] = [
            {
                "decision": "D" * 180,
                "rationale": "R" * 240,
                "source_uuids": [sorted(ACTIVE_UUIDS)[0]],
            }
        ]
        envelope["attempted_paths"] = [
            {
                "text": "P" * 180,
                "outcome": "failed",
                "why_stopped": "W" * 180,
                "source_uuids": [sorted(ACTIVE_UUIDS)[1]],
            }
        ]
        envelope["open_questions"] = [{"text": "Q" * 180, "source_uuids": [sorted(ACTIVE_UUIDS)[2]]}]
        envelope["files_and_reasons"] = [
            {
                "path": "src/main.py",
                "reason": "F" * 180,
                "source_uuids": [sorted(ACTIVE_UUIDS)[3]],
            }
        ]
        envelope["continuation_hints"] = [{"text": "C" * 180, "source_uuids": [sorted(ACTIVE_UUIDS)[0]]}]

        rendered = render_enriched_context_summary(
            base_markdown,
            envelope,
            is_primary_session=True,
            status=STATUS_OK,
            stored_source_hash="abc",
            current_source_hash="abc",
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )

        assert rendered.startswith("### Branch Resume Brief")
        assert "**Continuation hints:**" in rendered
        assert "**How we got here:**" in rendered
        assert "**Open questions:**" not in rendered
        assert "### Session: deterministic" in rendered
        assert sorted(ACTIVE_UUIDS)[0] not in rendered
        llm_block, _, suffix = rendered.partition("\n\n" + base_markdown)
        assert suffix == ""
        assert len(llm_block) <= PRIMARY_RENDER_BUDGET

    def test_render_llm_block_primary_stops_after_over_budget_continuation_hints(self):
        envelope = _valid_envelope()
        envelope["continuation_hints"] = [{"text": "C" * 180, "source_uuids": [sorted(ACTIVE_UUIDS)[0]]}]
        envelope["how_we_got_here"]["text"] = "Short causal history"
        envelope["key_decisions"] = []
        envelope["attempted_paths"] = []
        envelope["open_questions"] = []
        envelope["files_and_reasons"] = []

        budget = len(
            render_llm_block(
                {**copy.deepcopy(envelope), "continuation_hints": []},
                char_budget=PRIMARY_RENDER_BUDGET,
                is_primary_session=True,
            )
        )

        rendered = render_llm_block(envelope, char_budget=budget, is_primary_session=True)

        assert "**Continuation hints:**" not in rendered
        assert "**How we got here:**" not in rendered

    def test_render_llm_block_primary_stops_after_over_budget_key_decisions(self):
        envelope = _valid_envelope()
        envelope["key_decisions"] = [
            {
                "decision": "D" * 180,
                "rationale": "R" * 240,
                "source_uuids": [sorted(ACTIVE_UUIDS)[0]],
            }
            for _ in range(4)
        ]
        envelope["attempted_paths"] = [
            {
                "text": "small path",
                "outcome": "failed",
                "why_stopped": "small why",
                "source_uuids": [sorted(ACTIVE_UUIDS)[1]],
            }
        ]
        envelope["open_questions"] = [{"text": "small question", "source_uuids": [sorted(ACTIVE_UUIDS)[2]]}]
        envelope["files_and_reasons"] = [
            {
                "path": "src/main.py",
                "reason": "small reason",
                "source_uuids": [sorted(ACTIVE_UUIDS)[3]],
            }
        ]

        budget = len(
            render_llm_block(
                {**copy.deepcopy(envelope), "key_decisions": []},
                char_budget=PRIMARY_RENDER_BUDGET,
                is_primary_session=True,
            )
        )

        rendered = render_llm_block(envelope, char_budget=budget, is_primary_session=True)

        assert "**Key decisions:**" not in rendered
        assert "**Attempted paths:**" not in rendered
        assert "**Open questions:**" not in rendered
        assert "**Files touched:**" not in rendered

    def test_render_llm_block_supplementary_budget_keeps_title_latest_state_and_one_hint(self):
        envelope = _valid_envelope()
        envelope["where_we_left_off"]["text"] = "L" * 600
        envelope["continuation_hints"] = [
            {"text": "first hint", "source_uuids": [sorted(ACTIVE_UUIDS)[0]]},
            {"text": "second hint", "source_uuids": [sorted(ACTIVE_UUIDS)[1]]},
        ]
        envelope["open_questions"] = [{"text": "question one", "source_uuids": [sorted(ACTIVE_UUIDS)[2]]}]

        rendered = render_llm_block(envelope, char_budget=800, is_primary_session=False)

        assert rendered.startswith("### Branch Resume Brief")
        assert "**Title:**" in rendered
        assert "**Where we left off:**" in rendered
        assert rendered.count("- ") == 1
        assert "second hint" not in rendered
        assert "**How we got here:**" not in rendered
        assert len(rendered) <= 800


class TestStrictSchemaFailures:
    @pytest.mark.parametrize("missing_field", ["title", "where_we_left_off", "confidence"])
    def test_validate_claude_response_body_rejects_missing_required_fields(self, missing_field):
        body = copy.deepcopy(_valid_response_body())
        del body[missing_field]

        with pytest.raises(SummaryEnrichmentValidationError, match="missing required field"):
            validate_claude_response_body(
                body,
                active_branch_uuids=ACTIVE_UUIDS,
                valid_file_paths=VALID_FILE_PATHS,
            )

    @pytest.mark.parametrize(
        ("mutator", "message"),
        [
            (lambda body: body.__setitem__("key_decisions", "not-a-list"), "must be a list"),
            (lambda body: body["title"].__setitem__("source_uuids", "not-a-list"), "non-empty source_uuids list"),
            (lambda body: body.__setitem__("confidence", 1), "non-empty string"),
        ],
    )
    def test_validate_claude_response_body_rejects_wrong_field_types(self, mutator, message):
        body = copy.deepcopy(_valid_response_body())
        mutator(body)

        with pytest.raises(SummaryEnrichmentValidationError, match=message):
            validate_claude_response_body(
                body,
                active_branch_uuids=ACTIVE_UUIDS,
                valid_file_paths=VALID_FILE_PATHS,
            )

    @pytest.mark.parametrize(
        ("mutator", "message"),
        [
            (lambda body: body["title"].__setitem__("extra", "nope"), "unknown field"),
            (lambda body: body["key_decisions"][0].__setitem__("extra", "nope"), "unknown field"),
            (lambda body: body["files_and_reasons"][0].__setitem__("extra", "nope"), "unknown field"),
        ],
    )
    def test_validate_claude_response_body_rejects_nested_unknown_fields(self, mutator, message):
        body = copy.deepcopy(_valid_response_body())
        mutator(body)

        with pytest.raises(SummaryEnrichmentValidationError, match=message):
            validate_claude_response_body(
                body,
                active_branch_uuids=ACTIVE_UUIDS,
                valid_file_paths=VALID_FILE_PATHS,
            )


class TestImportIsolation:
    def test_summary_enrichment_import_is_dependency_light(self):
        code = (
            "import ccrecall.summary_enrichment\n"
            "import sys\n"
            "for name in ('subprocess', 'sqlite_vec', 'fastembed', 'onnxruntime', 'ccrecall.db'):\n"
            "    assert name not in sys.modules, f'{name} should not be imported'\n"
            "loaded_hooks = [name for name in sys.modules if name.startswith('ccrecall.hooks')]\n"
            "assert not loaded_hooks, f'hook modules imported: {loaded_hooks}'\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)

        assert result.returncode == 0, result.stderr
