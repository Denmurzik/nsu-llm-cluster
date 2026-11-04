"""Gateway 429/5xx counters the LiteLLM overload hook writes to Redis, summed for the «Состояние» page."""

import fakeredis
import pytest

from app import gateway_stats
from app.gateway_stats import bucket_key, read_counts


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_bucket_key_format_is_the_one_the_hook_writes():
    assert bucket_key("429", 100) == "portal:gw:429:100"
    assert bucket_key("5xx", 7) == "portal:gw:5xx:7"


async def test_sums_the_last_5_and_the_last_60_minutes(redis):
    now = 6000.0 + 30  # minute 100, half way through
    await redis.set(bucket_key("429", 100), 3)  # this minute
    await redis.set(bucket_key("429", 96), 4)  # 5 minute window: minutes 96..100
    await redis.set(bucket_key("429", 95), 5)  # outside 5 minutes, inside the hour
    await redis.set(bucket_key("429", 41), 6)  # inside the hour: minutes 41..100
    await redis.set(bucket_key("429", 40), 99)  # older than an hour
    await redis.set(bucket_key("5xx", 99), 2)
    await redis.set(bucket_key("5xx", 10), 50)
    assert await read_counts(redis, now) == {
        "last_5m": {"rate_limited": 7, "server_errors": 2},
        "last_1h": {"rate_limited": 18, "server_errors": 2},
    }


async def test_no_counters_means_zeros_not_unknown(redis):
    assert await read_counts(redis, 6000.0) == {
        "last_5m": {"rate_limited": 0, "server_errors": 0},
        "last_1h": {"rate_limited": 0, "server_errors": 0},
    }


async def test_unreadable_redis_gives_none(redis):
    class Broken:
        async def mget(self, keys):
            raise OSError("redis is down")

    assert await read_counts(Broken(), 6000.0) is None


async def test_garbage_values_are_skipped(redis):
    await redis.set(bucket_key("429", 100), "oops")
    await redis.set(bucket_key("429", 99), 2)
    assert (await read_counts(redis, 6000.0))["last_5m"]["rate_limited"] == 2


def test_windows_cover_the_documented_minutes():
    assert (gateway_stats.SHORT_MIN, gateway_stats.LONG_MIN) == (5, 60)
