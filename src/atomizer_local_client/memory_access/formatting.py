"""Output bounds for frontier-facing Library results."""

from __future__ import annotations

import json
from typing import Any, Iterable


MAX_RESULTS = 8
MAX_ITEM_CONTENT_CHARS = 1_800
MAX_TOTAL_OUTPUT_CHARS = 12_000
MAX_METADATA_CHARS = 256


def bounded_text(value: object, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def bounded_item(item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in item.items():
        if value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif key == "content":
            result[key] = bounded_text(value, MAX_ITEM_CONTENT_CHARS)
        else:
            result[key] = bounded_text(value, MAX_METADATA_CHARS)
    return result


def bounded_payload(
    operation: str,
    items: Iterable[dict[str, Any]],
    *,
    requested_limit: int,
) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    clipped = False
    for item in items:
        if len(accepted) >= min(MAX_RESULTS, requested_limit):
            clipped = True
            break
        candidate = bounded_item(item)
        trial = {
            "status": "ok",
            "operation": operation,
            "items": [*accepted, candidate],
            "result_count": len(accepted) + 1,
            "truncated": clipped,
        }
        if len(json.dumps(trial, ensure_ascii=False, separators=(",", ":"))) > MAX_TOTAL_OUTPUT_CHARS:
            clipped = True
            break
        accepted.append(candidate)
    return {
        "status": "ok",
        "operation": operation,
        "items": accepted,
        "result_count": len(accepted),
        "truncated": clipped,
    }
