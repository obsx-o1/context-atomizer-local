"""Deterministic read projections for the local human-facing Library view."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any

from atomizer_local_client.chat.contracts import CorpusType
from atomizer_local_client.history.connection import database
from atomizer_local_client.lexical.search import (
    search_all_local_history,
    search_current_project,
)


_OPAQUE_CHATGPT_PROJECT = re.compile(r"g-p-[0-9a-f]{32}(?:-[a-z0-9-]+)?", re.IGNORECASE)
_UNTRUSTWORTHY_CHATGPT_TITLES = {
    "chatgpt",
    "chatgpt chat",
    "chatgpt_web chat",
}
_UNTRUSTWORTHY_CODEX_TITLES = {
    "codex",
    "codex chat",
}
_CODEX_OPAQUE_TITLE = re.compile(r"codex [0-9a-f]{8}", re.IGNORECASE)
_CHAT_EXCERPT_LIMIT = 72


def _project_display_label(
    host: str,
    display_name: str,
    host_project_reference: str | None,
) -> str:
    """Return a human label without altering the stored Project identity or name."""
    normalized_name = display_name.strip()
    normalized_reference = (host_project_reference or "").strip()
    opaque_chatgpt_name = host == "chatgpt_web" and (
        normalized_name.casefold() == normalized_reference.casefold()
        or _OPAQUE_CHATGPT_PROJECT.fullmatch(normalized_name) is not None
    )
    return "Unnamed ChatGPT Project" if opaque_chatgpt_name else normalized_name


def _readable_timestamp(value: Any) -> str:
    """Format canonical stored timestamps in the machine's local timezone."""
    if value is None or not str(value).strip():
        return "Never"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone()
    date_label = local.strftime("%b %d, %Y")
    time_label = local.strftime("%I:%M:%S %p %Z").lstrip("0")
    return f"{date_label}, {time_label}"


