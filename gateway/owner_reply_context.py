"""Durable, cross-process-safe opaque bindings for owner support replies."""
from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.config import get_hermes_home

_STORE_FILENAME = "owner_reply_context.sqlite3"
_MAX_BINDINGS = 500
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


@dataclass(frozen=True)
class OwnerReplyBinding:
    platform: str
    chat_id: str
    delivered_message_id: str
    owner_user_id: str
    session_id: str
    binding_handle: str
    binding_assertion: str
    expires_at: float


def _platform(value: Any) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _valid(value: Any) -> Optional[str]:
    text = str(value or "")
    return text if _SAFE.fullmatch(text) else None


class OwnerReplyContextStore:
    """SQLite binding store. BEGIN IMMEDIATE makes a claim process-safe."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(home) if home is not None else get_hermes_home()
        self.path = self.home / _STORE_FILENAME
        self.home.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("""CREATE TABLE IF NOT EXISTS owner_reply_bindings (
                chat_id TEXT NOT NULL, delivered_message_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL, session_id TEXT NOT NULL,
                binding_handle TEXT NOT NULL UNIQUE, binding_assertion TEXT NOT NULL,
                expires_at REAL NOT NULL, recorded_at REAL NOT NULL,
                PRIMARY KEY (chat_id, delivered_message_id))""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def bind(self, *, platform: Any, chat_id: Any, delivered_message_id: Any,
             owner_user_id: Any, session_id: Any, binding_assertion: Any,
             expires_at: Any, binding_handle: Optional[str] = None, **_ignored: Any) -> str:
        values = [_valid(v) for v in (chat_id, delivered_message_id, owner_user_id, session_id)]
        assertion = str(binding_assertion or "")
        try:
            expiry = float(expires_at)
        except (TypeError, ValueError):
            expiry = 0
        if _platform(platform) != "telegram" or not all(values) or not assertion or expiry <= time.time():
            raise ValueError("invalid owner reply binding metadata")
        handle = binding_handle or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", handle):
            raise ValueError("invalid owner reply binding handle")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM owner_reply_bindings WHERE expires_at <= ?", (time.time(),))
            db.execute("""INSERT INTO owner_reply_bindings
                (chat_id,delivered_message_id,owner_user_id,session_id,binding_handle,binding_assertion,expires_at,recorded_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(chat_id,delivered_message_id) DO UPDATE SET
                owner_user_id=excluded.owner_user_id,session_id=excluded.session_id,
                binding_handle=excluded.binding_handle,binding_assertion=excluded.binding_assertion,
                expires_at=excluded.expires_at,recorded_at=excluded.recorded_at""",
                (*values, handle, assertion, expiry, time.time()))
            db.execute("""DELETE FROM owner_reply_bindings WHERE rowid IN (
                SELECT rowid FROM owner_reply_bindings ORDER BY recorded_at DESC LIMIT -1 OFFSET ?)""", (_MAX_BINDINGS,))
            db.execute("COMMIT")
        return handle

    def resolve_reply(self, *, platform: Any, chat_id: Any, reply_to_message_id: Any,
                      owner_user_id: Any, session_id: Any = None) -> Optional[OwnerReplyBinding]:
        if _platform(platform) != "telegram": return None
        values = [_valid(v) for v in (chat_id, reply_to_message_id, owner_user_id, session_id)]
        if not all(values): return None
        with self._connect() as db:
            row = db.execute("""SELECT * FROM owner_reply_bindings WHERE chat_id=? AND delivered_message_id=?
                AND owner_user_id=? AND session_id=? AND expires_at>?""", (*values, time.time())).fetchone()
        return OwnerReplyBinding("telegram", row["chat_id"], row["delivered_message_id"], row["owner_user_id"], row["session_id"], row["binding_handle"], row["binding_assertion"], row["expires_at"]) if row else None

    def claim_reply(self, **kwargs: Any) -> Optional[OwnerReplyBinding]:
        """Validate and delete in one durable transaction; no replay window."""
        if _platform(kwargs.get("platform")) != "telegram": return None
        values = [_valid(kwargs.get(k)) for k in ("chat_id", "reply_to_message_id", "owner_user_id", "session_id")]
        if not all(values): return None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""DELETE FROM owner_reply_bindings WHERE chat_id=? AND delivered_message_id=?
                AND owner_user_id=? AND session_id=? AND expires_at>? RETURNING *""", (*values, time.time())).fetchone()
            db.execute("COMMIT")
        return OwnerReplyBinding("telegram", row["chat_id"], row["delivered_message_id"], row["owner_user_id"], row["session_id"], row["binding_handle"], row["binding_assertion"], row["expires_at"]) if row else None

    def record_delivery(self, **_kwargs: Any) -> None:
        """Legacy receipt intentionally has no command capability."""


def bind_successful_direct_delivery(*, platform: Any, chat_id: Any, delivered_message_id: Any, context: Any) -> None:
    if isinstance(context, dict):
        OwnerReplyContextStore().bind(platform=platform, chat_id=chat_id, delivered_message_id=delivered_message_id,
            owner_user_id=context.get("owner_telegram_user_id"), session_id=context.get("session_id"),
            binding_assertion=context.get("binding_assertion"), expires_at=context.get("expires_at"))


def record_successful_delivery_receipt(**_kwargs: Any) -> None: pass

def reply_context_for_event(event: Any, source: Any) -> str: return ""
