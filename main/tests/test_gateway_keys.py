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
