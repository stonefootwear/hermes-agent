"""Cross-process-safe opaque owner-reply delivery receipts.

This SQLite file is a protected operational-identity record, not prompt or
forwarded metadata: it retains only the configured Telegram owner identity,
chat ID, delivered message ID, profile, and Fareeq-issued opaque handles.
Case IDs, message text, and model context are deliberately absent. Records
expire after the bounded reply window and are removed before lookup.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.config import get_hermes_home


@dataclass(frozen=True)
class OwnerReplyBinding:
    handle: str
    owner_profile_id: str
    chat_id: str
    owner_user_id: str
    delivered_message_id: str
    action_handle: str | None = None
    command_id: str | None = None
    action: str | None = None


_REPLY_RETENTION_SECONDS = 300


def configured_telegram_owner_user_id() -> str:
    """Return the sole configured Telegram owner identity, else fail closed."""
    values = {
        value.strip()
        for value in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
        if value.strip()
    }
    return values.pop() if len(values) == 1 else ""


class OwnerReplyContextStore:
    def __init__(self, home: Optional[Path] = None, retention_seconds: int = _REPLY_RETENTION_SECONDS) -> None:
        self.path = (Path(home) if home else get_hermes_home()) / "owner_reply_context.sqlite3"
        self.retention_seconds = retention_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS owner_reply_receipts (chat_id TEXT NOT NULL, message_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, owner_profile_id TEXT NOT NULL, handle TEXT NOT NULL, action_handle TEXT, command_id TEXT, action TEXT, expires_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(chat_id, message_id))")
            columns = {row[1] for row in db.execute("PRAGMA table_info(owner_reply_receipts)")}
            for column in ("action_handle", "command_id", "action", "expires_at"):
                if column not in columns:
                    db.execute(f"ALTER TABLE owner_reply_receipts ADD COLUMN {column} {'REAL' if column == 'expires_at' else 'TEXT'}")
            db.execute("DELETE FROM owner_reply_receipts WHERE expires_at <= ?", (time.time(),))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def record_delivery(self, *, platform: Any, chat_id: str, delivered_message_id: str, owner_user_id: str, owner_profile_id: str, handle: str) -> None:
        if str(getattr(platform, "value", platform)).lower() != "telegram" or not all((chat_id, delivered_message_id, owner_user_id, owner_profile_id, handle)) or len(handle) != 64:
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO owner_reply_receipts (chat_id, message_id, owner_user_id, owner_profile_id, handle, expires_at) VALUES (?, ?, ?, ?, ?, ?)", (str(chat_id), str(delivered_message_id), str(owner_user_id), str(owner_profile_id), handle, time.time() + self.retention_seconds))
            db.execute("COMMIT")

    def record_action(self, *, chat_id: str, message_id: str, owner_user_id: str, action_handle: str, command_id: str, action: str) -> None:
        if len(action_handle) != 64 or not action_handle.isalnum() or not command_id or action not in {"draft", "send"}:
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE owner_reply_receipts SET action_handle=?, command_id=?, action=? WHERE chat_id=? AND message_id=? AND owner_user_id=?", (action_handle, command_id, action, str(chat_id), str(message_id), str(owner_user_id)))
            db.execute("COMMIT")

    def record_continuation(self, *, chat_id: str, message_id: str, owner_user_id: str, continuation_handle: str) -> None:
        if len(continuation_handle) != 64 or not continuation_handle.isalnum():
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE owner_reply_receipts SET handle=?, action_handle=NULL, command_id=NULL, action=NULL WHERE chat_id=? AND message_id=? AND owner_user_id=?", (continuation_handle, str(chat_id), str(message_id), str(owner_user_id)))
            db.execute("COMMIT")

    def resolve_reply(self, *, platform: Any, chat_id: str, reply_to_message_id: str, owner_user_id: str) -> Optional[OwnerReplyBinding]:
        if str(getattr(platform, "value", platform)).lower() != "telegram":
            return None
        with self._connect() as db:
            db.execute("DELETE FROM owner_reply_receipts WHERE expires_at <= ?", (time.time(),))
            row = db.execute("SELECT handle, owner_profile_id, chat_id, owner_user_id, message_id, action_handle, command_id, action FROM owner_reply_receipts WHERE chat_id=? AND message_id=? AND owner_user_id=? AND expires_at > ?", (str(chat_id), str(reply_to_message_id), str(owner_user_id), time.time())).fetchone()
        return OwnerReplyBinding(*row) if row else None


def record_successful_delivery_receipt(**kwargs: Any) -> None:
    """Record only against Core's configured owner identity, never inbound metadata."""
    owner_user_id = configured_telegram_owner_user_id()
    if not owner_user_id:
        return
    kwargs["owner_user_id"] = owner_user_id
    OwnerReplyContextStore().record_delivery(**kwargs)


def reply_context_for_event(event: Any, source: Any) -> Optional[OwnerReplyBinding]:
    return OwnerReplyContextStore().resolve_reply(platform=getattr(source, "platform", ""), chat_id=getattr(source, "chat_id", ""), reply_to_message_id=getattr(event, "reply_to_message_id", ""), owner_user_id=getattr(source, "user_id", ""))
