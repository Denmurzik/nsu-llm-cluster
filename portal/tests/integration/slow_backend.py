"""Slow OpenAI-compatible streaming backend that counts its concurrent requests (pure Starlette).

Runs in a bare python:3.12-slim container (`pip install starlette uvicorn`):
    python slow_backend.py --port 18080 --delay-ms 100 --words 20
GET /stats -> {"in_flight", "max_in_flight", "total"}; POST /stats/reset zeroes max and total.
"""

import argparse
import asyncio
import json
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


def create_app(delay_ms: float, words: int) -> Starlette:
    delay = delay_ms / 1000
    stats = {"in_flight": 0, "max_in_flight": 0, "total": 0}

    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    async def get_stats(request: Request):
        return JSONResponse(stats)

    async def reset(request: Request):
        stats.update(max_in_flight=stats["in_flight"], total=0)
        return JSONResponse(stats)

    async def chat(request: Request):
        body = await request.json()
        model = body.get("model", "slow")
        stats["in_flight"] += 1
        stats["total"] += 1
        stats["max_in_flight"] = max(stats["max_in_flight"], stats["in_flight"])

        def chunk(delta: dict, finish: str | None = None) -> str:
            payload = {
                "id": "chatcmpl-slow",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n"

        async def events():
            try:
                await asyncio.sleep(delay)
                yield chunk({"role": "assistant", "content": ""})
                for i in range(words):
                    await asyncio.sleep(delay)
                    yield chunk({"content": f" w{i}"})
                yield chunk({}, finish="stop")
                yield "data: [DONE]\n\n"
            finally:
                stats["in_flight"] -= 1

        return StreamingResponse(events(), media_type="text/event-stream")

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/stats", get_stats),
            Route("/stats/reset", reset, methods=["POST"]),
            Route("/v1/chat/completions", chat, methods=["POST"]),
        ]
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Slow counting OpenAI backend")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--delay-ms", type=float, default=100)
    parser.add_argument("--words", type=int, default=20)
    args = parser.parse_args()
    uvicorn.run(create_app(args.delay_ms, args.words), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
