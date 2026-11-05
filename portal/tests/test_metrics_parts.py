"""Monitoring building blocks (task 2-33) that need no database: the Prometheus reader, the llama-server scrape, the
gateway traffic of a minute, the node payload and the downsampling."""

from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest
from pydantic import ValidationError

from app.metrics import gateway_models, history, live, prom
from app.metrics.schemas import NodeMetricsIn
from app.metrics.scrape import ModelScraper, metrics_url

LLAMA_TEXT = """\
# HELP llamacpp:prompt_tokens_total Number of prompt tokens processed.
# TYPE llamacpp:prompt_tokens_total counter
llamacpp:prompt_tokens_total 1000
# HELP llamacpp:prompt_seconds_total Prompt process time
# TYPE llamacpp:prompt_seconds_total counter
llamacpp:prompt_seconds_total 10.5
llamacpp:tokens_predicted_total 200
llamacpp:tokens_predicted_seconds_total 8
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 2
llamacpp:requests_deferred 3
llamacpp:kv_cache_usage_ratio 0.25
something_else 5
llamacpp:broken abc
llamacpp:nan NaN
"""


def test_prom_reads_llama_server_metrics():
    got = prom.parse(LLAMA_TEXT)
    assert got["prompt_tokens_total"] == 1000
    assert got["prompt_seconds_total"] == 10.5
    assert got["requests_processing"] == 2 and got["requests_deferred"] == 3
    assert got["kv_cache_usage_ratio"] == 0.25
    assert "something_else" not in got and "broken" not in got and "nan" not in got  # foreign, unparsable, not finite


def test_prom_ignores_labels_and_timestamps():
    assert prom.parse('llamacpp:x{a="b"} 4 1700000000\n') == {"x": 4.0}
    assert prom.parse("") == {}


def test_metrics_url_drops_the_v1_suffix():
    assert metrics_url("http://10.0.0.5:18080/v1") == "http://10.0.0.5:18080/metrics"
    assert metrics_url("http://10.0.0.5:18080/v1/") == "http://10.0.0.5:18080/metrics"
    assert metrics_url("http://10.0.0.5:18080") == "http://10.0.0.5:18080/metrics"


def llama(prompt_tokens, prompt_s, gen_tokens, gen_s, busy=0, deferred=0):
    return (
        f"llamacpp:prompt_tokens_total {prompt_tokens}\nllamacpp:prompt_seconds_total {prompt_s}\n"
        f"llamacpp:tokens_predicted_total {gen_tokens}\nllamacpp:tokens_predicted_seconds_total {gen_s}\n"
        f"llamacpp:requests_processing {busy}\nllamacpp:requests_deferred {deferred}\n"
    )


class Backends:
    """A fake fleet: url -> text of /metrics (or an HTTP status / an exception)."""

    def __init__(self):
        self.answers: dict[str, object] = {}
        self.seen: list[httpx.Request] = []

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            answer = self.answers.get(str(request.url))
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, int):
                return httpx.Response(answer)
            if answer is None:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, text=answer)

        return httpx.MockTransport(handler)


def backend(model, host):
    return {"model_name": model, "api_base": f"http://{host}:18080/v1"}


async def test_scrape_turns_two_scrapes_into_speeds():
    fleet = Backends()
    scraper = ModelScraper("secret", transport=fleet.transport())
    fleet.answers["http://a:18080/metrics"] = llama(1000, 10, 200, 8)
    first = await scraper.scrape([backend("gemma", "a")])
    # the first scrape has no earlier counters: no speed yet, gauges are there
    assert first["gemma"].up == 1 and first["gemma"].tok_s_gen is None
    fleet.answers["http://a:18080/metrics"] = llama(1600, 20, 500, 18, busy=2, deferred=1)
    second = (await scraper.scrape([backend("gemma", "a")]))["gemma"]
    assert second.tok_s_gen == 30.0  # 300 tokens in 10 s of generation
    assert second.tok_s_prompt == 60.0  # 600 tokens in 10 s of prompt processing
    assert second.slots_busy == 2 and second.queue == 1
    # the backend key travels as a bearer token
    assert fleet.seen[0].headers["authorization"] == "Bearer secret"


async def test_scrape_sums_the_backends_of_a_deployment_and_survives_a_restart():
    fleet = Backends()
    scraper = ModelScraper(transport=fleet.transport())
    fleet.answers.update({"http://a:18080/metrics": llama(0, 0, 100, 10), "http://b:18080/metrics": llama(0, 0, 100, 10)})
    both = [backend("gemma", "a"), backend("gemma", "b")]
    await scraper.scrape(both)
    # a produced 100 tokens in 5 s; b restarted (its counters went down to 50 tokens in 2 s)
    fleet.answers.update({"http://a:18080/metrics": llama(0, 0, 200, 15), "http://b:18080/metrics": llama(0, 0, 50, 2)})
    got = (await scraper.scrape(both))["gemma"]
    assert got.backends == 2 and got.up == 2
    assert got.tok_s_gen == round(150 / 7, 2)