def _bounded_excerpt(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    if not normalized:
        return None
    if len(normalized) > _CHAT_EXCERPT_LIMIT:
        return normalized[: _CHAT_EXCERPT_LIMIT - 1].rstrip() + "…"
    return normalized


def _chat_display_label(
    *,
    host: str,
    display_title: str,
    created_at: Any,
    first_user_content: Any,
    repeated_title_count: int,
) -> tuple[str, str]:
    """Use stored titles only when they are trustworthy human-facing metadata."""
    normalized_title = display_title.strip()
    untrustworthy = (
        host == "chatgpt_web"
        and (
            not normalized_title
            or normalized_title.casefold() in _UNTRUSTWORTHY_CHATGPT_TITLES
            or normalized_title.casefold().startswith("chatgpt - ")
            or repeated_title_count > 1
        )
    ) or (
        host == "codex"
        and (
            not normalized_title
            or normalized_title.casefold() in _UNTRUSTWORTHY_CODEX_TITLES
            or _CODEX_OPAQUE_TITLE.fullmatch(normalized_title) is not None
        )
    )
    if not untrustworthy:
        return normalized_title, "stored-title"
    label = f"Chat · {_readable_timestamp(created_at)}"
    excerpt = _bounded_excerpt(first_user_content)
    if excerpt:
        label += f' — First user message: “{excerpt}”'
        return label, "first-user-excerpt"
    return label, "timestamp-fallback"


def _decorate_project(project: dict[str, Any]) -> dict[str, Any]:
    project["display_label"] = _project_display_label(
        str(project["host"]),
        str(project["display_name"]),
        str(project["host_project_reference"])
        if project.get("host_project_reference") is not None
        else None,
    )
    project["created_at_display"] = _readable_timestamp(project.get("created_at"))
    project["updated_at_display"] = _readable_timestamp(project.get("updated_at"))
    return project


def _decorate_chat(chat: dict[str, Any]) -> dict[str, Any]:
    chat["display_label"], chat["display_label_source"] = _chat_display_label(
        host=str(chat["host"]),
        display_title=str(chat["display_title"]),
        created_at=chat.get("created_at"),
        first_user_content=chat.get("first_user_content"),
        repeated_title_count=int(chat.get("repeated_title_count") or 1),
    )
    chat["created_at_display"] = _readable_timestamp(chat.get("created_at"))
    chat["updated_at_display"] = _readable_timestamp(chat.get("updated_at"))
    return chat


def list_projects(database_path: Path) -> list[dict[str, Any]]:
    with database(database_path) as connection:
        rows = connection.execute(
            """
            SELECT p.project_id, p.host, p.host_project_reference,
                   p.display_name, p.created_at, p.updated_at,
                   (SELECT COUNT(*) FROM chats c WHERE c.project_id = p.project_id) AS chat_count,
                   (SELECT COUNT(*) FROM documents d WHERE d.project_id = p.project_id) AS document_count,
                   (SELECT COUNT(*) FROM elected_sources s WHERE s.project_id = p.project_id) AS source_count
            FROM projects p
            ORDER BY CASE WHEN p.display_name = 'Unassigned' THEN 1 ELSE 0 END,
                     lower(p.display_name), p.project_id
            """
        ).fetchall()
    return [_decorate_project(dict(row)) for row in rows]


def read_project_overview(database_path: Path, project_id: str) -> dict[str, Any]:
    with database(database_path) as connection:
        project = connection.execute(
            "SELECT project_id, host, host_project_reference, display_name, "
            "created_at, updated_at "
            "FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project is None:
            raise KeyError(project_id)
        chats = connection.execute(
            """
            SELECT c.chat_id, c.host, c.display_title, c.created_at, c.updated_at,
                   COUNT(m.message_id) AS message_count,
                   (SELECT content FROM messages first_user
                    WHERE first_user.chat_id = c.chat_id AND first_user.role = 'user'
                    ORDER BY first_user.sequence_number, first_user.message_id LIMIT 1)
                       AS first_user_content,
                   (SELECT COUNT(*) FROM chats same_title
                    WHERE same_title.project_id = c.project_id
                      AND lower(same_title.display_title) = lower(c.display_title))
                       AS repeated_title_count
            FROM chats c
            LEFT JOIN messages m ON m.chat_id = c.chat_id
            WHERE c.project_id = ?
            GROUP BY c.chat_id
            ORDER BY c.updated_at DESC, lower(c.display_title), c.chat_id
            """,
            (project_id,),
        ).fetchall()
        documents = connection.execute(
            """
            SELECT document_id, display_name, document_type, updated_at, file_size
            FROM documents WHERE project_id = ?
            ORDER BY lower(display_name), document_id
            """,
            (project_id,),
        ).fetchall()
        sources = connection.execute(
            """
            SELECT source_id, source_kind, display_name, local_source_reference,
                   created_at, updated_at, last_synced_at,
                   (SELECT COUNT(*) FROM document_source_memberships m
                    WHERE m.source_id = s.source_id) AS document_count
            FROM elected_sources s WHERE project_id = ?
            ORDER BY lower(display_name), source_id
            """,
            (project_id,),
        ).fetchall()
    result = _decorate_project(dict(project))
    result["chats"] = [_decorate_chat(dict(row)) for row in chats]
    result["documents"] = []
    for row in documents:
        document = dict(row)
        document["updated_at_display"] = _readable_timestamp(document.get("updated_at"))
        result["documents"].append(document)
    result["sources"] = []
    for row in sources:
        source = dict(row)
        source["available"] = Path(str(source["local_source_reference"])).exists()
        source["created_at_display"] = _readable_timestamp(source.get("created_at"))
        source["updated_at_display"] = _readable_timestamp(source.get("updated_at"))
        source["last_synced_at_display"] = _readable_timestamp(source.get("last_synced_at"))
        result["sources"].append(source)
    return result


def read_chat_view(database_path: Path, chat_id: str) -> dict[str, Any]:
    with database(database_path) as connection:
        chat = connection.execute(
            """
            SELECT c.chat_id, c.project_id, c.host, c.display_title,
                   c.created_at, c.updated_at,
                   p.host AS project_host,
                   p.host_project_reference,
                   p.display_name AS project_display_name,
                   (SELECT content FROM messages first_user
                    WHERE first_user.chat_id = c.chat_id AND first_user.role = 'user'
                    ORDER BY first_user.sequence_number, first_user.message_id LIMIT 1)
                       AS first_user_content,
                   (SELECT COUNT(*) FROM chats same_title
                    WHERE same_title.project_id = c.project_id
                      AND lower(same_title.display_title) = lower(c.display_title))
                       AS repeated_title_count
            FROM chats c JOIN projects p ON p.project_id = c.project_id
            WHERE c.chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        if chat is None:
            raise KeyError(chat_id)
        messages = connection.execute(
            """
            SELECT message_id, sequence_number, role, content, captured_at
            FROM messages WHERE chat_id = ?
            ORDER BY sequence_number, message_id
            """,
            (chat_id,),
        ).fetchall()
    result = _decorate_chat(dict(chat))
    result["project_display_label"] = _project_display_label(
        str(result["project_host"]),
        str(result["project_display_name"]),
        str(result["host_project_reference"]),
    )
    result["messages"] = []
    for row in messages:
        message = dict(row)
        message["captured_at_display"] = _readable_timestamp(message.get("captured_at"))
        result["messages"].append(message)
    return result


def read_document_view(database_path: Path, document_id: str) -> dict[str, Any]:
    with database(database_path) as connection:
        document = connection.execute(
            """
            SELECT d.document_id, d.project_id, d.display_name, d.document_type,
                   d.text_content, d.updated_at, d.file_size,
                   p.host AS project_host, p.host_project_reference,
                   p.display_name AS project_display_name
            FROM documents d JOIN projects p ON p.project_id = d.project_id
            WHERE d.document_id = ?
            """,
            (document_id,),
        ).fetchone()
        if document is None:
            raise KeyError(document_id)
        sources = connection.execute(
            """
            SELECT s.source_id, s.source_kind, s.display_name,
                   s.local_source_reference, s.last_synced_at
            FROM elected_sources s
            JOIN document_source_memberships m ON m.source_id = s.source_id
            WHERE m.document_id = ? ORDER BY lower(s.display_name), s.source_id
            """,
            (document_id,),
        ).fetchall()
    result = dict(document)
    result["project_display_label"] = _project_display_label(
        str(result["project_host"]),
        str(result["project_display_name"]),
        str(result["host_project_reference"]),
    )
    result["updated_at_display"] = _readable_timestamp(result.get("updated_at"))
    result["sources"] = []
    for row in sources:
        source = dict(row)
        source["last_synced_at_display"] = _readable_timestamp(source.get("last_synced_at"))
        result["sources"].append(source)
    return result


def search_library(
    database_path: Path,
    query: str,
    *,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    candidates = (
        search_current_project(database_path, project_id, query, limit=limit)
        if project_id
        else search_all_local_history(database_path, query, limit=limit)
    )
    results: list[dict[str, Any]] = []
    with database(database_path) as connection:
        for candidate in candidates:
            if candidate.corpus_type == CorpusType.CHAT_HISTORY:
                row = connection.execute(
                    """
                    SELECT m.message_id, m.sequence_number, m.role, m.content,
                           c.chat_id, c.host, c.display_title, c.created_at,
                           p.project_id, p.host AS project_host,
                           p.host_project_reference,
                           p.display_name AS project_display_name,
                           (SELECT content FROM messages first_user
                            WHERE first_user.chat_id = c.chat_id AND first_user.role = 'user'
                            ORDER BY first_user.sequence_number, first_user.message_id LIMIT 1)
                               AS first_user_content,
                           (SELECT COUNT(*) FROM chats same_title
                            WHERE same_title.project_id = c.project_id
                              AND lower(same_title.display_title) = lower(c.display_title))
                               AS repeated_title_count
                    FROM messages m
                    JOIN chats c ON c.chat_id = m.chat_id
                    JOIN projects p ON p.project_id = c.project_id
                    WHERE m.message_id = ?
                    """,
                    (candidate.source_id,),
                ).fetchone()
                if row is None:
                    continue
                value = dict(row)
                value = _decorate_chat(value)
                value["project_display_label"] = _project_display_label(
                    str(value["project_host"]),
                    str(value["project_display_name"]),
                    str(value["host_project_reference"]),
                )
                value.update(
                    source_type="Message",
                    source_name=str(value["display_label"]),
                    destination=f"/chat?chat_id={row['chat_id']}#message-{row['message_id']}",
                    score=candidate.score,
                )
                results.append(value)
            else:
                row = connection.execute(
                    """
                    SELECT d.document_id, d.display_name, d.text_content AS content,
                           p.project_id, p.host AS project_host,
                           p.host_project_reference,
                           p.display_name AS project_display_name
                    FROM documents d JOIN projects p ON p.project_id = d.project_id
                    WHERE d.document_id = ?
                    """,
                    (candidate.source_id,),
                ).fetchone()
                if row is None:
                    continue
                value = dict(row)
                value["project_display_label"] = _project_display_label(
                    str(value["project_host"]),
                    str(value["project_display_name"]),
                    str(value["host_project_reference"]),
                )
                value.update(
                    source_type="Document",
                    source_name=str(row["display_name"]),
                    destination=f"/document?document_id={row['document_id']}",
                    role=None,
                    score=candidate.score,
                )
                results.append(value)
    return results
