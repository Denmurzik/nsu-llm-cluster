"""Per-user limits against real LiteLLM v1.106.6 (task SEC-1, rule 7): the user's rpm is shared by all its keys.

Without the user-level limit every key has its own window, so five keys (and the Playground key) give five times the
limit; with it the third request of a user with rpm 2 is refused whichever key it uses.
"""

import uuid

import pytest

from app.litellm_client import LiteLLMError
from tests.integration.test_litellm_real import (  # noqa: F401 - the fixtures of the stack
    _backend_base,
    _chat_text,
    _eventually,
    client,
    stack,
)

pytestmark = pytest.mark.docker


async def _wait_ready(client, model: str) -> None:
    """Polls with a key of a user without limits, so no failed attempt counts in the window of the user under test."""
    warm = f"it-ul-warm-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(warm, f"{warm}@g.nsu.ru")
    key = await client.generate_key(warm, f"{warm}:w", None, None, None, {}, models=["public"])
    try:
        async def ready():
            return await _status(client, key.key, model) == 200

        await _eventually(ready, f"{model} answers")
    finally:
        await client.delete_keys([key.token])


async def _status(client, key: str, model: str) -> int:
    try:
        await _chat_text(client, key, model)
    except LiteLLMError as exc:
        return exc.status
    return 200


async def test_real_user_rpm_is_shared_by_all_keys_of_the_user(stack, client):
    user = f"it-ul-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru", rpm_limit=2, tpm_limit=1_000_000)
    await client.add_model("pm-ul", "ul-model", _backend_base(stack), "k8s", "n1", 4096)
    # each key alone allows far more than the user: only the user-level window can refuse the third request
    a = await client.generate_key(user, f"{user}:a", None, 100, 1_000_000, {}, models=["public"])
    b = await client.generate_key(user, f"{user}:b", None, 100, 1_000_000, {}, models=["public"])
    c = await client.generate_key(user, f"{user}:c", None, 100, 1_000_000, {}, models=["public"])
    try:
        await _wait_ready(client, "ul-model")
        assert await _status(client, a.key, "ul-model") == 200  # request 1 (user window: 1 of 2)
        assert await _status(client, b.key, "ul-model") == 200  # request 2, another key
        assert await _status(client, c.key, "ul-model") == 429  # request 3, a third key: the user's rpm is spent
        assert await _status(client, a.key, "ul-model") == 429

        # another user is not affected by this user's window
        other = f"it-ul-{uuid.uuid4().hex[:8]}"
        await client.ensure_user(other, f"{other}@g.nsu.ru", rpm_limit=2, tpm_limit=1_000_000)
        d = await client.generate_key(other, f"{other}:d", None, 100, 1_000_000, {}, models=["public"])
        try:
            assert await _status(client, d.key, "ul-model") == 200
        finally:
            await client.delete_keys([d.token])
    finally:
        await client.delete_keys([a.token, b.token, c.token])
        await client.delete_model("pm-ul")


async def test_real_ensure_user_of_an_existing_user_updates_its_limits(stack, client):
    user = f"it-ul-{uuid.uuid4().hex[:8]}"
    await client.ensure_user(user, f"{user}@g.nsu.ru")  # no limits: the user as the login creates it
    await client.ensure_user(user, f"{user}@g.nsu.ru", rpm_limit=1, tpm_limit=1_000_000)  # exists: update
    await client.add_model("pm-ul2", "ul2-model", _backend_base(stack), "k8s", "n1", 4096)
    a = await client.generate_key(user, f"{user}:a", None, 100, 1_000_000, {}, models=["public"])
    b = await client.generate_key(user, f"{user}:b", None, 100, 1_000_000, {}, models=["public"])
    try:
        await _wait_ready(client, "ul2-model")
        assert await _status(client, a.key, "ul2-model") == 200
        assert await _status(client, b.key, "ul2-model") == 429
        # the limit is raised in place (the panel changed it): the user's next request goes through again
        await client.update_user_limits(user, 100, 1_000_000)

        async def raised():
            return await _status(client, b.key, "ul2-model") == 200

        await _eventually(raised, "the raised user limit applies")
    finally:
        await client.delete_keys([a.token, b.token])
        await client.delete_model("pm-ul2")
