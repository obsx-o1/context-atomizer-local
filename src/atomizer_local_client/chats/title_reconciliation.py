"""Metadata-only enrichment of existing ChatGPT Chat titles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from atomizer_local_client.history.connection import database, transaction


_STABLE_CHAT_REFERENCE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_CANONICAL_PROJECT_REFERENCE = re.compile(r"g-p-[0-9a-f]{32}", re.IGNORECASE)
_OPAQUE_PROJECT_NAME = re.compile(r"g-p-[0-9a-f]{32}(?:-[a-z0-9-]+)?", re.IGNORECASE)
_PROJECT_ACCESSIBILITY_MARKER = ", chat in project "
_MAX_OBSERVATIONS = 200


@dataclass(frozen=True)
class ChatTitleObservation:
    host_chat_reference: str
    host_project_reference: str | None
    visible_title: str | None
    aria_label: str | None


@dataclass(frozen=True)
class TitleReconciliationResult:
    observed: int
    matched: int
    updated: int
    unchanged: int
    rejected: int


def _bounded_text(value: Any, field_name: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text or null")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ValueError(f"{field_name} is not a bounded title")
    return normalized


def parse_title_observation(value: Mapping[str, Any]) -> ChatTitleObservation:
    if not isinstance(value, Mapping):
        raise ValueError("title observation must be an object")
    chat_reference = _bounded_text(
        value.get("host_chat_reference"), "host_chat_reference", 64
    )
    if chat_reference is None or _STABLE_CHAT_REFERENCE.fullmatch(chat_reference) is None:
        raise ValueError("host_chat_reference must be a stable ChatGPT conversation ID")
    project_reference = _bounded_text(
        value.get("host_project_reference"), "host_project_reference", 80
    )
    if project_reference is not None:
        if _CANONICAL_PROJECT_REFERENCE.fullmatch(project_reference) is None:
            raise ValueError("host_project_reference must be canonical")
        project_reference = project_reference.casefold()
    visible_title = _bounded_text(value.get("visible_title"), "visible_title", 200)
    aria_label = _bounded_text(value.get("aria_label"), "aria_label", 400)
    if visible_title is None and aria_label is None:
        raise ValueError("title observation has no candidate metadata")
    return ChatTitleObservation(
        host_chat_reference=chat_reference,
        host_project_reference=project_reference,
        visible_title=visible_title,
        aria_label=aria_label,
    )


def _clean_title(value: str | None, *, reject_project_prose: bool) -> str | None:
    if value is None:
        return None
    if (
        len(value) > 200
        or value.casefold() in {"chatgpt", "chatgpt chat", "chatgpt_web chat"}
        or value.casefold().startswith("chatgpt - ")
        or (reject_project_prose and _PROJECT_ACCESSIBILITY_MARKER in value.casefold())
    ):
        return None
    return value


def _trusted_project_name(
    project_host: str,
    project_reference: str | None,
    project_display_name: str,
) -> str | None:
    if project_host != "chatgpt_web" or not project_reference:
        return None
    if (
        project_display_name.casefold() == project_reference.casefold()
        or _OPAQUE_PROJECT_NAME.fullmatch(project_display_name) is not None
    ):
        return None
    return project_display_name


def _selected_title(
    observation: ChatTitleObservation,
    *,
    trusted_project_name: str | None,
) -> str | None:
    visible_title = _clean_title(observation.visible_title, reject_project_prose=True)
    if visible_title is not None:
        return visible_title
    aria_label = observation.aria_label
    if aria_label is None:
        return None
    if trusted_project_name is not None:
        suffix = f"{_PROJECT_ACCESSIBILITY_MARKER}{trusted_project_name}"
        if aria_label.endswith(suffix):
            return _clean_title(
                aria_label[: -len(suffix)].strip(), reject_project_prose=True
            )
    return _clean_title(aria_label, reject_project_prose=True)


def reconcile_existing_chat_titles(
    database_path: Path,
    raw_observations: Sequence[Mapping[str, Any]],
) -> TitleReconciliationResult:
    if not isinstance(raw_observations, Sequence) or isinstance(
        raw_observations, (str, bytes)
    ):
        raise ValueError("observations must be an array")
    if len(raw_observations) > _MAX_OBSERVATIONS:
        raise ValueError("too many title observations")
    parsed = [parse_title_observation(value) for value in raw_observations]
    grouped: dict[str, list[ChatTitleObservation]] = {}
    for observation in parsed:
        grouped.setdefault(observation.host_chat_reference, []).append(observation)

    matched = updated = unchanged = rejected = 0
    with database(database_path) as connection:
        with transaction(connection):
            for chat_reference, observations in grouped.items():
                row = connection.execute(
                    """
                    SELECT c.chat_id, c.display_title,
                           p.host AS project_host,
                           p.host_project_reference,
                           p.display_name AS project_display_name
                    FROM chats c JOIN projects p ON p.project_id = c.project_id
                    WHERE c.host = 'chatgpt_web' AND c.host_chat_reference = ?
                    """,
                    (chat_reference,),
                ).fetchone()
                if row is None:
                    continue
                matched += 1
                candidate_titles: set[str] = set()
                invalid = False
                for observation in observations:
                    stored_project_reference = row["host_project_reference"]
                    if (
                        observation.host_project_reference is not None
                        and observation.host_project_reference != stored_project_reference
                    ):
                        invalid = True
                        break
                    trusted_project_name = _trusted_project_name(
                        str(row["project_host"]),
                        str(stored_project_reference)
                        if stored_project_reference is not None
                        else None,
                        str(row["project_display_name"]),
                    )
                    selected = _selected_title(
                        observation, trusted_project_name=trusted_project_name
                    )
                    if selected is None:
                        invalid = True
                        break
                    candidate_titles.add(selected)
                if invalid or len(candidate_titles) != 1:
                    rejected += 1
                    continue
                title = candidate_titles.pop()
                if title == row["display_title"]:
                    unchanged += 1
                    continue
                connection.execute(
                    "UPDATE chats SET display_title = ? WHERE chat_id = ?",
                    (title, row["chat_id"]),
                )
                updated += 1
    return TitleReconciliationResult(
        observed=len(parsed),
        matched=matched,
        updated=updated,
        unchanged=unchanged,
        rejected=rejected,
    )
