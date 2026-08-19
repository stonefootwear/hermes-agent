import sqlite3
from gateway.owner_reply_context import OwnerReplyContextStore

def test_sqlite_store_requires_exact_bound_identity_and_survives_recreation(tmp_path):
    store=OwnerReplyContextStore(home=tmp_path)
    store.bind(platform="telegram",chat_id="100",delivered_message_id="200",owner_user_id="owner-1",session_id="session-1",binding_assertion="signed-assertion",expires_at=4_102_444_800)
    same=OwnerReplyContextStore(home=tmp_path)
    assert same.resolve_reply(platform="telegram",chat_id="100",reply_to_message_id="200",owner_user_id="owner-1",session_id="session-1")
    for values in [("wrong","owner-1","session-1","200"),("100","wrong","session-1","200"),("100","owner-1","wrong","200"),("100","owner-1","session-1","wrong")]:
        chat,user,session,quote=values
        assert same.resolve_reply(platform="telegram",chat_id=chat,reply_to_message_id=quote,owner_user_id=user,session_id=session) is None

def test_expired_binding_and_invalid_assertion_fail_closed(tmp_path):
    store=OwnerReplyContextStore(home=tmp_path)
    try: store.bind(platform="telegram",chat_id="100",delivered_message_id="200",owner_user_id="owner-1",session_id="session-1",binding_assertion="",expires_at=4_102_444_800)
    except ValueError: pass
    else: assert False
    try: store.bind(platform="telegram",chat_id="100",delivered_message_id="200",owner_user_id="owner-1",session_id="session-1",binding_assertion="signed",expires_at=1)
    except ValueError: pass
    else: assert False

def test_wal_and_no_json_case_payload(tmp_path):
    store=OwnerReplyContextStore(home=tmp_path)
    assert sqlite3.connect(store.path).execute("PRAGMA journal_mode").fetchone()[0].lower()=="wal"
    assert not (tmp_path / "owner_reply_context.json").exists()
