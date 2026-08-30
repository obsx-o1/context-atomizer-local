from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PACKAGE_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from atomizer_local_client.chat.contracts import ChatEvent, Host, Role, utc_now  # noqa: E402


def chat_event(
    *,
    event_id: str,
    chat: str = "chat-1",
    turn: str | None = None,
    role: Role = Role.USER,
    content: str = "hello local history",
    project: str | None = "project-1",
    project_name: str | None = "Project One",
    host: Host = Host.CODEX,
    rebind_from: str | None = None,
) -> ChatEvent:
    return ChatEvent(
        event_id=event_id,
        host=host,
        host_project_reference=project,
        host_chat_reference=chat,
        host_turn_reference=turn or event_id,
        role=role,
        content=content,
        captured_at=utc_now(),
        project_display_name=project_name,
        chat_display_name=f"Chat {chat}",
        rebind_from_host_chat_reference=rebind_from,
    )


class TemporaryDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "data" / "history.sqlite3"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()
