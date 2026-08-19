from types import SimpleNamespace

from gateway.owner_reply_context import (
    OwnerReplyContextStore,
    record_successful_delivery_receipt,
    reply_context_for_event,
)


def test_records_and_resolves_same_owner_reply_in_profile_store(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)

    store.record_delivery(
        platform="telegram",
        chat_id="100",
        delivered_message_id="200",
        owner_user_id="owner-1",
        session_id="session-1",
        content="The order is awaiting payment confirmation.",
    )

    binding = store.resolve_reply(
        platform="telegram",
        chat_id="100",
        reply_to_message_id="200",
        owner_user_id="owner-1",
    )

    assert binding is not None
    assert binding.session_id == "session-1"
    assert binding.content == "The order is awaiting payment confirmation."


def test_rejects_cross_owner_or_cross_chat_reply(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(
        platform="telegram",
        chat_id="100",
        delivered_message_id="200",
        owner_user_id="owner-1",
        session_id="session-1",
        content="Private owner context",
    )

    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="other-owner"
    ) is None
    assert store.resolve_reply(
        platform="telegram", chat_id="other-chat", reply_to_message_id="200", owner_user_id="owner-1"
    ) is None


def test_survives_store_recreation_and_ignores_non_telegram(tmp_path):
    OwnerReplyContextStore(home=tmp_path).record_delivery(
        platform="telegram",
        chat_id="100",
        delivered_message_id="200",
        owner_user_id="owner-1",
        session_id="session-1",
        content="Durable context",
    )
    store = OwnerReplyContextStore(home=tmp_path)

    assert store.resolve_reply(
        platform="discord", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ) is None
    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ).content == "Durable context"


def test_inbound_context_requires_reply_id_and_same_owner(monkeypatch, tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(
        platform="telegram", chat_id="100", delivered_message_id="200",
        owner_user_id="owner-1", session_id="session-1", content="Prior answer",
    )
    monkeypatch.setattr("gateway.owner_reply_context.OwnerReplyContextStore", lambda: store)
    source = SimpleNamespace(platform="telegram", chat_id="100", user_id="owner-1")
    event = SimpleNamespace(reply_to_message_id="200")

    assert "Prior answer" in reply_context_for_event(event, source)
    source.user_id = "owner-2"
    assert reply_context_for_event(event, source) == ""


def test_receipt_hook_is_scalar_and_profile_scoped(monkeypatch, tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    monkeypatch.setattr("gateway.owner_reply_context.OwnerReplyContextStore", lambda: store)

    record_successful_delivery_receipt(
        platform=SimpleNamespace(value="telegram"), chat_id="100",
        delivered_message_id="200", owner_user_id="owner-1",
        session_id="session-1", content="Final answer",
    )

    binding = store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    )
    assert binding is not None
    assert binding.content == "Final answer"
