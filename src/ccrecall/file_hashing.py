"""Shared transcript file hashing helpers."""

import hashlib
from pathlib import Path

HASH_CHUNK_SIZE = 8192


def transcript_file_hash(filepath: Path) -> str:
    """Return the import-log content hash for one transcript file."""
    hasher = hashlib.md5(usedforsecurity=False)
    with open(filepath, "rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
