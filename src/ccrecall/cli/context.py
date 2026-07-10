"""CLI context object — frozen dataclass carrying per-invocation global options."""

from dataclasses import dataclass
from typing import Annotated

from cyclopts import Parameter


@dataclass(frozen=True)
class CLIContext:
    """Immutable global options for a single CLI invocation.

    Built by the meta launcher from parsed global flags and injected into every
    command that declares a ``ctx`` parameter via ``bound.arguments["ctx"]``.
    Commands map it onto their owning ``run(...)`` kwargs, so ``run`` stays the
    stable callable API while the CLI keeps one global ``--json`` surface.
    """

    json_mode: bool = False
    debug: bool = False

    @property
    def output_format(self) -> str:
        """The ``output_format`` string the markdown/JSON-aware ``run()`` funcs expect."""
        return "json" if self.json_mode else "markdown"


# parse=False: the launcher injects this, cyclopts never fills it from tokens,
# and it stays out of every command's --help.
CLIContextParam = Annotated[CLIContext, Parameter(parse=False)]

DEFAULT_CLI_CONTEXT = CLIContext()
