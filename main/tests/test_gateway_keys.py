"""The overload hook (written into the LiteLLM pod) and the portal (reads the counters) share a Redis key format
but not code: this reads both files and compares what they produce."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_hook_and_portal_agree_on_counter_keys():
    stats_src = (ROOT / "portal" / "app" / "gateway_stats.py").read_text(encoding="utf-8")
    ns: dict = {}
    # gateway_stats imports app.deps (redis, fastapi) only for the error tuple: take the constants and bucket_key
    exec(compile(stats_src.split("def _sum")[0].replace("from app.deps import REDIS_ERRORS", ""), "gateway_stats", "exec"), ns)
    hook_src = (ROOT / "main" / "litellm" / "overload_hook.py").read_text(encoding="utf-8")
    prefix = next(l for l in hook_src.splitlines() if l.startswith("COUNTER_PREFIX")).split("=")[1].strip().strip('"')
    ttl = int(next(l for l in hook_src.splitlines() if l.startswith("COUNTER_TTL_S")).split("=")[1])
    assert prefix == ns["PREFIX"]
    # the hook writes f"{prefix}:{kind}:{minute}"
    assert f"{{COUNTER_PREFIX}}:{{kind}}:{{int(time.time() // 60)}}" in hook_src
    assert ns["bucket_key"]("429", 5) == f"{prefix}:429:5"
    assert ns["bucket_key"]("5xx", 5) == f"{prefix}:5xx:5"
    # buckets outlive the longest window the page reads
    assert ttl > ns["LONG_MIN"] * 60


def test_hook_and_portal_agree_on_the_per_model_traffic_keys():
    """Task 2-33: `portal:gwm:<minute>` with the fields `<model>:req|429|5xx|t<bound>|tinf`."""
    src = (ROOT / "portal" / "app" / "metrics" / "gateway_models.py").read_text(encoding="utf-8")
    ns: dict = {}
    # the module imports app.deps (redis) only for the error tuple: take the constants and bucket_key
    exec(compile(src.split("@dataclass")[0].replace("from app.deps import REDIS_ERRORS", ""), "gateway_models", "exec"), ns)
    hook_src = (ROOT / "main" / "litellm" / "overload_hook.py").read_text(encoding="utf-8")
    hook_ns: dict = {}
    for line in hook_src.splitlines():
        if line.startswith(("MODEL_PREFIX", "TTFT_BOUNDS_MS", "COUNTER_TTL_S")):
            exec(line, hook_ns)
    assert hook_ns["MODEL_PREFIX"] == ns["PREFIX"]
    assert tuple(hook_ns["TTFT_BOUNDS_MS"]) == tuple(ns["TTFT_BOUNDS_MS"])
    assert ns["bucket_key"](7) == f"{hook_ns['MODEL_PREFIX']}:7"
    # the hook writes f"{MODEL_PREFIX}:{minute}" and the fields f"{model}:{field}" with field req, 429, 5xx, t<bound>, tinf
    assert 'f"{MODEL_PREFIX}:{int(time.time() // 60)}"' in hook_src
    assert 'f"{model}:{field}"' in hook_src
    assert 'return f"t{bound}"' in hook_src and 'return "tinf"' in hook_src
    # the minute hashes outlive the longest read (the previous minute) many times over
    assert hook_ns["COUNTER_TTL_S"] > 120
