"""LiteLLM admin client (keys, users, activity, managed models, streaming chat).

Errors: connect/timeout failures and 5xx responses raise `LiteLLMUnavailable`;
any other non-2xx response raises `LiteLLMError(status, body)`.

Field mapping for `/user/daily/activity` (verified against LiteLLM v1.106.6 in
`tests/integration/test_litellm_real.py`):
- response `results[]` holds one entry per day: `date` (YYYY-MM-DD), `metrics`
  (`prompt_tokens`, `completion_tokens`, `total_tokens`, `api_requests`, ...) and
  `breakdown.api_keys[<key hash>].metrics` with the same metric names;
- `Activity.prompt_tokens/completion_tokens/requests` sum the daily metrics
  (`api_requests` is the request count);
- `Activity.by_key` is keyed by the key hash (== `GeneratedKey.token`), tokens = `total_tokens`;
- `Activity.by_day` has one `DayUsage` per returned day, tokens = `total_tokens`;
- paging: `metadata.has_more` / `page` (page_size up to 1000) is followed until exhausted.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import httpx

from app.config import key_fingerprint

# LiteLLM access groups: `model_info.access_groups` of a model and `models` of a key (a key lists group names)
ACCESS_PUBLIC = "public"
ACCESS_EXPERIMENTAL = "experimental"

ADMIN_TIMEOUT = 10.0
STREAM_TIMEOUT = 600.0
# chat streams have their own connection pool: a full pool never delays admin and key calls, and a request
# that finds no free connection within STREAM_POOL_TIMEOUT gets LiteLLMBusy instead of waiting for minutes
STREAM_CONNECTIONS = 1000
STREAM_KEEPALIVE = 100
STREAM_POOL_TIMEOUT = 5.0
_PAGE_SIZE = 1000
_MAX_PAGES = 50


class LiteLLMError(Exception):
    def __init__(self, status: int, body: str, retry_after: str | None = None):
        super().__init__(f"LiteLLM returned {status}: {body[:500]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after  # the Retry-After header of a 429


class LiteLLMUnavailable(Exception):
    """LiteLLM cannot be reached or answers with a server error."""


class LiteLLMBusy(Exception):
    """Every connection of this process's stream pool to LiteLLM is in use (too many concurrent chats)."""


@dataclass(frozen=True)
class GeneratedKey:
    key: str
    token: str
    expires: datetime | None


@dataclass(frozen=True)
class KeyUsage:
    tokens: int
    requests: int


@dataclass(frozen=True)
class DayUsage:
    day: date
    tokens: int
    requests: int


@dataclass(frozen=True)
class Activity:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    requests: int = 0
    by_key: dict[str, KeyUsage] = field(default_factory=dict)
    by_day: list[DayUsage] = field(default_factory=list)


@dataclass(frozen=True)
class UsageRow:
    """One day of one model used by one user (all keys of the user summed): the cube of the admin «Использование»."""

    day: date
    model: str  # the public model name (`model_group`), "" when LiteLLM did not name one
    user_id: str  # the LiteLLM user (the portal user's id), "" for a deleted key whose owner LiteLLM cannot recover
    user_email: str  # what LiteLLM knows about the owner, "" when nothing
    prompt_tokens: int
    completion_tokens: int
    requests: int  # api_requests of LiteLLM: failed ones (denied, errors) count too, like in the user's own usage


@dataclass(frozen=True)
class ManagedModel:
    id: str
    model_name: str
    api_base: str
    source: str
    node: str
    max_input_tokens: int | None
    key_fingerprint: str = ""  # fingerprint of the backend key the entry was registered with (`model_info.backend_auth`)
    max_parallel_requests: int | None = None  # LiteLLM's in-flight bound per worker (`model_info`)
    access_groups: tuple[str, ...] = ()  # `model_info.access_groups`: who may call the model
    system_prompt: str = ""  # `model_info.system_prompt`: the gateway adds it to chats that have none (never logged)


def access_groups(experimental: bool) -> list[str]:
    """`model_info.access_groups` of a portal-managed model."""
    return [ACCESS_EXPERIMENTAL] if experimental else [ACCESS_PUBLIC]


