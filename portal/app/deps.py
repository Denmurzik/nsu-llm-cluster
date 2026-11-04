import redis.asyncio as aioredis
import redis.exceptions
from fastapi import Request

from app.config import Settings
from app.k8s import K8sClient
from app.litellm_client import LiteLLMClient
from app.catalog.sources import ModelSources, model_sources
from app.localmodels.store import LocalStore


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_litellm(request: Request) -> LiteLLMClient:
    return request.app.state.litellm


def get_backend_counts(request: Request):
    return request.app.state.backend_counts.get


def get_k8s(request: Request) -> K8sClient:
    return request.app.state.k8s


def get_hf(request: Request) -> ModelSources:
    """The model sources: Hugging Face, and own files for `local/<name>` repositories (app.catalog.sources)."""
    return model_sources(request.app.state)


def get_local_models(request: Request) -> LocalStore:
    return request.app.state.local_models


def get_progress(request: Request):
    """ProgressSource of the load progress (app.deployments.progress), or None."""
    return getattr(request.app.state, "progress", None)


# any of these means Redis cannot be used right now
REDIS_ERRORS = (redis.exceptions.RedisError, OSError, TimeoutError)


def make_redis(url: str) -> aioredis.Redis:
    return aioredis.Redis.from_url(
        url, decode_responses=True, socket_timeout=5, socket_connect_timeout=3, health_check_interval=30
    )


def get_redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis
