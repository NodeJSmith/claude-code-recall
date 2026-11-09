"""Help text uses plain wording, not internal implementation terms.

Walks the root app and every subcommand, renders its ``--help``, and checks
that none of the terms that only make sense to someone who has read the code
appear in it.
"""

import io

import pytest
from cyclopts import App
from rich.console import Console

from ccrecall.cli import app

BANNED_TERMS = (
    "entrypoint",
    "chunk-knn",
    "knn",
    "active-leaf",
    "active leaf",
    "stale tail",
    "pending tail",
    "watermark",
    "draft-quality",
    "vec extension",
)


def command_paths(cli: App, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths = [prefix]
    for name in cli:
        if name.startswith("-"):
            continue
        paths.extend(command_paths(cli[name], (*prefix, name)))
    return paths


def render_help(path: tuple[str, ...]) -> str:
    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc:
        app([*path, "--help"], console=Console(file=buf, width=300), exit_on_error=False)
    assert exc.value.code == 0
    return buf.getvalue()


@pytest.mark.parametrize("path", command_paths(app), ids=lambda p: " ".join(p) or "root")
def test_help_has_no_internal_terms(path: tuple[str, ...]) -> None:
    text = render_help(path).lower()
    found = [term for term in BANNED_TERMS if term in text]
    assert not found, f"`ccrecall {' '.join(path)} --help` uses internal terms: {found}"


def test_walk_covers_nested_commands() -> None:
    paths = command_paths(app)
    assert ("search-messages",) in paths
    assert ("backfill", "embeddings") in paths
