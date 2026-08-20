"""Cross-process-safe opaque owner-reply delivery receipts.

This store contains only Fareeq-issued opaque handles and verified delivery
coordinates; case IDs, message text and model context are deliberately absent.
"""
from __future__ import annotations
import sqlite3
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

class OwnerReplyContextStore:
    def __init__(self, home: Optional[Path] = None) -> None:
        self.path = (Path(home) if home else get_hermes_home()) / "owner_reply_context.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS owner_reply_receipts (chat_id TEXT NOT NULL, message_id TEXT NOT NULL, owner_user_id TEXT NOT NULL, owner_profile_id TEXT NOT NULL, handle TEXT NOT NULL, PRIMARY KEY(chat_id, message_id))")
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA busy_timeout=5000")
        return db
    def record_delivery(self, *, platform: Any, chat_id: str, delivered_message_id: str, owner_user_id: str, owner_profile_id: str, handle: str) -> None:
        if str(getattr(platform, "value", platform)).lower() != "telegram" or not all((chat_id, delivered_message_id, owner_user_id, owner_profile_id, handle)) or len(handle) != 64: return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO owner_reply_receipts VALUES (?, ?, ?, ?, ?)", (str(chat_id), str(delivered_message_id), str(owner_user_id), str(owner_profile_id), handle))
            db.execute("COMMIT")
    def resolve_reply(self, *, platform: Any, chat_id: str, reply_to_message_id: str, owner_user_id: str) -> Optional[OwnerReplyBinding]:
        if str(getattr(platform, "value", platform)).lower() != "telegram": return None
        with self._connect() as db:
            row = db.execute("SELECT handle, owner_profile_id, chat_id, owner_user_id, message_id FROM owner_reply_receipts WHERE chat_id=? AND message_id=? AND owner_user_id=?", (str(chat_id), str(reply_to_message_id), str(owner_user_id))).fetchone()
        return OwnerReplyBinding(*row) if row else None

def record_successful_delivery_receipt(**kwargs: Any) -> None:
    OwnerReplyContextStore().record_delivery(**kwargs)

def reply_context_for_event(event: Any, source: Any) -> Optional[OwnerReplyBinding]:
    return OwnerReplyContextStore().resolve_reply(platform=getattr(source, "platform", ""), chat_id=getattr(source, "chat_id", ""), reply_to_message_id=getattr(event, "reply_to_message_id", ""), owner_user_id=getattr(source, "user_id", ""))
