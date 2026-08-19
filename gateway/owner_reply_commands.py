"""Non-model execution path for replies quoting bound owner alerts."""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

from gateway.owner_reply_context import OwnerReplyContextStore

_DRAFT_CONFIRMATION = "تم حفظ المسودة."
_SEND_CONFIRMATION = "تم إرسال الرد بنجاح."
_FAILURE_CONFIRMATION = "تعذر تنفيذ الرد بأمان."
_MAX_REPLY_CHARS = 1_500


async def _post_json(url: str, payload: dict[str, str], credential: str) -> dict[str, Any]:
    from aiohttp import ClientSession, ClientTimeout

    timeout = ClientTimeout(total=15)
    async with ClientSession(timeout=timeout, trust_env=False) as session:
        async with session.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
            allow_redirects=False,
        ) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError("owner reply service rejected request")
            body = await response.json(content_type=None)
            if not isinstance(body, dict):
                raise RuntimeError("owner reply service returned invalid body")
            return body


def _validated_config(config: Any) -> Optional[tuple[str, str]]:
    if not isinstance(config, dict):
        return None
    endpoint = str(config.get("endpoint") or "").strip().rstrip("/")
    credential_env = str(config.get("credential_env") or "HERMES_OWNER_REPLY_EXCHANGE_TOKEN").strip()
    parsed = urlparse(endpoint)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query
            or not endpoint.endswith("/api/internal/support-cases/exchange")
            or not credential_env.replace("_", "").isalnum() or not credential_env[:1].isalpha()):
        return None
    credential = os.getenv(credential_env, "").strip()
    if len(credential) < 32:
        return None
    return endpoint, credential


async def handle_bound_owner_reply(event: Any, source: Any, *, store: Optional[OwnerReplyContextStore] = None,
                                   config: Any = None,
                                   post_json: Callable[[str, dict[str, str], str], Awaitable[dict[str, Any]]] = _post_json,
                                   ) -> Optional[str]:
    """Claim and execute an authorized Telegram bound command without an LLM."""
    if str(getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))).lower() != "telegram":
        return None
    reply_to = getattr(event, "reply_to_message_id", None)
    if not reply_to:
        return None
    store = store or OwnerReplyContextStore()
    binding = store.resolve_reply(platform=source.platform, chat_id=source.chat_id,
                                  reply_to_message_id=reply_to, owner_user_id=source.user_id)
    if binding is None:
        return None
    resolved = _validated_config(config)
    if resolved is None:
        return _FAILURE_CONFIRMATION
    text = str(getattr(event, "text", "") or "").strip()
    if not text or len(text) > _MAX_REPLY_CHARS:
        return _FAILURE_CONFIRMATION
    binding = store.claim_reply(platform=source.platform, chat_id=source.chat_id,
                                reply_to_message_id=reply_to, owner_user_id=source.user_id)
    if binding is None:
        return _FAILURE_CONFIRMATION
    endpoint, credential = resolved
    action = "send" if text == "ابعت" else "draft"
    try:
        exchange = await post_json(endpoint, {"caseId": binding.case_id, "action": action}, credential)
        capability = exchange.get("capability") if isinstance(exchange, dict) else None
        if not isinstance(capability, str) or not capability:
            raise RuntimeError("missing action capability")
        target = endpoint.rsplit("/", 1)[0] + f"/{action}"
        if action == "send":
            result = await post_json(target, {"reply": text, "confirmation": "ابعت"}, capability)
        else:
            result = await post_json(target, {"draft": text}, capability)
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise RuntimeError("missing action result")
    except Exception:
        return _FAILURE_CONFIRMATION
    return _SEND_CONFIRMATION if action == "send" else _DRAFT_CONFIRMATION
