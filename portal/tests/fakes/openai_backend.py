"""Fake OpenAI-compatible backend for tests (pure Starlette, no fastapi: it also runs in a bare
python:3.12-slim container with `pip install starlette uvicorn`).

Run as a file or as a module:
    python tests/fakes/openai_backend.py --port 18099 --model-id fake-model
    python -m tests.fakes.openai_backend --port 18099
"""

import argparse
import asyncio
import json
import math
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


def _count_tokens(text: str) -> int:
    return len(text.split())


def create_fake_openai_app(
    model_id: str = "fake-model",
    reply: str = "Привет из тестовой модели",
    delay_ms: float = 0,
    fill_max_tokens: bool = False,
) -> Starlette:
    """`delay_ms` pauses before the first chunk (time to first token) and before every token, so a
    stream lasts about (words + 1) * delay_ms; `fill_max_tokens` cycles the reply up to the request's
    `max_tokens` words (load tests use it to get long, predictable streams)."""
    delay = max(delay_ms, 0) / 1000

    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    async def list_models(request: Request):
        return JSONResponse(
            {"object": "list", "data": [{"id": model_id, "object": "model", "created": 0, "owned_by": "fake"}]}
        )

    seen: dict = {"auth": "", "messages": []}

    async def last_auth(request: Request):
        return JSONResponse({"authorization": seen["auth"]})

    async def last_messages(request: Request):
        return JSONResponse({"messages": seen["messages"]})

    async def chat_completions(request: Request):
        seen["auth"] = request.headers.get("authorization", "")
        body = await request.json()
        seen["messages"] = body.get("messages", [])
        prompt_text = " ".join(
            str(m.get("content", "")) for m in body.get("messages", []) if isinstance(m.get("content", ""), str)
        )
        prompt_tokens = max(_count_tokens(prompt_text), 1)
        words = reply.split()
        max_tokens = body.get("max_tokens")
        if fill_max_tokens and words and isinstance(max_tokens, int) and max_tokens > 0:
            words = (words * math.ceil(max_tokens / len(words)))[:max_tokens]
        completion_tokens = len(words)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        completion_id = "chatcmpl-fake"
        created = int(time.time())

        if not body.get("stream"):
            if delay:
                await asyncio.sleep(delay * len(words))
            return JSONResponse(
                {
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model_id,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": " ".join(words)}, "finish_reason": "stop"}
                    ],
                    "usage": usage,
                }
            )

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

        def chunk(delta: dict, finish: str | None = None, with_usage: bool = False) -> str:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_id,
                "choices": [] if with_usage else [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if with_usage:
                payload["usage"] = usage
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def events():
            if delay:
                await asyncio.sleep(delay)
            yield chunk({"role": "assistant", "content": ""})
            for i, word in enumerate(words):
                if delay:
                    await asyncio.sleep(delay)
                yield chunk({"content": word if i == 0 else " " + word})
            yield chunk({}, finish="stop")
            if include_usage:
                yield chunk({}, with_usage=True)
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/_debug/last_auth", last_auth),
            Route("/_debug/last_messages", last_messages),
            Route("/v1/models", list_models),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        ]
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Fake OpenAI backend")
    parser.add_argument("--port", type=int, default=18099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-id", default="fake-model")
    parser.add_argument("--delay-ms", type=float, default=0, help="pause before the first chunk and before every token")
    parser.add_argument("--fill-max-tokens", action="store_true", help="cycle the reply up to the request's max_tokens")
    args = parser.parse_args()
    app = create_fake_openai_app(args.model_id, delay_ms=args.delay_ms, fill_max_tokens=args.fill_max_tokens)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
