import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.owner_reply_context import OwnerReplyContextStore


CASE_ID = "123e4567-e89b-42d3-a456-426614174000"


def _binding(store, *, expires_at=4_102_444_800.0):
    store.bind(
        platform="telegram",
        chat_id="100",
        delivered_message_id="200",
        owner_user_id="owner-1",
        case_id=CASE_ID,
        project_ref="fareeq-stores",
        tenant_ref="tenant-opaque-1",
        session_id="session-1",
        expires_at=expires_at,
    )


def test_bind_persists_only_validated_opaque_metadata(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    _binding(store)

    binding = store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    )

    assert binding is not None
    assert binding.case_id == CASE_ID
    assert binding.project_ref == "fareeq-stores"
    assert binding.tenant_ref == "tenant-opaque-1"
    assert "content" not in store.path.read_text(encoding="utf-8")


def test_bind_rejects_missing_or_invalid_opaque_metadata(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    with pytest.raises(ValueError):
        store.bind(
            platform="telegram", chat_id="100", delivered_message_id="200",
            owner_user_id="owner-1", case_id="not-a-uuid", project_ref="fareeq",
            tenant_ref="tenant", session_id="session", expires_at=4_102_444_800,
        )
    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ) is None


@pytest.mark.asyncio
async def test_bound_reply_drafts_without_model_and_consumes_binding(monkeypatch, tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply

    store = OwnerReplyContextStore(home=tmp_path)
    _binding(store)
    post = AsyncMock(side_effect=[
        {"summary": "حالة دعم بانتظار رد المالك.", "capability": "draft-cap", "expiresInSeconds": 120},
        {"status": "waiting_owner"},
    ])
    monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x" * 32)
    source = SimpleNamespace(platform="telegram", chat_id="100", user_id="owner-1")
    event = SimpleNamespace(reply_to_message_id="200", text="نص الرد")

    result = await handle_bound_owner_reply(
        event, source, store=store,
        config={"endpoint": "https://fareeq.private/api/internal/support-cases/exchange"},
        post_json=post,
    )

    assert result == "تم حفظ المسودة."
    assert post.await_args_list[0].args[0].endswith("/exchange")
    assert post.await_args_list[0].args[1] == {"caseId": CASE_ID, "action": "draft"}
    assert post.await_args_list[1].args[0].endswith("/draft")
    assert post.await_args_list[1].args[1] == {"draft": "نص الرد"}
    assert all("draft-cap" not in str(call.args[1]) for call in post.await_args_list)
    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ) is None


@pytest.mark.asyncio
async def test_literal_send_uses_send_capability_and_real_server_result(monkeypatch, tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply

    store = OwnerReplyContextStore(home=tmp_path)
    _binding(store)
    post = AsyncMock(side_effect=[
        {"summary": "حالة دعم بانتظار رد المالك.", "capability": "send-cap", "expiresInSeconds": 120},
        {"status": "sent_to_merchant"},
    ])
    monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x" * 32)
    source = SimpleNamespace(platform="telegram", chat_id="100", user_id="owner-1")
    event = SimpleNamespace(reply_to_message_id="200", text="ابعت")

    result = await handle_bound_owner_reply(
        event, source, store=store,
        config={"endpoint": "https://fareeq.private/api/internal/support-cases/exchange"},
        post_json=post,
    )

    assert result == "تم إرسال الرد بنجاح."
    assert post.await_args_list[0].args[1] == {"caseId": CASE_ID, "action": "send"}
    assert post.await_args_list[1].args[0].endswith("/send")
    assert post.await_args_list[1].args[1] == {"reply": "ابعت", "confirmation": "ابعت"}


@pytest.mark.asyncio
async def test_wrong_chat_user_or_no_quote_never_calls_private_service(monkeypatch, tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply

    store = OwnerReplyContextStore(home=tmp_path)
    _binding(store)
    post = AsyncMock()
    monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x" * 32)
    config = {"endpoint": "https://fareeq.private/api/internal/support-cases/exchange"}
    for source, event in (
        (SimpleNamespace(platform="telegram", chat_id="wrong", user_id="owner-1"), SimpleNamespace(reply_to_message_id="200", text="رد")),
        (SimpleNamespace(platform="telegram", chat_id="100", user_id="wrong"), SimpleNamespace(reply_to_message_id="200", text="رد")),
        (SimpleNamespace(platform="telegram", chat_id="100", user_id="owner-1"), SimpleNamespace(reply_to_message_id=None, text="رد")),
    ):
        assert await handle_bound_owner_reply(event, source, store=store, config=config, post_json=post) is None
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_exchange_error_fails_closed_and_replay_does_not_retry(monkeypatch, tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply

    store = OwnerReplyContextStore(home=tmp_path)
    _binding(store)
    post = AsyncMock(side_effect=RuntimeError("exchange unavailable"))
    monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x" * 32)
    source = SimpleNamespace(platform="telegram", chat_id="100", user_id="owner-1")
    event = SimpleNamespace(reply_to_message_id="200", text="رد")
    config = {"endpoint": "https://fareeq.private/api/internal/support-cases/exchange"}

    assert await handle_bound_owner_reply(event, source, store=store, config=config, post_json=post) == "تعذر تنفيذ الرد بأمان."
    assert await handle_bound_owner_reply(event, source, store=store, config=config, post_json=post) is None
    assert post.await_count == 1
