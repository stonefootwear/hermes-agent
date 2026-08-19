import asyncio
import multiprocessing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gateway.owner_reply_context import OwnerReplyContextStore


def _bind(store, expiry=4_102_444_800.0):
    return store.bind(platform="telegram", chat_id="100", delivered_message_id="200", owner_user_id="owner-1", session_id="session-1", binding_assertion="signed.assertion.without-case-id", expires_at=expiry)

def _claim(path, queue):
    value = OwnerReplyContextStore(home=path).claim_reply(platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1", session_id="session-1")
    queue.put(value is not None)

def test_binding_handle_is_random_and_storage_has_no_case_identifier(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path)
    handle = _bind(store)
    assert len(handle) == 32
    assert "123e4567" not in store.path.read_bytes().decode("latin1")
    binding = store.resolve_reply(platform="telegram", chat_id="100", reply_to_message_id="200", owner_user_id="owner-1", session_id="session-1")
    assert binding and binding.binding_handle == handle

def test_two_processes_can_claim_only_once(tmp_path):
    store = OwnerReplyContextStore(home=tmp_path); _bind(store)
    queue = multiprocessing.Queue()
    processes = [multiprocessing.Process(target=_claim, args=(tmp_path, queue)) for _ in range(2)]
    [p.start() for p in processes]; [p.join(10) for p in processes]
    assert [queue.get() for _ in processes].count(True) == 1

@pytest.mark.asyncio
async def test_wrong_session_chat_user_or_quote_never_calls_service(monkeypatch, tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply
    store = OwnerReplyContextStore(home=tmp_path); _bind(store); monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x" * 32)
    post = AsyncMock(); config={"endpoint":"https://fareeq.test/api/internal/support-cases/exchange"}
    for chat, user, session, quote in [("wrong","owner-1","session-1","200"),("100","wrong","session-1","200"),("100","owner-1","wrong","200"),("100","owner-1","session-1",None)]:
        source=SimpleNamespace(platform="telegram",chat_id=chat,user_id=user,session_id=session)
        assert await handle_bound_owner_reply(SimpleNamespace(reply_to_message_id=quote,text="reply"),source,store=store,config=config,post_json=post) is None
    post.assert_not_awaited()

@pytest.mark.asyncio
async def test_command_uses_opaque_handles_idempotency_and_success_allowlist(monkeypatch, tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply
    store=OwnerReplyContextStore(home=tmp_path); _bind(store); monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x"*32)
    post=AsyncMock(side_effect=[{"actionHandle":"a"*48},{"status":"waiting_owner"}])
    source=SimpleNamespace(platform="telegram",chat_id="100",user_id="owner-1",session_id="session-1")
    assert await handle_bound_owner_reply(SimpleNamespace(reply_to_message_id="200",text="reply"),source,store=store,config={"endpoint":"https://fareeq.test/api/internal/support-cases/exchange"},post_json=post)=="تم حفظ المسودة."
    exchange=post.await_args_list[0].args[1]; action=post.await_args_list[1].args[1]
    assert "caseId" not in exchange and exchange["commandId"] == action["commandId"]
    assert await handle_bound_owner_reply(SimpleNamespace(reply_to_message_id="200",text="reply"),source,store=store,config={"endpoint":"https://fareeq.test/api/internal/support-cases/exchange"},post_json=post) is None

@pytest.mark.asyncio
async def test_http_and_failed_status_fail_closed(monkeypatch,tmp_path):
    from gateway.owner_reply_commands import handle_bound_owner_reply
    store=OwnerReplyContextStore(home=tmp_path); _bind(store); monkeypatch.setenv("HERMES_OWNER_REPLY_EXCHANGE_TOKEN", "x"*32)
    source=SimpleNamespace(platform="telegram",chat_id="100",user_id="owner-1",session_id="session-1")
    post=AsyncMock()
    assert await handle_bound_owner_reply(SimpleNamespace(reply_to_message_id="200",text="reply"),source,store=store,config={"endpoint":"http://fareeq.test/api/internal/support-cases/exchange"},post_json=post)=="تعذر تنفيذ الرد بأمان."
    post.assert_not_awaited()
    post.side_effect=[{"actionHandle":"a"*48},{"status":"unexpected"}]
    assert await handle_bound_owner_reply(SimpleNamespace(reply_to_message_id="200",text="reply"),source,store=store,config={"endpoint":"https://fareeq.test/api/internal/support-cases/exchange"},post_json=post)=="تعذر تنفيذ الرد بأمان."
