"""Controllers process: `python -m app.controllers` (supervisord program `portal-controllers`).

The API runs in several uvicorn workers with CONTROLLERS_ENABLED=false; the workload and backend controllers
run here, once, against the same database, LiteLLM, Kubernetes and Redis. SIGTERM stops them cleanly.
"""

import argparse
import asyncio
import logging
import signal
from types import SimpleNamespace

from starlette.datastructures import State

from app.config import Settings, get_settings
from app.controllers import runner
from app.main import close_state, open_state

log = logging.getLogger("app.controllers")


async def run(settings: Settings | None = None, stop: asyncio.Event | None = None, redis=None) -> None:
    """Runs both controllers until `stop` is set (by SIGTERM/SIGINT when no event is given)."""
    settings = settings or get_settings()
    holder = SimpleNamespace(state=State())
    if redis is not None:
        holder.state.redis = redis
    signals: list[signal.Signals] = []
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                signals.append(sig)
            except (NotImplementedError, RuntimeError):  # Windows event loops have no signal handlers
                pass
    await open_state(holder.state, settings)
    runner.start(holder)
    log.info("controllers started (interval %s s)", settings.controller_interval_s)
    try:
        await stop.wait()
    finally:
        await runner.stop(holder)
        await close_state(holder.state)
        for sig in signals:
            asyncio.get_running_loop().remove_signal_handler(sig)
        log.info("controllers stopped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.controllers",
        description="Контроллеры портала (рабочие нагрузки и бэкенды LiteLLM) в отдельном процессе. "
        "Настройки берутся из переменных окружения, как у портала.",
    )
    parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per probe every interval otherwise
    asyncio.run(run())


if __name__ == "__main__":
    main()