def models_for_role(role: str) -> list[str]:
    """`models` of a key by its owner's role: admins also see the experimental models."""
    return [ACCESS_PUBLIC, ACCESS_EXPERIMENTAL] if role == "admin" else [ACCESS_PUBLIC]


def _parse_expires(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _int(value: Any) -> int:
    return int(value or 0)


def _looks_like_already_exists(body: str) -> bool:
    text = body.lower()
    return "already exists" in text or "unique constraint" in text or "duplicate key" in text


class LiteLLMClient:
    def __init__(
        self,
        base_url: str,
        master_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        backend_api_key: str = "",
        stream_connections: int = STREAM_CONNECTIONS,
        stream_pool_timeout: float = STREAM_POOL_TIMEOUT,
    ):
        self.base_url = base_url
        self.master_key = master_key
        self.backend_api_key = backend_api_key
        self.transport = transport
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=ADMIN_TIMEOUT, transport=transport, trust_env=False
        )
        self._stream_http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(STREAM_TIMEOUT, connect=ADMIN_TIMEOUT, pool=stream_pool_timeout),
            limits=httpx.Limits(
                max_connections=stream_connections, max_keepalive_connections=min(STREAM_KEEPALIVE, stream_connections)
            ),
            transport=transport,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._stream_http.aclose()

    # -- transport helpers -------------------------------------------------

    @property
    def _admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.master_key}"}

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Admin request; only transport failures raise here."""
        try:
            return await self._http.request(method, path, headers=self._admin_headers, **kwargs)
        except httpx.TransportError as exc:
            raise LiteLLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _check(resp: httpx.Response) -> httpx.Response:
        if resp.is_success:
            return resp
        if resp.status_code >= 500:
            raise LiteLLMUnavailable(f"LiteLLM returned {resp.status_code}: {resp.text[:300]}")
        raise LiteLLMError(resp.status_code, resp.text)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self._check(await self._send(method, path, **kwargs))
        return resp.json() if resp.content else None

    # -- health, users, keys -----------------------------------------------

    async def health(self) -> bool:
        try:
            resp = await self._http.get("/health/readiness")
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def ensure_user(
        self, user_id: str, email: str, rpm_limit: int | None = None, tpm_limit: int | None = None
    ) -> None:
        """Creates the LiteLLM user (an existing one is fine). With limits, the user gets them as its own `rpm_limit` /
        `tpm_limit`: LiteLLM v1.106.6 counts them over all keys of the user together (parallel_request_limiter_v3,
        descriptor `user`), so five keys and the Playground share one budget. An existing user gets them by update."""
        body: dict[str, Any] = {
            "user_id": user_id,
            "user_email": email,
            "user_role": "internal_user",
            "auto_create_key": False,
        }
        if rpm_limit is not None:
            body["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            body["tpm_limit"] = tpm_limit
        resp = await self._send("POST", "/user/new", json=body)
        if not resp.is_success and _looks_like_already_exists(resp.text):
            if rpm_limit is not None or tpm_limit is not None:
                await self.update_user_limits(user_id, rpm_limit, tpm_limit)
            return
        self._check(resp)

    async def update_user_limits(self, user_id: str, rpm_limit: int | None, tpm_limit: int | None) -> None:
        """Sets the user-level request and token limits (shared by all the user's keys); a limit given as None stays.
        v1.106.6 answers 200 for a user it does not know as well."""
        body: dict[str, Any] = {"user_id": user_id}
        if rpm_limit is not None:
            body["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            body["tpm_limit"] = tpm_limit
        await self._call("POST", "/user/update", json=body)

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
        """`models` are access group names (`models_for_role`); without them the key reaches only the public group."""
        body: dict[str, Any] = {
            "user_id": user_id,
            "key_alias": alias,
            "models": list(models) if models is not None else [ACCESS_PUBLIC],
            "key_type": "llm_api",
            "metadata": metadata,
        }
        if duration is not None:
            body["duration"] = duration
        if rpm_limit is not None:
            body["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            body["tpm_limit"] = tpm_limit
        data = await self._call("POST", "/key/generate", json=body)
        return GeneratedKey(key=data["key"], token=data["token"], expires=_parse_expires(data.get("expires")))

    async def delete_keys(self, tokens: list[str]) -> None:
        if not tokens:
            return
        resp = await self._send("POST", "/key/delete", json={"keys": tokens})
        if resp.status_code == 404:  # "No keys found": already deleted
            return
        self._check(resp)

    async def update_key_models(self, tokens: list[str], models: list[str]) -> None:
        """Gives existing keys (by token hash) another `models` list at once; the key values stay valid."""
        for token in tokens:
            resp = await self._send("POST", "/key/update", json={"key": token, "models": list(models)})
            if resp.status_code == 404:  # a key the owner deleted meanwhile
                continue
            self._check(resp)

    # -- admin: blocking and limits of a user's keys (task 2-32) -------------------

    async def block_keys(self, tokens: list[str]) -> list[str]:
        """Disables keys (`/key/block`): the key values stay and `unblock_keys` brings them back. Returns the tokens
        that were blocked. When one call fails, the keys blocked so far are released again and the error is raised, so
        a failed block never leaves a half-blocked user."""
        done: list[str] = []
        try:
            for token in tokens:
                resp = await self._send("POST", "/key/block", json={"key": token})
                if resp.status_code == 404:  # deleted meanwhile
                    continue
                self._check(resp)
                done.append(token)
        except (LiteLLMError, LiteLLMUnavailable):
            for token in done:
                try:
                    await self._send("POST", "/key/unblock", json={"key": token})
                except Exception:  # best effort: the original error is the one to report
                    pass
            raise
        return done

    async def unblock_keys(self, tokens: list[str]) -> None:
        """Enables keys that `block_keys` disabled; a key that is gone (deleted) is skipped."""
        for token in tokens:
            resp = await self._send("POST", "/key/unblock", json={"key": token})
            if resp.status_code == 404:
                continue
            self._check(resp)

    async def update_key_limits(self, token: str, rpm_limit: int | None, tpm_limit: int | None) -> bool:
        """Sets the request and token limits of one existing key (by token hash); a limit given as None stays.
        False for a key LiteLLM no longer has (the owner deleted it meanwhile)."""
        body: dict[str, Any] = {"key": token}
        if rpm_limit is not None:
            body["rpm_limit"] = rpm_limit
        if tpm_limit is not None:
            body["tpm_limit"] = tpm_limit
        resp = await self._send("POST", "/key/update", json=body)
        if resp.status_code == 404:
            return False
        self._check(resp)
        return True

    # -- usage ---------------------------------------------------------------

    async def user_activity(self, user_id: str, start: date, end: date) -> Activity:
        prompt = completion = requests = 0
        by_key: dict[str, list[int]] = {}
        by_day: list[DayUsage] = []
        for page in range(1, _MAX_PAGES + 1):
            data = await self._call(
                "GET",
                "/user/daily/activity",
                params={
                    "user_id": user_id,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "page": page,
                    "page_size": _PAGE_SIZE,
                },
            )
            for row in data.get("results", []):
                metrics = row.get("metrics") or {}
                prompt += _int(metrics.get("prompt_tokens"))
                completion += _int(metrics.get("completion_tokens"))
                requests += _int(metrics.get("api_requests"))
                by_day.append(
                    DayUsage(
                        day=date.fromisoformat(str(row["date"])[:10]),
                        tokens=_int(metrics.get("total_tokens")),
                        requests=_int(metrics.get("api_requests")),
                    )
                )
                for key_hash, entry in ((row.get("breakdown") or {}).get("api_keys") or {}).items():
                    key_metrics = entry.get("metrics") or {}
                    totals = by_key.setdefault(key_hash, [0, 0])
                    totals[0] += _int(key_metrics.get("total_tokens"))
                    totals[1] += _int(key_metrics.get("api_requests"))
            if not (data.get("metadata") or {}).get("has_more"):
                break
        return Activity(
            prompt_tokens=prompt,
            completion_tokens=completion,
            requests=requests,
            by_key={k: KeyUsage(tokens=v[0], requests=v[1]) for k, v in by_key.items()},
            by_day=sorted(by_day, key=lambda d: d.day),
        )

    async def global_usage(self, start: date, end: date) -> list[UsageRow]:
        """Usage of every user, per day, model and user (admins; `GET /user/daily/activity` without `user_id`).

        Verified against LiteLLM v1.106.6 (`tests/integration/test_litellm_real.py`): without `user_id` the master key
        gets the global view; `results[]` is one entry per day, `breakdown.model_groups[<model name>]` holds the totals
        of the model and `.api_key_breakdown[<key hash>]` the same per key with `metadata.user_id`/`user_email`
        (`models` is keyed by the backend's `openai/<id>` name, so it is not used unless a day has no groups).
        Paging counts database rows (user x key x model x day), not days: a day can be split between pages, so rows
        are summed by (day, model, user) after the last page.
        """
        cube: dict[tuple[date, str, str], list[Any]] = {}
        for page in range(1, _MAX_PAGES + 1):
            data = await self._call(
                "GET",
                "/user/daily/activity",
                params={
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "page": page,
                    "page_size": _PAGE_SIZE,
                },
            )
            for row in (data or {}).get("results", []):
                day = date.fromisoformat(str(row["date"])[:10])
                breakdown = row.get("breakdown") or {}
                groups = breakdown.get("model_groups") or breakdown.get("models") or {}
                for model, entry in groups.items():
                    for entry_key in (entry.get("api_key_breakdown") or {}).values():
                        metrics = entry_key.get("metrics") or {}
                        meta = entry_key.get("metadata") or {}
                        slot = cube.setdefault(
                            (day, str(model or ""), str(meta.get("user_id") or "")), [0, 0, 0, ""]
                        )
                        slot[0] += _int(metrics.get("prompt_tokens"))
                        slot[1] += _int(metrics.get("completion_tokens"))
                        slot[2] += _int(metrics.get("api_requests"))
                        slot[3] = slot[3] or str(meta.get("user_email") or "")
            if not ((data or {}).get("metadata") or {}).get("has_more"):
                break
        return [
            UsageRow(day, model, user_id, v[3], v[0], v[1], v[2])
            for (day, model, user_id), v in sorted(cube.items(), key=lambda kv: kv[0])
        ]

    # -- managed models --------------------------------------------------------

    async def _model_info(self) -> list[dict[str, Any]]:
        data = await self._call("GET", "/model/info")
        return list((data or {}).get("data") or [])

    async def list_managed_models(self) -> list[ManagedModel]:
        models = []
        for item in await self._model_info():
            info = item.get("model_info") or {}
            if info.get("portal_managed") is not True:
                continue
            models.append(
                ManagedModel(
                    id=info["id"],
                    model_name=item["model_name"],
                    api_base=(item.get("litellm_params") or {}).get("api_base", ""),
                    source=info.get("source", ""),
                    node=info.get("node", ""),
                    max_input_tokens=info.get("max_input_tokens"),
                    key_fingerprint=info["backend_auth"] if isinstance(info.get("backend_auth"), str) else "",
                    max_parallel_requests=info.get("max_parallel_requests"),
                    access_groups=tuple(info["access_groups"]) if isinstance(info.get("access_groups"), list) else (),
                    system_prompt=info["system_prompt"] if isinstance(info.get("system_prompt"), str) else "",
                )
            )
        return models

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
        """`max_parallel_requests` bounds the requests LiteLLM sends to this backend at once (per LiteLLM
        worker); over the bound LiteLLM tries another backend of the model, then answers 429.
        `experimental` puts the model into the access group of that name (keys of admins only), else into `public`;
        `system_prompt` is what main/litellm/system_prompt_hook.py adds to chats that have no system message.
        `api_key`: the backend's own key (the node's, app.nodes.identity); None: the shared key of before."""
        key = self.backend_api_key if api_key is None else api_key
        info: dict[str, Any] = {
            "id": model_id,
            "portal_managed": True,
            "source": source,
            "node": node,
            "access_groups": access_groups(experimental),
        }
        if system_prompt:
            info["system_prompt"] = system_prompt
        if key:
            info["backend_auth"] = key_fingerprint(key)
        if max_input_tokens is not None:
            info["max_input_tokens"] = max_input_tokens
        params: dict[str, Any] = {
            "model": f"openai/{model_name}",
            "api_base": api_base,
            "api_key": key or "none",
        }
        if max_parallel_requests is not None:
            params["max_parallel_requests"] = max_parallel_requests
            info["max_parallel_requests"] = max_parallel_requests
        resp = await self._send(
            "POST", "/model/new", json={"model_name": model_name, "litellm_params": params, "model_info": info}
        )
        if not resp.is_success and _looks_like_already_exists(resp.text):
            return  # the entry is there already (added earlier through another LiteLLM worker)
        self._check(resp)

    async def update_model_key(self, model_id: str, api_key: str | None = None) -> None:
        """Gives an existing entry the backend's current key (`api_key`, the node's own; None: the shared key of
        before), keeping its id and the rest of its settings.

        `PATCH /model/{id}/update` merges `litellm_params` and `model_info`, and the router reloads the entry, so
        requests keep flowing to the backend while the key changes (no delete, no gap).
        """
        key = self.backend_api_key if api_key is None else api_key
        await self._call(
            "PATCH",
            f"/model/{model_id}/update",
            json={
                "litellm_params": {"api_key": key or "none"},
                "model_info": {"backend_auth": key_fingerprint(key)},
            },
        )

    async def update_model_limit(self, model_id: str, max_parallel_requests: int) -> None:
        """Gives an existing entry another in-flight bound in place (`PATCH /model/{id}/update`, like the key).

        LiteLLM v1.106.6 builds the bound's counter lazily and keeps it in the router's in-memory cache
        (default TTL 600 s, router_utils/client_initalization_utils.py), so a running worker applies the new
        value within 10 minutes.
        """
        await self._call(
            "PATCH",
            f"/model/{model_id}/update",
            json={
                "litellm_params": {"max_parallel_requests": max_parallel_requests},
                "model_info": {"max_parallel_requests": max_parallel_requests},
            },
        )

    async def update_model_access(self, model_id: str, experimental: bool, system_prompt: str | None) -> None:
        """Gives an existing entry its access group and system prompt in place (`PATCH /model/{id}/update`).

        The PATCH merges `model_info`, so a cleared prompt is written as an empty string (the hook ignores it).
        """
        await self._call(
            "PATCH",
            f"/model/{model_id}/update",
            json={"model_info": {"access_groups": access_groups(experimental), "system_prompt": system_prompt or ""}},
        )

    async def delete_model(self, model_id: str) -> None:
        resp = await self._send("POST", "/model/delete", json={"id": model_id})
        # v1.106.6 answers 400 "Model with id=... not found in db" for an unknown id
        if resp.status_code == 404 or (resp.status_code == 400 and "not found" in resp.text.lower()):
            return
        self._check(resp)

    async def model_group_status(self) -> dict[str, int]:
        """Model name -> number of deployments in the router.

        The portal registers only backends whose `/health` answers and removes the rest, so every
        registered deployment counts as healthy. LiteLLM's own `/health` is not used here: it sends
        real completions to every backend.
        """
        counts: dict[str, int] = {}
        for item in await self._model_info():
            name = item["model_name"]
            counts[name] = counts.get(name, 0) + 1
        return counts

    # -- chat ------------------------------------------------------------------

    async def stream_chat(self, api_key: str, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        """Streams a chat completion; LiteLLMBusy when no pooled connection frees up within the pool timeout."""
        try:
            async with self._stream_http.stream(
                "POST",
                "/v1/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            ) as resp:
                if not resp.is_success:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    if resp.status_code >= 500:
                        raise LiteLLMUnavailable(f"LiteLLM returned {resp.status_code}: {body[:300]}")
                    raise LiteLLMError(resp.status_code, body, retry_after=resp.headers.get("retry-after"))
                async for chunk in resp.aiter_raw():
                    yield chunk
        except httpx.PoolTimeout as exc:
            raise LiteLLMBusy("no free connection to LiteLLM") from exc
        except httpx.TransportError as exc:
            raise LiteLLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
