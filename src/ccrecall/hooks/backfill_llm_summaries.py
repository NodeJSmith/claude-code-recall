"""Compatibility entry point superseded by the durable recap drainer."""

import argparse
import logging

from ccrecall.config import remove_pid_file, try_acquire_pid_file

PID_KEY = "ccrecall-backfill-llm-summaries"
EXIT_OK = 0


def run(
    *,
    days: int | None = None,
    limit: int | None = None,
    session: str | None = None,
    current_session: bool = False,
    force: bool = False,
    verbose: bool = False,
) -> int:
    """Keep the public command inert until T07 wires the durable drainer."""
    del days, limit, session, current_session, force, verbose
    if not try_acquire_pid_file(PID_KEY):
        return EXIT_OK
    try:
        logging.getLogger(__name__).info("LLM summary compatibility worker awaits the recap drainer")
        return EXIT_OK
    finally:
        remove_pid_file(PID_KEY)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccrecall-llm-summaries")
    parser.add_argument("--days", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--session")
    parser.add_argument("--current-session", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.days is not None and args.days < 1:
        parser.error("--days must be >= 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.current_session and args.session is None:
        parser.error("--current-session requires --session")
    return run(
        days=args.days,
        limit=args.limit,
        session=args.session,
        current_session=args.current_session,
        force=args.force,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
