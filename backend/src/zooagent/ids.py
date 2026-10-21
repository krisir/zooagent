"""Stable, sortable identifiers.

We use ULIDs (lower-cased, prefixed with the entity type) so ids sort by
creation time, never change when a user renames something, and stay readable
in logs and URLs.
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, length: int) -> str:
    chars: list[str] = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def ulid(timestamp: float | None = None) -> str:
    millis = int((timestamp if timestamp is not None else time.time()) * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(millis, 10) + _encode(randomness, 16)


def new_id(prefix: str) -> str:
    return f"{prefix}_{ulid()}"


def utc_now() -> str:
    """ISO-8601 UTC timestamp with milliseconds and a trailing Z."""
    now = time.time()
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    return f"{base}.{int(now * 1000) % 1000:03d}Z"
