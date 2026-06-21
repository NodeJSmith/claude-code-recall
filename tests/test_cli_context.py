"""CLIContext — the global-options object the meta launcher injects into commands."""

import dataclasses

import pytest

from ccrecall.cli.context import DEFAULT_CLI_CONTEXT, CLIContext


def test_output_format_maps_json_mode():
    assert CLIContext(json_mode=True).output_format == "json"
    assert CLIContext(json_mode=False).output_format == "markdown"


def test_default_context_is_markdown():
    assert DEFAULT_CLI_CONTEXT.json_mode is False
    assert DEFAULT_CLI_CONTEXT.output_format == "markdown"


def test_context_is_frozen():
    ctx = CLIContext()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.json_mode = True