async def test_a_backend_that_does_not_answer_is_counted_down_and_does_not_hide_the_others():
    fleet = Backends()
    scraper = ModelScraper(transport=fleet.transport())
    fleet.answers.update({"http://a:18080/metrics": llama(0, 0, 1, 1), "http://b:18080/metrics": 401, "http://c:18080/metrics": None})
    got = await scraper.scrape([backend("gemma", "a"), backend("gemma", "b"), backend("gemma", "c")])
    assert got["gemma"].backends == 3 and got["gemma"].up == 1


async def test_a_text_without_llama_metrics_is_a_down_backend():
    fleet = Backends()
    scraper = ModelScraper(transport=fleet.transport())
    fleet.answers["http://a:18080/metrics"] = "# nothing here\n"  # started without --metrics, behind a proxy, ...
    assert (await scraper.scrape([backend("gemma", "a")]))["gemma"].up == 0


async def test_scrape_forgets_the_counters_of_a_backend_that_left():
    fleet = Backends()
    scraper = ModelScraper(transport=fleet.transport())
    fleet.answers["http://a:18080/metrics"] = llama(0, 0, 100, 10)
    await scraper.scrape([backend("gemma", "a")])
    await scraper.scrape([])
    fleet.answers["http://a:18080/metrics"] = llama(0, 0, 500, 20)
    again = await scraper.scrape([backend("gemma", "a")])
    assert again["gemma"].tok_s_gen is None  # it came back: no speed from the old counters


# ---------------------------------------------------------------- gateway traffic of a minute


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_gateway_model_key_format_is_the_one_the_hook_writes():
    assert gateway_models.bucket_key(100) == "portal:gwm:100"


async def test_read_minute_parses_counts_and_the_histogram(redis):
    await redis.hset(
        "portal:gwm:100",
        mapping={
            "gemma:req": 12, "gemma:429": 2, "gemma:5xx": 1, "gemma:t250": 3, "gemma:t500": 1, "gemma:tinf": 1,
            "other:model:req": 5, "x:t7": 9, "x:tbad": 9, "junk": 4, "gemma:req2": 7,
        },
    )  # fmt: skip
    got = await gateway_models.read_minute(redis, 100)
    assert got["gemma"].requests == 12 and got["gemma"].e429 == 2 and got["gemma"].e5xx == 1
    assert got["gemma"].ttft[1] == 3 and got["gemma"].ttft[2] == 1 and got["gemma"].ttft[-1] == 1
    assert got["other:model"].requests == 5  # a model name may hold a colon: the kind is the last part
    assert got["x"].ttft == [0] * (len(gateway_models.TTFT_BOUNDS_MS) + 1)  # an unknown bound is ignored
    assert await gateway_models.read_minute(redis, 99) == {}


def test_percentile_interpolates_inside_the_bucket():
    m = gateway_models.ModelMinute()
    m.ttft[1] = 10  # ten answers between 100 and 250 ms
    assert m.percentile(0.5) == 175.0
    assert m.percentile(0.95) == 242.5
    assert gateway_models.ModelMinute().percentile(0.5) is None


def test_percentile_over_several_buckets_and_the_open_end():
    m = gateway_models.ModelMinute()
    m.ttft[0] = 5  # up to 100 ms
    m.ttft[3] = 4  # 500 - 1000 ms
    m.ttft[-1] = 1  # above a minute
    assert m.percentile(0.5) == 100.0  # the 5th of 10 is the top of the first bucket
    assert 500 < m.percentile(0.8) <= 1000
    assert m.percentile(0.99) == 60000.0


async def test_read_minute_without_redis_is_unknown():
    class Broken:
        async def hgetall(self, key):
            raise OSError("down")

    assert await gateway_models.read_minute(Broken(), 1) is None


# ---------------------------------------------------------------- node payload


GPU = {"util_pct": 40, "vram_used_mib": 6000, "vram_total_mib": 12282, "temp_c": 61, "power_w": 120.5}


