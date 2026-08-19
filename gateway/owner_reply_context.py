"""Durable, profile-scoped binding metadata for owner replies.

The gateway records a receipt only after a final response was accepted by a
platform. On a later authorized inbound Telegram reply, the message preparation
path can recover an opaque binding for the reply target even when Telegram does
not include quoted text in its update.

Delivered assistant text is intentionally neither persisted nor injected: reply
binding requires identity and reference metadata only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli.config import get_hermes_home


_STORE_FILENAME = "owner_reply_context.json"
_MAX_BINDINGS = 500
_SAFE_METADATA_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


@dataclass(frozen=True)
class OwnerReplyBinding:
    platform: str
    chat_id: str
    delivered_message_id: str
    owner_user_id: str
    session_id: str
    recorded_at: float


def _platform_value(platform: Any) -> str:
    """Normalize enum-backed platform values at both receipt and lookup seams."""
    return str(getattr(platform, "value", platform) or "").lower()


def _safe_metadata_value(value: Any) -> Optional[str]:
    """Allow only scalar IDs safe to interpolate into a fixed metadata note."""
    scalar = str(value or "")
    return scalar if _SAFE_METADATA_VALUE.fullmatch(scalar) else None


class OwnerReplyContextStore:
    """Small JSON binding store rooted under the active profile home."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(home) if home is not None else get_hermes_home()
        self.path = self.home / _STORE_FILENAME

    @staticmethod
    def _key(chat_id: str, message_id: str) -> str:
        return f"{chat_id}:{message_id}"

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, bindings: dict[str, dict[str, Any]]) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{_STORE_FILENAME}.", dir=self.home, text=True
        )
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

    def record_delivery(
        self,
        *,
        platform: Any,
        chat_id: str,
        delivered_message_id: str,
        owner_user_id: str,
        session_id: str,
    ) -> None:
        """Persist a receipt when all identity and reference fields are present."""
        if _platform_value(platform) != "telegram":
            return
        values = (chat_id, delivered_message_id, owner_user_id, session_id)
        if not all(str(value).strip() for value in values):
            return
        binding = OwnerReplyBinding(
            platform="telegram",
            chat_id=str(chat_id),
            delivered_message_id=str(delivered_message_id),
            owner_user_id=str(owner_user_id),
            session_id=str(session_id),
            recorded_at=time.time(),
        )
        bindings = self._read()
        bindings[self._key(binding.chat_id, binding.delivered_message_id)] = asdict(binding)
        if len(bindings) > _MAX_BINDINGS:
            oldest = sorted(
                bindings,
                key=lambda key: float(bindings[key].get("recorded_at", 0) or 0),
            )[: len(bindings) - _MAX_BINDINGS]
            for key in oldest:
                bindings.pop(key, None)
        self._write(bindings)

    def resolve_reply(
        self,
        *,
        platform: Any,
        chat_id: str,
        reply_to_message_id: str,
        owner_user_id: str,
    ) -> Optional[OwnerReplyBinding]:
        """Return a binding only for the same Telegram chat and owner identity."""
        if _platform_value(platform) != "telegram":
            return None
        if not all(str(value).strip() for value in (chat_id, reply_to_message_id, owner_user_id)):
            return None
        raw = self._read().get(self._key(str(chat_id), str(reply_to_message_id)))
        if not isinstance(raw, dict):
            return None
        try:
            # Discard legacy stored content rather than retaining it in memory.
            binding = OwnerReplyBinding(**{key: value for key, value in raw.items() if key != "content"})
        except (TypeError, ValueError):
            return None
        if (
            binding.platform != "telegram"
            or binding.chat_id != str(chat_id)
            or binding.delivered_message_id != str(reply_to_message_id)
            or binding.owner_user_id != str(owner_user_id)
        ):
            return None
        return binding


def record_successful_delivery_receipt(
    *,
    platform: Any,
    chat_id: Any,
    delivered_message_id: Any,
    owner_user_id: Any,
    session_id: Any,
) -> None:
    """Post-success receipt hook used by gateway delivery; failures stay isolated."""
    OwnerReplyContextStore().record_delivery(
        platform=platform,
        chat_id=str(chat_id or ""),
        delivered_message_id=str(delivered_message_id or ""),
        owner_user_id=str(owner_user_id or ""),
        session_id=str(session_id or ""),
    )


def reply_context_for_event(event: Any, source: Any) -> str:
    """Return a fixed trusted note for a verified owner reply, or an empty string."""
    binding = OwnerReplyContextStore().resolve_reply(
        platform=getattr(source, "platform", ""),
        chat_id=getattr(source, "chat_id", ""),
        reply_to_message_id=getattr(event, "reply_to_message_id", ""),
        owner_user_id=getattr(source, "user_id", ""),
    )
    if binding is None:
        return ""
    session_id = _safe_metadata_value(binding.session_id)
    delivered_message_id = _safe_metadata_value(binding.delivered_message_id)
    if session_id is None or delivered_message_id is None:
        return ""
    return (
        "Verified owner reply binding: platform=telegram; "
        f"session_id={session_id}; delivered_message_id={delivered_message_id}."
    )
