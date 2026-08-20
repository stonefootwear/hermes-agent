"""Opaque owner-reply commands execute before an agent/model turn."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from gateway.owner_reply_context import OwnerReplyContextStore
from gateway.owner_reply_commands import OwnerReplyCommandClient, execute_owner_reply_for_event


HANDLE = "a" * 64
ACTION_HANDLE = "b" * 64


def _source():
    return SimpleNamespace(platform="telegram", chat_id="chat-7", user_id="owner-9", profile="owner-profile")


def _event(text="رسالة للعميل"):
    return SimpleNamespace(text=text, reply_to_message_id="receipt-42")


def _mock_service(requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] in {"Bearer exchange-secret", "Bearer action-secret"}
        body = json.loads(request.content)
        if request.url.path.endswith("/owner-reply/exchange"):
            assert request.headers["authorization"] == "Bearer exchange-secret"
            assert body == {
                "handle": HANDLE,
                "ownerChatId": "chat-7",
                "ownerUserId": "owner-9",
                "ownerProfileId": "owner-profile",
                "repliedMessageId": "receipt-42",
            }
            return httpx.Response(200, json={"actionHandle": ACTION_HANDLE, "expiresAt": "2099-01-01T00:00:00Z"})
        if request.url.path.endswith("/draft"):
            assert request.headers["authorization"] == "Bearer action-secret"
            assert body["actionHandle"] == ACTION_HANDLE
            assert body["draft"] == "رسالة للعميل"
            assert body["commandId"].startswith("owner-reply-")
            return httpx.Response(200, json={"status": "drafted", "continuationHandle": "c" * 64})
        if request.url.path.endswith("/send"):
            assert request.headers["authorization"] == "Bearer action-secret"
            assert body["actionHandle"] == "c" * 64
            assert "reply" not in body
            assert body["confirmation"] == "ابعت"
            assert body["commandId"].startswith("owner-reply-")
            return httpx.Response(200, json={"status": "sent_to_merchant"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_bound_telegram_reply_exchanges_then_drafts_without_model_or_session_id(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(
        platform="telegram", chat_id="chat-7", delivered_message_id="receipt-42",
        owner_user_id="owner-9", owner_profile_id="owner-profile", handle=HANDLE,
    )
    requests = []
    client = OwnerReplyCommandClient(
        base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret",
        transport=_mock_service(requests),
    )

    result = asyncio.run(execute_owner_reply_for_event(_event(), _source(), store=store, client=client))

    assert result == "تم حفظ المسودة. اكتب ابعت للإرسال."
    assert [request.url.path for request in requests] == [
        "/api/internal/support-cases/owner-reply/exchange",
        "/api/internal/support-cases/draft",
    ]
    assert all(b"session" not in request.content.lower() for request in requests)
    assert all(b"uuid" not in request.content.lower() for request in requests)


def test_bound_telegram_draft_then_literal_send_rotates_the_same_receipt(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(
        platform="telegram", chat_id="chat-7", delivered_message_id="receipt-42",
        owner_user_id="owner-9", owner_profile_id="owner-profile", handle=HANDLE,
    )
    requests = []
    client = OwnerReplyCommandClient(
        base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret",
        transport=_mock_service(requests), action_attempts=2,
    )

    assert asyncio.run(execute_owner_reply_for_event(_event(), _source(), store=store, client=client)) == "تم حفظ المسودة. اكتب ابعت للإرسال."
    assert asyncio.run(execute_owner_reply_for_event(_event("ابعت"), _source(), store=store, client=client)) == "تم إرسال الرد للعميل."

    assert [request.url.path for request in requests] == [
        "/api/internal/support-cases/owner-reply/exchange",
        "/api/internal/support-cases/draft",
        "/api/internal/support-cases/send",
    ]
    assert len(json.loads(requests[-1].content)["commandId"]) <= 255


def test_remote_status_and_error_are_fail_closed_to_allowlists(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="chat-7", delivered_message_id="receipt-42", owner_user_id="owner-9", owner_profile_id="owner-profile", handle=HANDLE)

    def handler(request):
        if request.url.path.endswith("exchange"):
            return httpx.Response(200, json={"actionHandle": ACTION_HANDLE})
        return httpx.Response(200, json={"status": "unexpected", "error": "leak-this-private-error"})

    client = OwnerReplyCommandClient(base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret", transport=httpx.MockTransport(handler))
    result = asyncio.run(execute_owner_reply_for_event(_event(), _source(), store=store, client=client))
    assert result == "تعذر تنفيذ رد المالك. حاول مرة أخرى."
    assert "leak-this-private-error" not in result


def test_http_endpoint_and_unbound_reply_are_not_called(tmp_path):
    client = OwnerReplyCommandClient(base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret", transport=httpx.MockTransport(lambda _: pytest.fail("not called")))
    assert asyncio.run(execute_owner_reply_for_event(_event(), _source(), store=OwnerReplyContextStore(home=tmp_path), client=client)) is None
    with pytest.raises(ValueError, match="HTTPS"):
        OwnerReplyCommandClient(base_url="http://fareeq.test", exchange_token="exchange-secret", action_token="action-secret")


def test_gateway_runner_executes_bound_receipt_before_any_agent_path(monkeypatch, tmp_path):
    """The actual post-auth core handler returns the action result pre-model."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="chat-7", delivered_message_id="receipt-42", owner_user_id="owner-9", owner_profile_id="owner-profile", handle=HANDLE)
    requests = []
    command_client = OwnerReplyCommandClient(base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret", transport=_mock_service(requests))
    monkeypatch.setattr("gateway.owner_reply_commands.OwnerReplyContextStore", lambda: store)
    monkeypatch.setattr("gateway.owner_reply_commands.OwnerReplyCommandClient.from_platform_config", lambda _config: command_client)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._is_user_authorized = lambda _source: True
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._adapter_for_source = lambda _source: SimpleNamespace(config=SimpleNamespace(extra={}))
    runner._handle_message_with_agent = lambda *_args: pytest.fail("model path must not run")
    source = _source()
    source.platform = Platform.TELEGRAM
    event = _event()
    event.source = source
    event.internal = False

    assert asyncio.run(runner._handle_message(event)) == "تم حفظ المسودة. اكتب ابعت للإرسال."
    assert [request.url.path for request in requests] == [
        "/api/internal/support-cases/owner-reply/exchange",
        "/api/internal/support-cases/draft",
    ]


def test_action_timeout_after_server_mutation_replays_exact_command_id(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="chat-7", delivered_message_id="receipt-42", owner_user_id="owner-9", owner_profile_id="owner-profile", handle=HANDLE)
    commands = []
    attempts = 0
    def handler(request):
        nonlocal attempts
        body = json.loads(request.content)
        if request.url.path.endswith("exchange"):
            return httpx.Response(200, json={"actionHandle": ACTION_HANDLE})
        attempts += 1
        commands.append(body["commandId"])
        if attempts == 1:  # Fareeq mutated, but the response became ambiguous.
            raise httpx.ReadTimeout("after mutation", request=request)
        return httpx.Response(200, json={"status": "drafted", "continuationHandle": "c" * 64})
    client = OwnerReplyCommandClient(base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret", transport=httpx.MockTransport(handler), action_attempts=2)
    assert asyncio.run(execute_owner_reply_for_event(_event(), _source(), store=store, client=client)) == "تم حفظ المسودة. اكتب ابعت للإرسال."
    assert commands[0] == commands[1]


def test_wrong_chat_profile_or_message_never_calls_fareeq(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="chat-7", delivered_message_id="receipt-42", owner_user_id="owner-9", owner_profile_id="owner-profile", handle=HANDLE)
    client = OwnerReplyCommandClient(base_url="https://fareeq.test", exchange_token="exchange-secret", action_token="action-secret", transport=httpx.MockTransport(lambda _: pytest.fail("must not call Fareeq")))
    wrong_chat = SimpleNamespace(platform="telegram", chat_id="other", user_id="owner-9", profile="owner-profile")
    wrong_profile = SimpleNamespace(platform="telegram", chat_id="chat-7", user_id="owner-9", profile="other-profile")
    wrong_message = SimpleNamespace(text="draft", reply_to_message_id="other")
    assert asyncio.run(execute_owner_reply_for_event(_event(), wrong_chat, store=store, client=client)) is None
    assert asyncio.run(execute_owner_reply_for_event(_event(), wrong_profile, store=store, client=client)) == "تعذر تنفيذ رد المالك. حاول مرة أخرى."
    assert asyncio.run(execute_owner_reply_for_event(wrong_message, _source(), store=store, client=client)) is None
