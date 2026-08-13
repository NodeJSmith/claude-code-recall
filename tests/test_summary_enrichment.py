import subprocess
import sys

import pytest

from ccrecall.recap_input import ELIGIBILITY_POLICY_VERSION, RECAP_INPUT_CONTRACT_VERSION
from ccrecall.summary_enrichment import (
    CLAUDE_RESPONSE_SCHEMA,
    PRIMARY_RENDER_BUDGET,
    STATUS_OK,
    SUMMARY_ENRICHMENT_VERSION,
    SUMMARY_MAX_CHARS,
    SUPPLEMENTARY_RENDER_BUDGET,
    SummaryEnrichmentValidationError,
    build_stored_enrichment_envelope,
    render_enriched_context_summary,
    validate_claude_response_body,
)


def _envelope(**body: object) -> dict:
    return build_stored_enrichment_envelope(
        {"summary": "Implemented the recap renderer and validated the current cache.", **body},
        model="sonnet",
        generated_at="2026-08-12T12:34:56Z",
        attempt_id=42,
        recap_input_hash="input-hash",
    )


class TestRecapContract:
    def test_schema_requires_only_summary_and_permits_unknown_fields(self):
        assert CLAUDE_RESPONSE_SCHEMA["required"] == ["summary"]
        assert CLAUDE_RESPONSE_SCHEMA["additionalProperties"] is True
        assert set(CLAUDE_RESPONSE_SCHEMA["properties"]) == {"summary", "title", "outcome"}

    def test_normalizes_useful_summary_and_drops_defective_optional_fields(self):
        assert validate_claude_response_body(
            {"summary": "  A useful work arc.  ", "title": 3, "outcome": "not a state", "extra": "ignored"}
        ) == {"summary": "A useful work arc."}

    def test_bounds_display_fields_and_normalizes_outcome(self):
        recap = validate_claude_response_body(
            {"summary": "s" * (SUMMARY_MAX_CHARS + 20), "title": "t" * 200, "outcome": "PARTIAL"}
        )
        assert len(recap["summary"]) == SUMMARY_MAX_CHARS
        assert len(recap["title"]) == 160
        assert recap["outcome"] == "partial"

    @pytest.mark.parametrize("value", [None, "", "  ", [], "not json"])
    def test_rejects_only_unusable_summary_or_unparseable_body(self, value):
        body = value if value == "not json" else {"summary": value}
        with pytest.raises(SummaryEnrichmentValidationError):
            validate_claude_response_body(body)

    def test_worker_metadata_is_v2_and_complete(self):
        recap = _envelope(title="Recap contract", outcome="completed")
        assert recap == {
            "version": SUMMARY_ENRICHMENT_VERSION,
            "model": "sonnet",
            "generated_at": "2026-08-12T12:34:56Z",
            "attempt_id": 42,
            "recap_input_hash": "input-hash",
            "input_contract_version": RECAP_INPUT_CONTRACT_VERSION,
            "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
            "summary": "Implemented the recap renderer and validated the current cache.",
            "title": "Recap contract",
            "outcome": "completed",
        }


class TestRendering:
    def _render(self, recap: object, **overrides: object) -> str:
        values = {
            "status": STATUS_OK,
            "current_input_hash": "input-hash",
            "materialized_input_hash": "input-hash",
            "materialized_input_contract_version": RECAP_INPUT_CONTRACT_VERSION,
            "materialized_policy_version": ELIGIBILITY_POLICY_VERSION,
            "stored_enrichment_version": SUMMARY_ENRICHMENT_VERSION,
            **overrides,
        }
        return render_enriched_context_summary(
            "### Session: deterministic\n\nBase summary",
            recap,
            is_primary_session=True,
            **values,
        )

    def test_renders_current_recap_above_deterministic_context_within_budget(self):
        rendered = self._render(_envelope(title="Renderer", outcome="partial"))
        assert rendered.startswith("### Session Recap")
        assert "**Title:** Renderer" in rendered
        assert "**Outcome:** partial" in rendered
        assert rendered.endswith("### Session: deterministic\n\nBase summary")
        assert len(rendered.partition("\n\n### Session:")[0]) <= PRIMARY_RENDER_BUDGET

    @pytest.mark.parametrize(
        "overrides",
        [
            {"status": "unusable_output"},
            {"current_input_hash": "new-input"},
            {"materialized_input_contract_version": 0},
            {"materialized_policy_version": 0},
            {"stored_enrichment_version": 1},
        ],
    )
    def test_stale_or_failed_recaps_fall_back_deterministically(self, overrides):
        assert self._render(_envelope(), **overrides) == "### Session: deterministic\n\nBase summary"

    def test_malformed_and_v1_payloads_fall_back_without_interpretation(self):
        assert self._render({"version": 1, "summary": "legacy"}) == "### Session: deterministic\n\nBase summary"
        assert self._render({"version": 2, "summary": "usable"}) == "### Session: deterministic\n\nBase summary"

    def test_supplementary_recap_always_renders_a_bounded_summary(self):
        rendered = render_enriched_context_summary(
            "Deterministic supplementary context",
            _envelope(title="A title", summary="s" * SUMMARY_MAX_CHARS),
            is_primary_session=False,
            status=STATUS_OK,
            current_input_hash="input-hash",
            materialized_input_hash="input-hash",
            materialized_input_contract_version=RECAP_INPUT_CONTRACT_VERSION,
            materialized_policy_version=ELIGIBILITY_POLICY_VERSION,
            stored_enrichment_version=SUMMARY_ENRICHMENT_VERSION,
        )

        recap, _, _ = rendered.partition("\n\nDeterministic supplementary context")
        assert recap.startswith("### Session Recap")
        assert "s" in recap
        assert len(recap) <= SUPPLEMENTARY_RENDER_BUDGET


class TestImportIsolation:
    def test_summary_enrichment_import_is_dependency_light(self):
        code = (
            "import ccrecall.summary_enrichment\nimport sys\n"
            "for name in ('subprocess', 'sqlite_vec', 'fastembed', 'onnxruntime', 'ccrecall.db'):\n"
            "    assert name not in sys.modules, f'{name} should not be imported'\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
