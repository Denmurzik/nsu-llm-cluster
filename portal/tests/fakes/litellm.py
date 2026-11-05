"""In-memory LiteLLM for other tasks' tests; mirrors `app.litellm_client.LiteLLMClient`."""

import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import key_fingerprint
from app.litellm_client import (
    ACCESS_PUBLIC,
    Activity,
    GeneratedKey,
    LiteLLMError,
    LiteLLMUnavailable,
    ManagedModel,
    UsageRow,
    access_groups,
)

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class FakeLiteLLM:
    """Switches: `fail_health` makes `health()` return False; `unavailable` makes every other call raise."""

    def __init__(self, reply: str = "Привет из тестовой модели") -> None:
        self.fail_health = False
        self.unavailable = False
        self.reply = reply
        self.users: dict[str, str] = {}
        self.keys: dict[str, dict[str, Any]] = {}  # token hash -> record (with raw "key")
        self.models: dict[str, ManagedModel] = {}
        self.backend_api_key = ""  # like LiteLLMClient.backend_api_key
        self.api_keys: dict[str, str] = {}  # model id -> the backend key the entry was given (add_model, update_model_key)
        self.activity: dict[str, Activity] = {}
        self.usage_rows: list[UsageRow] = []  # what global_usage() reports (admin «Использование»)
        self.usage_asked: list[tuple[date, date]] = []  # the periods global_usage() was asked for
        self.usage_error: Exception | None = None  # raised by global_usage()
        self.chat_calls: list[tuple[str, dict[str, Any]]] = []
        self.key_model_updates: list[tuple[list[str], list[str]]] = []  # (tokens, models) of each update_key_models call
        self.key_limit_updates: list[tuple[str, int | None, int | None]] = []  # (token, rpm, tpm) of each update_key_limits
        self.key_limit_failures: set[str] = set()  # tokens whose update_key_limits raises a LiteLLM 400
        self.block_calls: list[list[str]] = []  # tokens of each block_keys call
        self.user_limits: dict[str, dict[str, int | None]] = {}  # user id -> its own rpm_limit/tpm_limit (SEC-1 rule 7)
        self.user_limit_updates: list[tuple[str, int | None, int | None]] = []  # (user, rpm, tpm) of each user update

    def _guard(self) -> None:
        if self.unavailable:
            raise LiteLLMUnavailable("fake: LiteLLM unavailable")

    async def aclose(self) -> None:
        return None

    async def health(self) -> bool:
        return not self.fail_health and not self.unavailable

    async def ensure_user(
        self, user_id: str, email: str, rpm_limit: int | None = None, tpm_limit: int | None = None
    ) -> None:
        self._guard()
        self.users.setdefault(user_id, email)
        if rpm_limit is not None or tpm_limit is not None:
            await self.update_user_limits(user_id, rpm_limit, tpm_limit)

    async def update_user_limits(self, user_id: str, rpm_limit: int | None, tpm_limit: int | None) -> None:
        """Like LiteLLM v1.106.6: the user's own limits, shared by all its keys (test_litellm_real_user_limits.py)."""
        self._guard()
        self.user_limit_updates.append((user_id, rpm_limit, tpm_limit))
        limits = self.user_limits.setdefault(user_id, {"rpm_limit": None, "tpm_limit": None})
        if rpm_limit is not None:
            limits["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            limits["tpm_limit"] = tpm_limit

    async def generate_key(
        self,
        user_id: str,
        alias: str,
        duration: str | None,
        rpm_limit: int | None,
        tpm_limit: int | None,
        metadata: dict[str, Any],
        models: list[str] | None = None,
    ) -> GeneratedKey:
        self._guard()
        if any(k["alias"] == alias for k in self.keys.values()):
            raise LiteLLMError(400, json.dumps({"error": {"message": f"Key with alias {alias} already exists"}}))
        raw = "sk-" + secrets.token_urlsafe(16)
        token = hashlib.sha256(raw.encode()).hexdigest()
        expires = None
        if duration:
            expires = datetime.now(timezone.utc) + timedelta(seconds=int(duration[:-1]) * _UNITS[duration[-1]])
        self.keys[token] = {
            "key": raw,
            "user_id": user_id,
            "alias": alias,
            "expires": expires,
            "rpm_limit": rpm_limit,
            "tpm_limit": tpm_limit,
            "metadata": metadata,
            "models": list(models) if models is not None else [ACCESS_PUBLIC],
        }
        return GeneratedKey(key=raw, token=token, expires=expires)

    async def delete_keys(self, tokens: list[str]) -> None:
        self._guard()
        for token in tokens:
            self.keys.pop(token, None)

    async def update_key_models(self, tokens: list[str], models: list[str]) -> None:
        self._guard()
        self.key_model_updates.append((list(tokens), list(models)))
        for token in tokens:
            if token in self.keys:
                self.keys[token]["models"] = list(models)

    async def block_keys(self, tokens: list[str]) -> list[str]:
        self._guard()
        self.block_calls.append(list(tokens))
        done = []
        for token in tokens:
            if token in self.keys:
                self.keys[token]["blocked"] = True
                done.append(token)
        return done

    async def unblock_keys(self, tokens: list[str]) -> None:
        self._guard()
        for token in tokens:
            if token in self.keys:
                self.keys[token]["blocked"] = False

    async def update_key_limits(self, token: str, rpm_limit: int | None, tpm_limit: int | None) -> bool:
        self._guard()
        self.key_limit_updates.append((token, rpm_limit, tpm_limit))
        if token in self.key_limit_failures:
            raise LiteLLMError(400, "fake: key update refused")
        if token not in self.keys:
            return False
        if rpm_limit is not None:
            self.keys[token]["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            self.keys[token]["tpm_limit"] = tpm_limit
        return True

    async def user_activity(self, user_id: str, start: date, end: date) -> Activity:
        self._guard()
        return self.activity.get(user_id, Activity())

    async def global_usage(self, start: date, end: date) -> list[UsageRow]:
        self._guard()
        self.usage_asked.append((start, end))
        if self.usage_error:
            raise self.usage_error
        return [r for r in self.usage_rows if start <= r.day <= end]

    async def list_managed_models(self) -> list[ManagedModel]:
        self._guard()
        return list(self.models.values())

    async def add_model(
        self,
        model_id: str,
        model_name: str,
        api_base: str,
        source: str,
        node: str,
        max_input_tokens: int | None,
        max_parallel_requests: int | None = None,
        experimental: bool = False,
        system_prompt: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._guard()
        if model_id in self.models:
            return  # like the client: an entry that is already there counts as added
        key = self.backend_api_key if api_key is None else api_key
        self.api_keys[model_id] = key
        self.models[model_id] = ManagedModel(
            model_id,
            model_name,
            api_base,
            source,
            node,
            max_input_tokens,
            key_fingerprint(key),
            max_parallel_requests=max_parallel_requests,
            access_groups=tuple(access_groups(experimental)),
            system_prompt=system_prompt or "",
        )

    async def update_model_access(self, model_id: str, experimental: bool, system_prompt: str | None) -> None:
        self._guard()
        old = self.models.get(model_id)
        if old is None:
            raise LiteLLMError(404, json.dumps({"error": {"message": f"Model {model_id} not found on proxy."}}))
        self.models[model_id] = replace(
            old, access_groups=tuple(access_groups(experimental)), system_prompt=system_prompt or ""
        )

    async def update_model_key(self, model_id: str, api_key: str | None = None) -> None:
        self._guard()
        old = self.models.get(model_id)
        if old is None:
            raise LiteLLMError(404, json.dumps({"error": {"message": f"Model {model_id} not found on proxy."}}))
        key = self.backend_api_key if api_key is None else api_key
        self.api_keys[model_id] = key
        self.models[model_id] = replace(old, key_fingerprint=key_fingerprint(key))

    async def update_model_limit(self, model_id: str, max_parallel_requests: int) -> None:
        self._guard()
        old = self.models.get(model_id)
        if old is None:
            raise LiteLLMError(404, json.dumps({"error": {"message": f"Model {model_id} not found on proxy."}}))
        self.models[model_id] = replace(old, max_parallel_requests=max_parallel_requests)

    async def delete_model(self, model_id: str) -> None:
        self._guard()
        self.models.pop(model_id, None)

    async def model_group_status(self) -> dict[str, int]:
        self._guard()
        counts: dict[str, int] = {}
        for model in self.models.values():
            counts[model.model_name] = counts.get(model.model_name, 0) + 1
        return counts

    async def stream_chat(self, api_key: str, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        self._guard()
        if not any(k["key"] == api_key for k in self.keys.values()):
            raise LiteLLMError(401, json.dumps({"error": {"message": "Authentication Error, invalid key"}}))
        model = payload.get("model", "fake-model")
        record = next(k for k in self.keys.values() if k["key"] == api_key)
        if record.get("blocked"):
            raise LiteLLMError(401, json.dumps({"error": {"message": "Authentication Error, key is blocked"}}))
        groups = {g for m in self.models.values() if m.model_name == model for g in m.access_groups}
        if groups and not groups & set(record.get("models") or []):  # like LiteLLM: the key's models must name a group
            message = f"The requested model '{model}' is not available for this API key, or the model name is invalid."
            body = {"error": {"message": message, "type": "key_model_access_denied", "param": "model", "code": "403"}}
            raise LiteLLMError(403, json.dumps(body))  # as LiteLLM v1.106.6 answers (test_litellm_real.py)
        self.chat_calls.append((api_key, payload))
        words = self.reply.split()
        for i, word in enumerate(words):
            delta = {"content": word if i == 0 else " " + word}
            chunk = {"object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": delta}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"
