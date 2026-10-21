"""Scheme-safe primitives for the HTTP layer: errors and NDJSON streaming."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any


def ndjson_line(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, default=str) + "\n"


async def ndjson_stream(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    async for event in events:
        yield ndjson_line(event)


def static_events(events: Iterable[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    async def _gen() -> AsyncIterator[dict[str, Any]]:
        for event in events:
            yield event

    return _gen()
