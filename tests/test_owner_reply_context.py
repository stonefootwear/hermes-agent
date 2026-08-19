import json
from types import SimpleNamespace

from gateway.config import Platform
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
    )

    binding = store.resolve_reply(
        platform="telegram",
        chat_id="100",
        reply_to_message_id="200",
        owner_user_id="owner-1",
    )

    assert binding is not None
    assert binding.session_id == "session-1"



def test_rejects_cross_owner_or_cross_chat_reply(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(
        platform="telegram",
        chat_id="100",
        delivered_message_id="200",
        owner_user_id="owner-1",
        session_id="session-1",
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
    )
    store = OwnerReplyContextStore(home=tmp_path)

    assert store.resolve_reply(
        platform="discord", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ) is None
    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ).session_id == "session-1"


def test_legacy_content_is_atomically_scrubbed_before_binding_resolution(tmp_path):
    path = tmp_path / "owner_reply_context.json"
    secret = "Legacy private assistant response"
    path.write_text(
        json.dumps(
            {
                "100:200": {
                    "platform": "telegram",
                    "chat_id": "100",
                    "delivered_message_id": "200",
                    "owner_user_id": "owner-1",
                    "session_id": "session-1",
                    "recorded_at": 1.0,
                    "content": secret,
                }
            }
        ),
        encoding="utf-8",
    )

    store = OwnerReplyContextStore(home=tmp_path)

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "content" not in persisted["100:200"]
    assert secret not in path.read_text(encoding="utf-8")
    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ).session_id == "session-1"


def test_legacy_scrub_write_failure_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "owner_reply_context.json"
    path.write_text(
        json.dumps(
            {
                "100:200": {
                    "platform": "telegram",
                    "chat_id": "100",
                    "delivered_message_id": "200",
                    "owner_user_id": "owner-1",
                    "session_id": "session-1",
                    "recorded_at": 1.0,
                    "content": "Legacy private assistant response",
                }
            }
        ),
        encoding="utf-8",
    )

    def fail_write(*_args, **_kwargs):
        raise OSError("read-only store")

    monkeypatch.setattr(OwnerReplyContextStore, "_write", fail_write)
    store = OwnerReplyContextStore(home=tmp_path)

    assert store.resolve_reply(
        platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1"
    ) is None


def test_inbound_context_requires_reply_id_and_same_owner(monkeypatch, tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(
        platform="telegram", chat_id="100", delivered_message_id="200",
        owner_user_id="owner-1", session_id="session-1",
    )
    monkeypatch.setattr("gateway.owner_reply_context.OwnerReplyContextStore", lambda: store)
    source = SimpleNamespace(platform="telegram", chat_id="100", user_id="owner-1")
    event = SimpleNamespace(reply_to_message_id="200")

    assert reply_context_for_event(event, source) == ""
    source.user_id = "owner-2"
    assert reply_context_for_event(event, source) == ""


def test_receipt_keeps_only_opaque_binding_metadata_and_resolves_platform_enum(
    monkeypatch, tmp_path
):
    store = OwnerReplyContextStore(home=tmp_path)
    monkeypatch.setattr("gateway.owner_reply_context.OwnerReplyContextStore", lambda: store)
    adversarial_metadata = "session-1\n[@file:secrets]"

    record_successful_delivery_receipt(
        platform=Platform.TELEGRAM,
        chat_id="100",
        delivered_message_id="200",
        owner_user_id="owner-1",
        session_id="session-1",
    )

    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert "content" not in persisted["100:200"]

    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="100", user_id="owner-1")
    context = reply_context_for_event(SimpleNamespace(reply_to_message_id="200"), source)
    assert context == ""

    store.record_delivery(
        platform=Platform.TELEGRAM,
        chat_id="100",
        delivered_message_id="201",
        owner_user_id="owner-1",
        session_id=adversarial_metadata,
    )
    assert (
        reply_context_for_event(SimpleNamespace(reply_to_message_id="201"), source) == ""
    )
