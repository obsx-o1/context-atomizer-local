"""Transactional sequencing from a normalized event to local history and FTS."""

from __future__ import annotations

from pathlib import Path

from atomizer_local_client.chat.contracts import ChatEvent, IngestionReceipt
from atomizer_local_client.chats.repository import get_or_create_chat
from atomizer_local_client.history.connection import database, transaction
from atomizer_local_client.history.message_writer import write_message
from atomizer_local_client.lexical.indexer import index_message, rebind_chat_index
from atomizer_local_client.projects.resolver import resolve_project


def ingest_chat_event(database_path: Path, event: ChatEvent) -> IngestionReceipt:
    with database(database_path) as connection:
        with transaction(connection):
            project_id = resolve_project(connection, event)
            chat_id = get_or_create_chat(connection, project_id, event)
            rebind_chat_index(connection, chat_id, project_id)
            message_id, inserted, sequence_number = write_message(connection, chat_id, event)
            if inserted:
                index_message(
                    connection,
                    message_id=message_id,
                    project_id=project_id,
                    chat_id=chat_id,
                    event=event,
                )
    return IngestionReceipt(project_id, chat_id, message_id, inserted, sequence_number)
