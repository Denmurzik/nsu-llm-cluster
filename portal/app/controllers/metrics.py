"""Monitoring controller (task 2-33): once a minute one row per node and per deployment into `metric_samples`, and the
deletion of rows older than the setting `metrics_retention_days`.

- A node's row is its latest live sample (Redis, pushed by node-metrics.sh) when it is not older than NODE_ROW_MAX_AGE_S.
- A deployment's row joins the scrape of its llama-server backends (app/metrics/scrape.py: tokens per second, slots
  busy, queue) with the gateway traffic of the previous full minute (app/metrics/gateway_models.py: requests, 429, 5xx,
  time to the first token). The row's time is the start of the minute it was made in.

The loop of runner.py calls reconcile_once every controller interval; the work is done once per clock minute.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

from app import settings_store
from app.deployments import service
from app.metrics import gateway_models, history, live
from app.metrics.scrape import ModelScraper
from app.nodes import identity

log = logging.getLogger(__name__)

NODE_ROW_MAX_AGE_S = 90  # a node silent for longer has no row for this minute
PRUNE_EVERY_S = 600.0


def model_data(scrape, minute: gateway_models.ModelMinute | None, gateway_known: bool) -> dict[str, Any]:
    """The fields of a deployment's row; None where a source had nothing (no backend answered, no traffic, no Redis)."""
    data: dict[str, Any] = {
        "backends": scrape.backends if scrape else 0,
        "backends_up": scrape.up if scrape else 0,
        "tok_s_gen": scrape.tok_s_gen if scrape else None,
        "tok_s_prompt": scrape.tok_s_prompt if scrape else None,
        "slots_busy": scrape.slots_busy if scrape else None,
        "queue": scrape.queue if scrape else None,
        "rpm": None,
        "e429": None,
        "e5xx": None,
        "ttft_p50_ms": None,
        "ttft_p95_ms": None,
    }
    if gateway_known:  # a minute without traffic is a real zero
        m = minute or gateway_models.ModelMinute()
        data.update(rpm=m.requests, e429=m.e429, e5xx=m.e5xx, ttft_p50_ms=m.percentile(0.5), ttft_p95_ms=m.percentile(0.95))
    return data


class MetricsController:
    def __init__(self, app_state, clock=time.time, monotonic=time.monotonic):
        self.app_state = app_state
        self._clock = clock
        self._monotonic = monotonic
        self._minute: int | None = None
        self._next_prune = 0.0
        self._scraper = ModelScraper(app_state.settings.backend_api_key.get_secret_value())

    async def reconcile_once(self) -> None:
        now = self._clock()
        minute = int(now // history.MINUTE_S)
        if minute == self._minute:
            return
        state = self.app_state
        at = history.minute_start(now)

        nodes = await live.read_safe(state.redis, live.NODE_KEY) or {}
        moment = datetime.fromtimestamp(now, UTC)
        node_rows = {}
        for name, sample in nodes.items():
            age = live.age_s(sample, moment)
            if age is not None and age <= NODE_ROW_MAX_AGE_S:
                node_rows[name] = {k: v for k, v in sample.items() if k != "at"}

        backends = (getattr(state, "backend_status", None) or {}).get("backends", [])
        async with state.sessionmaker() as db:
            keys = await identity.backend_keys(db)  # every backend answers its node's own key only
        scrapes = await self._scraper.scrape(backends, keys)
        traffic = await gateway_models.read_minute(state.redis, minute - 1)

        async with state.sessionmaker() as db:
            deployments = {d.name for d in await service.list_all(db)}
            names = set(scrapes) | (set(traffic or {}) & deployments)
            model_rows = {
                name: model_data(scrapes.get(name), (traffic or {}).get(name), traffic is not None) for name in sorted(names)
            }
            await history.add_rows(db, at, "node", node_rows)
            await history.add_rows(db, at, "model", model_rows)
            await db.commit()
        self._minute = minute  # only after the write: a failed minute is tried again on the next tick
        for name, data in model_rows.items():
            try:
                await live.put(state.redis, live.MODEL_KEY, name, {"at": at.isoformat(), **data}, live.MODEL_LIVE_TTL_S)
            except Exception as exc:  # noqa: BLE001 - the live view is an extra, the rows are written
                log.warning("metrics: cannot keep the live sample of %s: %s", name, exc)
                break
        await self._prune()

    async def _prune(self) -> None:
        if self._monotonic() < self._next_prune:
            return
        self._next_prune = self._monotonic() + PRUNE_EVERY_S
        async with self.app_state.sessionmaker() as db:
            days = int(await settings_store.get(db, "metrics_retention_days", self.app_state.settings))
            deleted = await history.prune(db, days, datetime.fromtimestamp(self._clock(), UTC))
            await db.commit()
        if deleted:
            log.info("metrics: %d samples older than %d days deleted", deleted, days)
