import time

from gateway.owner_reply_context import OwnerReplyContextStore


def test_sqlite_receipt_keeps_only_opaque_handle_and_requires_same_owner(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    handle = "a" * 64
    store.record_delivery(platform="telegram", chat_id="100", delivered_message_id="200", owner_user_id="owner-1", owner_profile_id="default", handle=handle)
    binding = store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1")
    assert binding and binding.handle == handle
    assert not store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="other")
    import sqlite3
    with sqlite3.connect(store.path) as db:
        assert [row[1] for row in db.execute("PRAGMA table_info(owner_reply_receipts)")][:5] == ["chat_id", "message_id", "owner_user_id", "owner_profile_id", "handle"]
        assert "case_id" not in [row[1] for row in db.execute("PRAGMA table_info(owner_reply_receipts)")]


def test_receipt_expires_before_it_can_authorize_a_reply(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path, retention_seconds=1)
    store.record_delivery(platform="telegram", chat_id="100", delivered_message_id="200", owner_user_id="owner", owner_profile_id="default", handle="a" * 64)
    time.sleep(1.05)
    assert store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner") is None


def test_no_receipt_without_telegram_send_identity(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="100", delivered_message_id="", owner_user_id="owner", owner_profile_id="default", handle="a" * 64)
    assert store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="", owner_user_id="owner") is None


def test_receipt_durably_rotates_to_opaque_continuation_and_command_before_send(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="100", delivered_message_id="200", owner_user_id="owner", owner_profile_id="default", handle="a" * 64)
    store.record_action(chat_id="100", message_id="200", owner_user_id="owner", action_handle="b" * 64, command_id="owner-reply-command", action="draft")
    store.record_continuation(chat_id="100", message_id="200", owner_user_id="owner", continuation_handle="c" * 64)
    binding = store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner")
    assert binding and binding.handle == "c" * 64
    assert binding.action_handle is None
    assert binding.command_id is None
    assert binding.action is None
