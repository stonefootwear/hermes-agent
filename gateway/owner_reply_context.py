"""Durable, profile-scoped opaque bindings for owner support replies.

Bindings deliberately retain only validated routing references.  They never
contain alert/reply text, private case context, credentials, or capabilities.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.config import get_hermes_home

_STORE_FILENAME = "owner_reply_context.json"
_MAX_BINDINGS = 500
_SAFE_METADATA_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class OwnerReplyBinding:
    platform: str
    chat_id: str
    delivered_message_id: str
    owner_user_id: str
    session_id: str
    recorded_at: float
    case_id: str = ""
    project_ref: str = ""
    tenant_ref: str = ""
    expires_at: float = 0.0


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def _safe_metadata_value(value: Any) -> Optional[str]:
    scalar = str(value or "")
    return scalar if _SAFE_METADATA_VALUE.fullmatch(scalar) else None


class OwnerReplyContextStore:
    """Small atomic JSON store rooted under the active profile home."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(home) if home is not None else get_hermes_home()
        self.path = self.home / _STORE_FILENAME
        self._read()

    @staticmethod
    def _key(chat_id: str, message_id: str) -> str:
        return f"{chat_id}:{message_id}"

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        sanitized = {
            key: {field: value for field, value in raw.items() if field != "content"}
            if isinstance(raw, dict) and "content" in raw else raw
            for key, raw in data.items()
        }
        if sanitized == data:
            return data
        try:
            self._write(sanitized)
        except OSError:
            return {}
        return sanitized

    def _write(self, bindings: dict[str, dict[str, Any]]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{_STORE_FILENAME}.", dir=self.home, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(bindings, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def _put(self, binding: OwnerReplyBinding) -> None:
        bindings = self._read()
        bindings[self._key(binding.chat_id, binding.delivered_message_id)] = asdict(binding)
        if len(bindings) > _MAX_BINDINGS:
            oldest = sorted(bindings, key=lambda key: float(bindings[key].get("recorded_at", 0) or 0))[
                : len(bindings) - _MAX_BINDINGS
            ]
            for key in oldest:
                bindings.pop(key, None)
        self._write(bindings)

    def bind(self, *, platform: Any, chat_id: Any, delivered_message_id: Any,
             owner_user_id: Any, case_id: Any, project_ref: Any, tenant_ref: Any,
             session_id: Any, expires_at: Any) -> None:
        """Bind a confirmed Telegram delivery to strictly opaque command metadata."""
        values = [str(value or "") for value in (chat_id, delivered_message_id, owner_user_id,
                                                   project_ref, tenant_ref, session_id)]
        if _platform_value(platform) != "telegram" or any(
            _safe_metadata_value(value) is None for value in values
        ):
            raise ValueError("invalid owner reply binding metadata")
        try:
            normalized_case_id = str(uuid.UUID(str(case_id)))
            expiry = float(expires_at)
        except (ValueError, TypeError):
            raise ValueError("invalid owner reply binding metadata") from None
        if expiry <= time.time():
            raise ValueError("expired owner reply binding")
        self._put(OwnerReplyBinding(
            platform="telegram", chat_id=values[0], delivered_message_id=values[1],
            owner_user_id=values[2], case_id=normalized_case_id, project_ref=values[3],
            tenant_ref=values[4], session_id=values[5], expires_at=expiry, recorded_at=time.time(),
        ))

    def record_delivery(self, *, platform: Any, chat_id: str, delivered_message_id: str,
                        owner_user_id: str, session_id: str) -> None:
        """Compatibility receipt without command capability (never command-routable)."""
        values = (chat_id, delivered_message_id, owner_user_id, session_id)
        if _platform_value(platform) != "telegram" or not all(str(value).strip() for value in values):
            return
        self._put(OwnerReplyBinding(platform="telegram", chat_id=str(chat_id),
                  delivered_message_id=str(delivered_message_id), owner_user_id=str(owner_user_id),
                  session_id=str(session_id), recorded_at=time.time()))

    def resolve_reply(self, *, platform: Any, chat_id: Any, reply_to_message_id: Any,
                      owner_user_id: Any) -> Optional[OwnerReplyBinding]:
        if _platform_value(platform) != "telegram":
            return None
        if not all(str(value or "").strip() for value in (chat_id, reply_to_message_id, owner_user_id)):
            return None
        raw = self._read().get(self._key(str(chat_id), str(reply_to_message_id)))
        if not isinstance(raw, dict):
            return None
        try:
            binding = OwnerReplyBinding(**{key: value for key, value in raw.items() if key != "content"})
        except (TypeError, ValueError):
            return None
        if (binding.platform != "telegram" or binding.chat_id != str(chat_id)
                or binding.delivered_message_id != str(reply_to_message_id)
                or binding.owner_user_id != str(owner_user_id)):
            return None
        return binding

    def claim_reply(self, *, platform: Any, chat_id: Any, reply_to_message_id: Any,
                    owner_user_id: Any) -> Optional[OwnerReplyBinding]:
        """Atomically consume one valid opaque command binding; fail closed otherwise."""
        if _platform_value(platform) != "telegram":
            return None
        if not all(str(value or "").strip() for value in (chat_id, reply_to_message_id, owner_user_id)):
            return None
        with _STORE_LOCK:
            raw = self._read().get(self._key(str(chat_id), str(reply_to_message_id)))
            if not isinstance(raw, dict):
                return None
            try:
                binding = OwnerReplyBinding(**{key: value for key, value in raw.items() if key != "content"})
            except (TypeError, ValueError):
                return None
            if (
                binding.platform != "telegram" or binding.chat_id != str(chat_id)
                or binding.delivered_message_id != str(reply_to_message_id)
                or binding.owner_user_id != str(owner_user_id)
                or binding.expires_at <= time.time() or not binding.case_id
                or not binding.project_ref or not binding.tenant_ref
            ):
                return None
            bindings = self._read()
            key = self._key(binding.chat_id, binding.delivered_message_id)
            if key not in bindings:
                return None
            bindings.pop(key, None)
            try:
                self._write(bindings)
            except OSError:
                return None
            return binding


def bind_successful_direct_delivery(*, platform: Any, chat_id: Any, delivered_message_id: Any,
                                     context: Any) -> None:
    """Post-ack receipt hook for direct webhook delivery only."""
    if not isinstance(context, dict):
        return
    OwnerReplyContextStore().bind(
        platform=platform, chat_id=chat_id, delivered_message_id=delivered_message_id,
        owner_user_id=context.get("owner_telegram_user_id"), case_id=context.get("case_id"),
        project_ref=context.get("project_ref"), tenant_ref=context.get("tenant_ref"),
        session_id=context.get("session_id"), expires_at=context.get("expires_at"),
    )


def record_successful_delivery_receipt(*, platform: Any, chat_id: Any, delivered_message_id: Any,
                                       owner_user_id: Any, session_id: Any) -> None:
    OwnerReplyContextStore().record_delivery(platform=platform, chat_id=str(chat_id or ""),
        delivered_message_id=str(delivered_message_id or ""), owner_user_id=str(owner_user_id or ""),
        session_id=str(session_id or ""))


def reply_context_for_event(event: Any, source: Any) -> str:
    """Legacy lookup is intentionally inert: bindings never enter a model turn."""
    return ""
