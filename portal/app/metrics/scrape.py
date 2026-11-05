"""Scrape of the llama-server backends of the deployments (`GET <backend>/metrics`, behind the backend key).

llama-server counts tokens and seconds since its start; the speed over the last minute is the difference of two
scrapes: generated tokens per second of generation (and the same for the prompt), which is the speed a user sees
while a model works, not diluted by idle time. A restarted backend starts its counters from zero: a counter that
went down is taken as new.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.metrics import prom

log = logging.getLogger(__name__)

SCRAPE_TIMEOUT_S = 3.0
COUNTERS = ("prompt_tokens_total", "prompt_seconds_total", "tokens_predicted_total", "tokens_predicted_seconds_total")
MAX_PARALLEL = 32


@dataclass
class Scrape:
    """One deployment over all its backends."""

    backends: int = 0  # known backends of the deployment
    up: int = 0  # of them, those that answered the scrape
    tok_s_gen: float | None = None
    tok_s_prompt: float | None = None
    slots_busy: float | None = None
    queue: float | None = None


def _delta(cur: float, prev: float | None) -> float:
    if prev is None:
        return 0.0
    return cur - prev if cur >= prev else cur  # a restarted server counts from zero again


def metrics_url(api_base: str) -> str:
    """http://ip:port/v1 -> http://ip:port/metrics"""
    root = api_base.rstrip("/")
    return root.removesuffix("/v1") + "/metrics"


class ModelScraper:
    def __init__(self, backend_key: str = "", timeout_s: float = SCRAPE_TIMEOUT_S, transport: httpx.AsyncBaseTransport | None = None):
        self._headers = {"Authorization": f"Bearer {backend_key}"} if backend_key else {}
        self._timeout_s = timeout_s
        self._transport = transport  # tests
        self._prev: dict[str, dict[str, float]] = {}  # metrics url -> the counters of the previous scrape

    async def _fetch(self, http: httpx.AsyncClient, url: str, key: str | None = None) -> dict[str, float] | None:
        headers = {"Authorization": f"Bearer {key}"} if key else self._headers
        try:
            async with asyncio.timeout(self._timeout_s):
                r = await http.get(url, headers=headers)
        except (httpx.HTTPError, OSError, TimeoutError):
            return None
        if r.status_code != 200:
            log.debug("metrics: %s answered HTTP %s", url, r.status_code)
            return None
        return prom.parse(r.text) or None

    async def scrape(self, backends: list[dict], keys: dict[str, str] | None = None) -> dict[str, Scrape]:
        """{deployment: Scrape} for `backends` (the entries of the backend status: model_name, api_base, node). `keys`:
        node -> its own backend key (app.nodes.identity); a node without one is asked with the key of the constructor."""
        keys = keys or {}
        targets = [
            (b["model_name"], metrics_url(b["api_base"]), keys.get(str(b.get("node") or "").lower()))
            for b in backends
            if b.get("model_name") and b.get("api_base")
        ]
        out: dict[str, Scrape] = {}
        gen: dict[str, list[float]] = {}
        prompt: dict[str, list[float]] = {}
        sem = asyncio.Semaphore(MAX_PARALLEL)
        async with httpx.AsyncClient(timeout=self._timeout_s, trust_env=False, transport=self._transport) as http:

            async def one(url: str, key: str | None):
                async with sem:
                    return await self._fetch(http, url, key)

            readings = await asyncio.gather(*(one(url, key) for _, url, key in targets))
        for (model, url, _), cur in zip(targets, readings, strict=True):
            s = out.setdefault(model, Scrape())
            s.backends += 1
            if cur is None:
                self._prev.pop(url, None)
                continue
            s.up += 1
            prev = self._prev.get(url)
            self._prev[url] = {k: cur[k] for k in COUNTERS if k in cur}
            s.slots_busy = (s.slots_busy or 0.0) + cur.get("requests_processing", 0.0)
            s.queue = (s.queue or 0.0) + cur.get("requests_deferred", 0.0)
            if prev is not None:
                g = gen.setdefault(model, [0.0, 0.0])
                g[0] += _delta(cur.get("tokens_predicted_total", 0.0), prev.get("tokens_predicted_total"))
                g[1] += _delta(cur.get("tokens_predicted_seconds_total", 0.0), prev.get("tokens_predicted_seconds_total"))
                p = prompt.setdefault(model, [0.0, 0.0])
                p[0] += _delta(cur.get("prompt_tokens_total", 0.0), prev.get("prompt_tokens_total"))
                p[1] += _delta(cur.get("prompt_seconds_total", 0.0), prev.get("prompt_seconds_total"))
        for model, (tokens, seconds) in gen.items():
            if seconds > 0:
                out[model].tok_s_gen = round(tokens / seconds, 2)
        for model, (tokens, seconds) in prompt.items():
            if seconds > 0:
                out[model].tok_s_prompt = round(tokens / seconds, 2)
        live = {url for _, url, _ in targets}
        for url in [u for u in self._prev if u not in live]:  # backends that left
            del self._prev[url]
        return out
