from types import SimpleNamespace
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
        assert [row[1] for row in db.execute("PRAGMA table_info(owner_reply_receipts)")] == ["chat_id", "message_id", "owner_user_id", "owner_profile_id", "handle"]

def test_no_receipt_without_telegram_send_identity(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    store.record_delivery(platform="telegram", chat_id="100", delivered_message_id="", owner_user_id="owner", owner_profile_id="default", handle="a" * 64)
    assert store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="", owner_user_id="owner") is None
