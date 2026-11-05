"""LiteLLM proxy callback: overload answers come back as 429 in Russian with Retry-After.

Loaded by LiteLLM from the directory of config.yaml (`litellm_settings.callbacks: [overload_hook.handler]`).
LiteLLM v1.106.6 lets `async_post_call_failure_hook` return an HTTPException that replaces the error sent to
the client (proxy/utils.py `post_call_failure_hook`). Two overload cases are rewritten:
- every deployment of the model has all its `max_parallel_requests` slots in use (the router's RateLimitError
  from router_utils/client_initalization_utils.py, raised again after the router's retries);
- no deployment can take the request (RouterRateLimitError, e.g. all of them are in cooldown).
Everything else keeps LiteLLM's own answer.

Every failure that ends as 429 or 5xx is also counted in Redis for the portal's «Состояние» page: one counter per
kind and minute, `portal:gw:<429|5xx>:<minute since epoch>`, expiring after two hours (the portal sums the last
5 and the last 60 buckets, see portal/app/gateway_stats.py). Counting is best effort and fire-and-forget: the write
runs as its own task (at most MAX_PENDING_COUNTS at a time, the rest are dropped, each one gives up after
COUNT_TIMEOUT_S), so a Redis that is down or slow never changes or delays the answer the client gets.
"""

import asyncio
import logging
import math
import os
import time

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.router import RouterErrors, RouterRateLimitError

log = logging.getLogger("overload_hook")

SATURATED_RETRY_AFTER_S = 5
# counters read by portal/app/gateway_stats.py (the key format is checked against it by main/tests/test_gateway_keys.py)
COUNTER_PREFIX = "portal:gw"
COUNTER_TTL_S = 7200
COUNT_TIMEOUT_S = 1.0
# per-model traffic read by portal/app/metrics/gateway_models.py
MODEL_PREFIX = "portal:gwm"
TTFT_BOUNDS_MS = (100, 250, 500, 1000, 2000, 3000, 5000, 10000, 20000, 30000, 60000)
MODEL_NAME_MAX = 100  # a client picks the model name of a failed request: keep the hash fields short
MAX_PENDING_COUNTS = 200  # writes in flight; a stalled Redis must not pile up tasks
REDIS_URL = os.environ.get("GATEWAY_STATS_REDIS_URL", "redis://127.0.0.1:6379/0")
_WARN_EVERY_S = 60.0
SATURATED = "Модель {model} перегружена: все слоты и очередь заняты. Повторите запрос через {seconds} с."
NO_BACKENDS = "Нет свободных экземпляров модели {model}. Повторите запрос через {seconds} с."


def _too_many(message: str, seconds: int) -> HTTPException:
    return HTTPException(status_code=429, detail=message, headers={"Retry-After": str(seconds)})


_redis = None
_pending: set[asyncio.Task] = set()  # the loop keeps only weak references to tasks: hold them until they finish


def _redis_client():
    """One Redis client per worker process, created on first use inside the event loop."""
    global _redis
    if _redis is None:
        from redis import asyncio as aioredis  # imported here: the hook loads (and is tested) without it

        _redis = aioredis.Redis.from_url(REDIS_URL, socket_timeout=COUNT_TIMEOUT_S, socket_connect_timeout=COUNT_TIMEOUT_S)
    return _redis


def _status_of(exc) -> int | None:
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _kind(status: int | None) -> str | None:
    if status == 429:
        return "429"
    if status is not None and 500 <= status <= 599:
        return "5xx"
    return None


def _ttft_kind(ms: float) -> str:
    for bound in TTFT_BOUNDS_MS:
        if ms <= bound:
            return f"t{bound}"
    return "tinf"


def _model_of(kwargs: dict) -> str:
    """The model group the client asked for (the router puts it into the metadata of the call)."""
    meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    name = meta.get("model_group") or (kwargs.get("standard_logging_object") or {}).get("model_group") or kwargs.get("model")
    return str(name or "")[:MODEL_NAME_MAX]


