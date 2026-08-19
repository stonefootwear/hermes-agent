"""Non-model execution path for replies quoting bound owner alerts."""
from __future__ import annotations

import os
import uuid
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from gateway.owner_reply_context import OwnerReplyContextStore

_DRAFT_CONFIRMATION = "تم حفظ المسودة."
_SEND_CONFIRMATION = "تم إرسال الرد بنجاح."
_FAILURE_CONFIRMATION = "تعذر تنفيذ الرد بأمان."
_MAX_REPLY_CHARS = 1_500
_SUCCESS_STATUSES = {"waiting_owner", "sent_to_merchant"}  # Fareeq documented terminal command results.

async def _post_json(url: str, payload: dict[str, str], credential: str) -> dict[str, Any]:
    from aiohttp import ClientSession, ClientTimeout
    async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as session:
        async with session.post(url, json=payload, headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}, allow_redirects=False) as response:
            if response.status not in {200, 201}: raise RuntimeError("owner reply service rejected request")
            body = await response.json(content_type=None)
            if not isinstance(body, dict): raise RuntimeError("owner reply service returned invalid body")
            return body

def _validated_config(config: Any) -> Optional[tuple[str, str]]:
    if not isinstance(config, dict): return None
    endpoint = str(config.get("endpoint") or "").strip().rstrip("/")
    credential_env = str(config.get("credential_env") or "HERMES_OWNER_REPLY_EXCHANGE_TOKEN").strip()
    parsed = urlparse(endpoint)
    local_http = bool(config.get("allow_insecure_local_dev")) and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (parsed.scheme == "http" and local_http) or not parsed.netloc or parsed.query
            or not endpoint.endswith("/api/internal/support-cases/exchange") or not credential_env.replace("_", "").isalnum() or not credential_env[:1].isalpha()): return None
    credential = os.getenv(credential_env, "").strip()
    return (endpoint, credential) if len(credential) >= 32 else None

def _session_id(source: Any) -> str:
    return str(getattr(source, "session_id", "") or getattr(source, "session", "") or "")

async def handle_bound_owner_reply(event: Any, source: Any, *, store: Optional[OwnerReplyContextStore] = None, config: Any = None, post_json: Callable[[str, dict[str, str], str], Awaitable[dict[str, Any]]] = _post_json) -> Optional[str]:
    if str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower() != "telegram": return None
    reply_to = getattr(event, "reply_to_message_id", None)
    session_id = _session_id(source)
    if not reply_to or not session_id: return None
    store = store or OwnerReplyContextStore()
    binding = store.resolve_reply(platform=source.platform, chat_id=source.chat_id, reply_to_message_id=reply_to, owner_user_id=source.user_id, session_id=session_id)
    if binding is None: return None
    resolved = _validated_config(config)
    text = str(getattr(event, "text", "") or "").strip()
    if resolved is None or not text or len(text) > _MAX_REPLY_CHARS: return _FAILURE_CONFIRMATION
    binding = store.claim_reply(platform=source.platform, chat_id=source.chat_id, reply_to_message_id=reply_to, owner_user_id=source.user_id, session_id=session_id)
    if binding is None: return _FAILURE_CONFIRMATION
    endpoint, credential = resolved
    action = "send" if text == "ابعت" else "draft"
    command_id = str(uuid.uuid4())
    try:
        exchange = await post_json(endpoint, {"bindingAssertion": binding.binding_assertion, "bindingHandle": binding.binding_handle, "action": action, "commandId": command_id}, credential)
        handle = exchange.get("actionHandle")
        if not isinstance(handle, str) or len(handle) < 32: raise RuntimeError("missing opaque action handle")
        target = endpoint.rsplit("/", 1)[0] + f"/{action}"
        payload = {"commandId": command_id, "reply": text, "confirmation": "ابعت"} if action == "send" else {"commandId": command_id, "draft": text}
        result = await post_json(target, payload, handle)
        if result.get("status") not in _SUCCESS_STATUSES: raise RuntimeError("unexpected command status")
    except Exception: return _FAILURE_CONFIRMATION
    return _SEND_CONFIRMATION if action == "send" else _DRAFT_CONFIRMATION
