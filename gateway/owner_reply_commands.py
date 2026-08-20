"""Fail-closed, pre-model execution of opaque Fareeq owner-reply actions.

Only a receipt previously bound to the exact Telegram chat/user/profile may
reach this client.  The client never receives a Hermes session identifier,
model credential, case identifier, or rendered receipt text.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from gateway.owner_reply_context import OwnerReplyContextStore

_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,255}\Z")
_HANDLE_RE = re.compile(r"[a-f0-9]{64}\Z")
_DRAFT_STATUSES = frozenset({"drafted"})
_SEND_STATUSES = frozenset({"sent_to_merchant"})
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})

DRAFT_SUCCESS = "تم حفظ المسودة. اكتب ابعت للإرسال."
SEND_SUCCESS = "تم إرسال الرد للعميل."
ACTION_FAILED = "تعذر تنفيذ رد المالك. حاول مرة أخرى."
ACTION_UNAVAILABLE = "خدمة رد المالك غير مهيأة."


class OwnerReplyCommandError(RuntimeError):
    """Safe, local error category; remote messages are never propagated."""


def _valid_identity(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


@dataclass
class OwnerReplyCommandClient:
    """Minimal client for Fareeq's canonical opaque command endpoints."""

    base_url: str
    exchange_token: str
    action_token: str
    transport: Optional[httpx.AsyncBaseTransport] = None
    action_attempts: int = 2

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Owner reply endpoint must use HTTPS")
        self.base_url = self.base_url.rstrip("/")
        self.action_attempts = max(1, min(int(self.action_attempts), 3))

    @classmethod
    def from_platform_config(cls, platform_config: Any) -> Optional["OwnerReplyCommandClient"]:
        extra = getattr(platform_config, "extra", None) or {}
        base_url = str(extra.get("owner_reply_base_url") or "").strip()
        if not base_url:
            return None
        # These are independent service credentials, deliberately never derived
        # from provider/model credentials or exposed in event/context metadata.
        from agent.secret_scope import get_secret

        exchange_token = str(get_secret("HERMES_OWNER_REPLY_EXCHANGE_TOKEN") or "").strip()
        action_token = str(get_secret("HERMES_OWNER_REPLY_ACTION_TOKEN") or "").strip()
        if not exchange_token or not action_token:
            return None
        return cls(base_url=base_url, exchange_token=exchange_token, action_token=action_token)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def _post(
        self, path: str, body: dict[str, str], token: str, *, attempts: int = 1
    ) -> dict[str, Any]:
        headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=httpx.Timeout(10.0), follow_redirects=False
                ) as client:
                    response = await client.post(self._url(path), json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0)
                    continue
                raise OwnerReplyCommandError("network") from exc
            if response.status_code in _RETRYABLE_STATUSES and attempt + 1 < attempts:
                await asyncio.sleep(0)
                continue
            # Do not surface service-provided status/error strings. A strict
            # local allowlist is the only authority for remote outcomes.
            if response.status_code != 200:
                raise OwnerReplyCommandError("remote_status")
            try:
                payload = response.json()
            except ValueError as exc:
                raise OwnerReplyCommandError("remote_json") from exc
            if not isinstance(payload, dict):
                raise OwnerReplyCommandError("remote_shape")
            return payload
        raise OwnerReplyCommandError("retry_exhausted")

    async def exchange(self, *, handle: str, chat_id: str, user_id: str, profile_id: str, replied_message_id: str) -> str:
        if not _HANDLE_RE.fullmatch(handle) or not all(
            _valid_identity(value) for value in (chat_id, user_id, profile_id, replied_message_id)
        ):
            raise OwnerReplyCommandError("identity")
        payload = await self._post(
            "/api/internal/support-cases/owner-reply/exchange",
            {"handle": handle, "ownerChatId": chat_id, "ownerUserId": user_id,
             "ownerProfileId": profile_id, "repliedMessageId": replied_message_id},
            self.exchange_token,
        )
        action_handle = payload.get("actionHandle")
        if not isinstance(action_handle, str) or not _HANDLE_RE.fullmatch(action_handle):
            raise OwnerReplyCommandError("exchange_shape")
        return action_handle

    @staticmethod
    def command_id(*, action_handle: str, action: str, text: str) -> str:
        # Deterministic means a retry after an ambiguous response is the same
        # Fareeq command tuple, never a second mutation. No UUID is generated.
        digest = hashlib.sha256(f"{action_handle}\0{action}\0{text}".encode("utf-8")).hexdigest()
        return f"owner-reply-{digest}"

    async def execute(self, *, action_handle: str, text: str, command_id: str) -> tuple[str, dict[str, Any]]:
        send = text == "ابعت"
        action = "send" if send else "draft"
        body = {"actionHandle": action_handle, "commandId": command_id}
        if send:
            body.update({"confirmation": "ابعت"})
            path, allowed = "/api/internal/support-cases/send", _SEND_STATUSES
        else:
            body["draft"] = text
            path, allowed = "/api/internal/support-cases/draft", _DRAFT_STATUSES
        payload = await self._post(path, body, self.action_token, attempts=self.action_attempts)
        if payload.get("status") not in allowed:
            raise OwnerReplyCommandError("action_status")
        if not send:
            continuation = payload.get("continuationHandle")
            if not isinstance(continuation, str) or not _HANDLE_RE.fullmatch(continuation):
                raise OwnerReplyCommandError("continuation_shape")
        return (SEND_SUCCESS if send else DRAFT_SUCCESS), payload


