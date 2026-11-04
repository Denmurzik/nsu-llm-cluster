"""Unit tests of main/litellm/overload_hook.py with LiteLLM and FastAPI stubbed out.

The real behaviour inside LiteLLM v1.106.6 is covered by portal/tests/integration/test_litellm_scale.py.
"""

import asyncio
import importlib.util
import sys
import time
import types
from enum import Enum
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "litellm" / "overload_hook.py"
SATURATION_TEXT = "Deployment has all max_parallel_requests slots in use."


class HTTPException(Exception):
    def __init__(self, status_code, detail=None, headers=None):
        super().__init__(detail)
        self.status_code, self.detail, self.headers = status_code, detail, headers


class RouterErrors(Enum):
    max_parallel_requests_exceeded = SATURATION_TEXT


class RouterRateLimitError(ValueError):
    def __init__(self, model, cooldown_time):
        super().__init__("No deployments available for selected model")
        self.model, self.cooldown_time = model, cooldown_time


class RateLimitError(Exception):
    status_code = 429


@pytest.fixture
def hook(monkeypatch):
    stubs = {
        "fastapi": types.ModuleType("fastapi"),
        "litellm": types.ModuleType("litellm"),
        "litellm.integrations": types.ModuleType("litellm.integrations"),
        "litellm.integrations.custom_logger": types.ModuleType("litellm.integrations.custom_logger"),
        "litellm.types": types.ModuleType("litellm.types"),
        "litellm.types.router": types.ModuleType("litellm.types.router"),
    }
    stubs["fastapi"].HTTPException = HTTPException
    stubs["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
    stubs["litellm.types.router"].RouterErrors = RouterErrors
    stubs["litellm.types.router"].RouterRateLimitError = RouterRateLimitError
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("overload_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def call(hook, exc, model="gemma-4-e4b", drain=True):
    """Run the failure hook. The counter write is fire-and-forget; `drain` waits for it so tests can look at Redis."""

    async def run():
        result = await hook.handler.async_post_call_failure_hook({"model": model}, exc, None)
        if drain:
            await asyncio.gather(*list(hook._pending))
        return result

    return asyncio.run(run())


def test_saturated_deployments_become_russian_429_with_retry_after(hook):
    exc = RateLimitError(f"litellm.RateLimitError: {SATURATION_TEXT} Deployment model_group=gemma-4-e4b, id=pm-1")
    result = call(hook, exc)
    assert isinstance(result, HTTPException)
    assert result.status_code == 429
    assert result.headers == {"Retry-After": "5"}
    assert result.detail == "Модель gemma-4-e4b перегружена: все слоты и очередь заняты. Повторите запрос через 5 с."


def test_no_deployment_available_uses_the_cooldown_for_retry_after(hook):
    result = call(hook, RouterRateLimitError("gemma-4-e4b", 29.2))
    assert result.status_code == 429
    assert result.headers == {"Retry-After": "30"}
    assert result.detail == "Нет свободных экземпляров модели gemma-4-e4b. Повторите запрос через 30 с."
    assert call(hook, RouterRateLimitError("m", 0)).headers == {"Retry-After": "1"}


@pytest.mark.parametrize(
    "exc",
    [
        RateLimitError("litellm.RateLimitError: key rpm limit exceeded"),  # the key's own limit keeps LiteLLM's answer
        ValueError(SATURATION_TEXT),  # not a 429
        RuntimeError("backend down"),
    ],
)
def test_other_errors_are_left_alone(hook, exc):
    assert call(hook, exc) is None


def test_hook_is_a_custom_logger(hook):
    assert isinstance(hook.handler, sys.modules["litellm.integrations.custom_logger"].CustomLogger)


# ---------------------------------------------------------------- gateway counters (status page, Redis)


class FakeRedis:
    """The few commands the hook uses; a pipeline runs them in order."""

    def __init__(self):
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.broken = False

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis):
        self.redis, self.ops = redis, []

    def incr(self, key):
        self.ops.append(("incr", key))
        return self

    def expire(self, key, seconds):
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self):
        if self.redis.broken:
            raise OSError("redis is down")
        for op, key, *rest in self.ops:
            if op == "incr":
                self.redis.counts[key] = self.redis.counts.get(key, 0) + 1
            else:
                self.redis.ttls[key] = rest[0]


class ServerError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


@pytest.fixture
def redis(hook, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(hook, "_redis_client", lambda: fake)
    monkeypatch.setattr(hook.time, "time", lambda: 6000.0)  # minute 100
    return fake


def test_saturation_429_is_counted_in_the_current_minute_bucket(hook, redis):
    call(hook, RateLimitError(f"litellm.RateLimitError: {SATURATION_TEXT} Deployment model_group=m"))
    assert redis.counts == {"portal:gw:429:100": 1}
    assert redis.ttls == {"portal:gw:429:100": 7200}


def test_no_deployment_available_counts_as_429(hook, redis):
    call(hook, RouterRateLimitError("m", 30))
    assert redis.counts == {"portal:gw:429:100": 1}


def test_keys_own_429_is_counted_though_litellm_answers_it(hook, redis):
    assert call(hook, RateLimitError("litellm.RateLimitError: key rpm limit exceeded")) is None
    assert redis.counts == {"portal:gw:429:100": 1}


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_counted_as_5xx(hook, redis, status):
    assert call(hook, ServerError(status)) is None
    assert redis.counts == {"portal:gw:5xx:100": 1}


@pytest.mark.parametrize("exc", [ServerError(400), ServerError(401), ServerError(408), RuntimeError("no status")])
def test_other_failures_are_not_counted(hook, redis, exc):
    call(hook, exc)
    assert redis.counts == {}


def test_counters_accumulate_per_kind(hook, redis):
    for exc in (ServerError(500), ServerError(503), RouterRateLimitError("m", 5)):
        call(hook, exc)
    assert redis.counts == {"portal:gw:5xx:100": 2, "portal:gw:429:100": 1}


def test_redis_failure_never_changes_the_answer(hook, redis):
    redis.broken = True
    result = call(hook, RouterRateLimitError("gemma-4-e4b", 29.2))
    assert result.status_code == 429 and result.headers == {"Retry-After": "30"}
    assert call(hook, ServerError(500)) is None


class HangingRedis(FakeRedis):
    def pipeline(self):
        pipe = super().pipeline()

        async def hang():
            await asyncio.sleep(30)

        pipe.execute = hang
        return pipe


def test_slow_redis_does_not_hold_the_failure_answer(hook, monkeypatch):
    # COUNT_TIMEOUT_S is long here: the answer must not wait for the counter write at all, not just for its timeout
    monkeypatch.setattr(hook, "_redis_client", lambda: HangingRedis())
    monkeypatch.setattr(hook, "COUNT_TIMEOUT_S", 5.0)
    started = time.monotonic()
    result = call(hook, RouterRateLimitError("m", 5), drain=False)
    assert result.status_code == 429
    assert time.monotonic() - started < 1.0


def test_counter_writes_in_flight_are_bounded_when_redis_hangs(hook, monkeypatch):
    monkeypatch.setattr(hook, "_redis_client", lambda: HangingRedis())
    monkeypatch.setattr(hook, "COUNT_TIMEOUT_S", 5.0)

    async def run():
        for _ in range(hook.MAX_PENDING_COUNTS + 50):
            await hook.handler.async_post_call_failure_hook({"model": "m"}, ServerError(500), None)
        return len(hook._pending)

    assert asyncio.run(run()) == hook.MAX_PENDING_COUNTS


def test_finished_counter_writes_are_not_kept(hook, redis):
    call(hook, ServerError(500))
    assert len(hook._pending) == 0