def test_the_payload_of_a_gpu_node_becomes_one_flat_sample():
    body = NodeMetricsIn.model_validate(
        {
            "gpus": [GPU, {**GPU, "util_pct": 60, "temp_c": 70, "power_w": 100}],
            "ram_used_mib": 8000, "ram_total_mib": 16000, "cpu_pct": 12.34, "models_bytes": 5, "llama_up": True,
        }
    )  # fmt: skip
    s = body.sample(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    assert s["gpu_count"] == 2 and s["gpu_util"] == 50.0
    assert s["vram_used_mib"] == 12000 and s["vram_total_mib"] == 24564
    assert s["gpu_temp_c"] == 70 and s["power_w"] == 220.5
    assert s["cpu_pct"] == 12.3 and s["llama_up"] is True and s["rpc_up"] is False
    assert s["at"] == "2026-11-04T10:00:00+00:00"


def test_a_cpu_node_has_no_gpu_fields_and_power_may_be_missing():
    s = NodeMetricsIn.model_validate({"ram_used_mib": 1, "ram_total_mib": 2, "cpu_pct": 3}).sample()
    assert s["gpu_count"] == 0 and s["gpu_util"] is None and s["vram_total_mib"] is None and s["gpu_temp_c"] is None
    s = NodeMetricsIn.model_validate(
        {"gpus": [{"util_pct": 1, "vram_used_mib": 1, "vram_total_mib": 2}], "ram_used_mib": 1, "ram_total_mib": 2, "cpu_pct": 3}
    ).sample()
    assert s["power_w"] is None and s["gpu_temp_c"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"cpu_pct": 101}, {"cpu_pct": -1}, {"ram_used_mib": -5}, {"gpus": [{**GPU, "util_pct": 101}]},
        {"gpus": [{**GPU, "temp_c": 1000}]}, {"gpus": [GPU] * 17}, {"llama_up": "maybe"}, {"models_bytes": -1},
    ],
)  # fmt: skip
def test_out_of_range_payloads_are_refused(patch):
    base = {"gpus": [GPU], "ram_used_mib": 1, "ram_total_mib": 2, "cpu_pct": 3}
    with pytest.raises(ValidationError):
        NodeMetricsIn.model_validate({**base, **patch})


# ---------------------------------------------------------------- live store and downsampling


async def test_the_push_gap_allows_one_sample_per_node(redis):
    assert await live.allow_push(redis, "pc1") is True
    assert await live.allow_push(redis, "pc1") is False
    assert await live.allow_push(redis, "pc2") is True


async def test_live_read_all_ignores_foreign_and_broken_values(redis):
    await live.put(redis, live.NODE_KEY, "pc1", {"at": "2026-11-04T10:00:00+00:00", "gpu_util": 5}, 60)
    await redis.set(live.NODE_KEY + "pc2", "{not json")
    await redis.set(live.NODE_KEY + "pc3", "[1]")
    await redis.set("portal:other", "{}")
    assert await live.read_all(redis, live.NODE_KEY) == {"pc1": {"at": "2026-11-04T10:00:00+00:00", "gpu_util": 5}}


def test_age_of_a_sample():
    now = datetime(2026, 11, 4, 10, 0, 30, tzinfo=UTC)
    assert live.age_s({"at": "2026-11-04T10:00:00+00:00"}, now) == 30
    assert live.age_s({"at": "2026-11-04T10:01:00+00:00"}, now) == 0  # a clock a little ahead
    assert live.age_s({"at": "garbage"}, now) is None and live.age_s({}, now) is None


T0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)


def rows(*values, key="gpu_util"):
    return [(T0 + timedelta(minutes=i), {key: v}) for i, v in enumerate(values)]


def test_downsample_averages_and_keeps_peaks_of_errors_queue_and_temperature():
    data = [
        (T0, {"gpu_util": 10, "e5xx": 0, "queue": 1, "gpu_temp_c": 60, "llama_up": False, "name": "x"}),
        (T0 + timedelta(minutes=1), {"gpu_util": 20, "e5xx": 4, "queue": 5, "gpu_temp_c": 70, "llama_up": True, "name": "x"}),
        (T0 + timedelta(minutes=5), {"gpu_util": 90, "e5xx": 0, "queue": 0, "gpu_temp_c": 50, "llama_up": False}),
    ]
    points = history.downsample(data, 300)
    assert [p["t"] for p in points] == [T0.isoformat(), (T0 + timedelta(minutes=5)).isoformat()]
    assert points[0]["gpu_util"] == 15 and points[0]["e5xx"] == 4 and points[0]["queue"] == 5
    assert points[0]["gpu_temp_c"] == 70 and points[0]["llama_up"] is True and points[0]["name"] == "x"
    assert points[1]["gpu_util"] == 90 and points[1]["llama_up"] is False


def test_downsample_skips_none_and_leaves_gaps_empty():
    points = history.downsample(rows(None, 40, None) + [(T0 + timedelta(minutes=30), {"gpu_util": None})], 60)
    assert [p["gpu_util"] for p in points] == [None, 40, None, None]  # a bucket is there when it has a row
    assert len(points) == 4  # nothing is invented for the 27 empty minutes in between


def test_auto_step_fits_the_range_into_the_point_limit():
    assert history.auto_step(T0, T0 + timedelta(hours=1)) == 60
    day = history.auto_step(T0, T0 + timedelta(hours=24))
    week = history.auto_step(T0, T0 + timedelta(days=7))
    assert day % 60 == 0 and week % 60 == 0
    assert 24 * 3600 / day <= history.MAX_POINTS and 7 * 86400 / week <= history.MAX_POINTS
    assert day == 300 and week == 2040