def _ttft_ms(kwargs: dict, start_time) -> float | None:
    """Time to the first token of a streamed answer; a whole answer has no such time."""
    stream = kwargs.get("stream") or (kwargs.get("optional_params") or {}).get("stream")
    first = kwargs.get("completion_start_time")
    if not stream or first is None or start_time is None:
        return None
    try:
        ms = (first - start_time).total_seconds() * 1000.0
    except (TypeError, AttributeError):
        return None
    return ms if ms >= 0 else None


class OverloadHook(CustomLogger):
    def __init__(self):
        super().__init__()
        self._warned_at = -_WARN_EVERY_S

    def _warn(self, text: str, *args) -> None:
        now = time.monotonic()
        if now - self._warned_at >= _WARN_EVERY_S:
            self._warned_at = now
            log.warning(text, *args)

    def _count_later(self, kind: str) -> None:
        if len(_pending) >= MAX_PENDING_COUNTS:
            self._warn("gateway counters: %d writes still in flight, dropping a %s count", len(_pending), kind)
            return
        task = asyncio.get_running_loop().create_task(self._count(kind))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    async def _count(self, kind: str) -> None:
        key = f"{COUNTER_PREFIX}:{kind}:{int(time.time() // 60)}"
        try:
            await asyncio.wait_for(_redis_client().pipeline().incr(key).expire(key, COUNTER_TTL_S).execute(), COUNT_TIMEOUT_S)
        except Exception as exc:  # the counters are an extra: never fail or slow the client's answer
            self._warn("gateway counters: cannot write %s: %r", key, exc)

    def _model_count_later(self, model: str, fields: list[str]) -> None:
        if not model:
            return
        if len(_pending) >= MAX_PENDING_COUNTS:
            self._warn("gateway counters: %d writes still in flight, dropping the counts of %s", len(_pending), model)
            return
        task = asyncio.get_running_loop().create_task(self._model_count(model, fields))
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    async def _model_count(self, model: str, fields: list[str]) -> None:
        key = f"{MODEL_PREFIX}:{int(time.time() // 60)}"
        try:
            pipe = _redis_client().pipeline()
            for field in fields:
                pipe = pipe.hincrby(key, f"{model}:{field}", 1)
            await asyncio.wait_for(pipe.expire(key, COUNTER_TTL_S).execute(), COUNT_TIMEOUT_S)
        except Exception as exc:  # the counters are an extra: never fail or slow the client's answer
            self._warn("gateway counters: cannot write %s: %r", key, exc)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            fields = ["req"]
            ttft = _ttft_ms(kwargs, start_time)
            if ttft is not None:
                fields.append(_ttft_kind(ttft))
            self._model_count_later(_model_of(kwargs), fields)
        except Exception as exc:  # a logging callback must never break the request
            self._warn("gateway counters: success event ignored: %r", exc)

    async def async_post_call_failure_hook(
        self, request_data, original_exception, user_api_key_dict, traceback_str=None
    ):
        result = self._answer(request_data, original_exception)
        status = result.status_code if result is not None else _status_of(original_exception)
        kind = _kind(status)
        if kind is not None:
            self._count_later(kind)
            self._model_count_later(str((request_data or {}).get("model") or "")[:MODEL_NAME_MAX], ["req", kind])
        return result

    def _answer(self, request_data, original_exception):
        model = str(request_data.get("model") or getattr(original_exception, "model", "") or "")
        if isinstance(original_exception, RouterRateLimitError):
            seconds = max(1, math.ceil(original_exception.cooldown_time or 0))
            return _too_many(NO_BACKENDS.format(model=model, seconds=seconds), seconds)
        if (
            getattr(original_exception, "status_code", None) == 429
            and RouterErrors.max_parallel_requests_exceeded.value in str(original_exception)
        ):
            seconds = SATURATED_RETRY_AFTER_S
            return _too_many(SATURATED.format(model=model, seconds=seconds), seconds)
        return None


handler = OverloadHook()