async def execute_owner_reply_for_event(
    event: Any, source: Any, *, store: Optional[OwnerReplyContextStore] = None,
    client: Optional[OwnerReplyCommandClient] = None,
) -> Optional[str]:
    """Execute a bound owner action, returning None only when no receipt matches.

    This is called after GatewayRunner authorization and before all model/context
    preparation.  It trusts only the transport-derived SessionSource identities
    and the reply message id; SessionSource deliberately has no session id here.
    """
    store = store or OwnerReplyContextStore()
    chat_id = str(getattr(source, "chat_id", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    reply_id = str(getattr(event, "reply_to_message_id", "") or "")
    profile_id = str(getattr(source, "profile", "") or "default")
    binding = store.resolve_reply(
        platform=getattr(source, "platform", ""), chat_id=chat_id,
        reply_to_message_id=reply_id, owner_user_id=user_id,
    )
    if binding is None:
        return None
    if binding.owner_profile_id != profile_id:
        return ACTION_FAILED
    text = str(getattr(event, "text", "") or "").strip()
    if not text or len(text) > 1500:
        return ACTION_FAILED
    if client is None:
        return ACTION_UNAVAILABLE
    try:
        send = text == "ابعت"
        # A draft must first exchange its initial receipt.  After a successful
        # draft the receipt itself is rotated to Fareeq's separate continuation
        # handle, so the literal confirmation consumes that handle directly.
        action_handle = binding.handle if send else await client.exchange(
            handle=binding.handle, chat_id=chat_id, user_id=user_id,
            profile_id=profile_id, replied_message_id=reply_id,
        )
        action = "send" if send else "draft"
        command_id = client.command_id(action_handle=action_handle, action=action, text=text)
        # Persist the exact opaque Fareeq action and deterministic command before
        # the request. _post retries the same tuple after a timeout/mutation.
        store.record_action(chat_id=chat_id, message_id=reply_id, owner_user_id=user_id,
                            action_handle=action_handle, command_id=command_id, action=action)
        result, payload = await client.execute(action_handle=action_handle, text=text, command_id=command_id)
        if not send:
            store.record_continuation(chat_id=chat_id, message_id=reply_id, owner_user_id=user_id,
                                      continuation_handle=payload["continuationHandle"])
        return result
    except (OwnerReplyCommandError, ValueError):
        return ACTION_FAILED
