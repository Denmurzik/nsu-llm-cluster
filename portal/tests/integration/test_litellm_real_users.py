"""Blocking, unblocking and the limits of a user's keys against real LiteLLM v1.106.6 (task 2-32)."""

import uuid

import pytest

from app.litellm_client import LiteLLMClient, LiteLLMError
from tests.integration.test_litellm_real import (  # noqa: F401 - the fixtures of the stack
    _backend_base,
    _chat_text,
    _eventually,
    _refused,
    client,
    stack,
)

pytestmark = pytest.mark.docker

HELLO = "Привет из тестовой модели"


async def test_real_block_and_unblock_keep_the_key_values(stack, client):
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.add_model("pm-blk", "blk-model", _backend_base(stack), "k8s", "n1", 4096)
    a = await client.generate_key(user, f"{user}:a", None, None, None, {}, models=["public"])
    b = await client.generate_key(user, f"{user}:b", None, None, None, {}, models=["public"])
    try:
        assert await _chat_text(client, a.key, "blk-model") == HELLO
        assert await client.block_keys([a.token, b.token]) == [a.token, b.token]

        async def both_refused():
            return (await _refused(client, a.key, "blk-model")) and (await _refused(client, b.key, "blk-model"))

        denied = await _eventually(both_refused, "the blocked keys refused")
        assert denied.status in (401, 403), denied

        await client.unblock_keys([a.token, b.token])

        async def a_works():
            return (await _refused(client, a.key, "blk-model")) is None

        await _eventually(a_works, "the unblocked key works again")  # the very same key value
        assert await _chat_text(client, b.key, "blk-model") == HELLO

        # a key that is gone is skipped, not an error: blocking and unblocking after an admin revoked it
        await client.delete_keys([b.token])
        assert await client.block_keys([b.token, a.token]) == [a.token]
        await client.unblock_keys([b.token, a.token])
    finally:
        await client.delete_keys([a.token, b.token])
        await client.delete_model("pm-blk")


async def test_real_a_failed_block_releases_the_keys_it_had_blocked(stack, client):
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.add_model("pm-blk2", "blk2-model", _backend_base(stack), "k8s", "n1", 4096)
    a = await client.generate_key(user, f"{user}:a", None, None, None, {}, models=["public"])
    real_send = client._send
    calls = []

    async def flaky(method, path, **kwargs):
        calls.append(path)
        if path == "/key/block" and len([c for c in calls if c == "/key/block"]) == 2:
            raise LiteLLMError(400, "boom")
        return await real_send(method, path, **kwargs)

    client._send = flaky
    try:
        with pytest.raises(LiteLLMError):
            await client.block_keys([a.token, "a-token-that-fails"])
    finally:
        client._send = real_send
    try:

        async def works():
            return (await _refused(client, a.key, "blk2-model")) is None

        await _eventually(works, "the key released after the failed block")
        assert await _chat_text(client, a.key, "blk2-model") == HELLO
    finally:
        await client.delete_keys([a.token])
        await client.delete_model("pm-blk2")


async def test_real_update_key_limits_applies_to_a_live_key(stack, client: LiteLLMClient):
    user = f"it-user-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")
    await client.add_model("pm-lim", "lim-model", _backend_base(stack), "k8s", "n1", 4096)
    key = await client.generate_key(user, f"{user}:l", None, 600, 1_000_000, {}, models=["public"])
    try:
        assert await _chat_text(client, key.key, "lim-model") == HELLO
        assert await client.update_key_limits(key.token, 1, None)  # one request a minute; tpm stays

        async def limited():
            refused = await _refused(client, key.key, "lim-model")
            return refused if refused is not None and refused.status == 429 else None

        await _eventually(limited, "the key refused with 429 after its rpm limit dropped to 1")
        assert await client.update_key_limits("no-such-token-hash", 5, 5) is False  # a deleted key is skipped
    finally:
        await client.delete_keys([key.token])
        await client.delete_model("pm-lim")
