"""Hugging Face client: file listing, GGUF header Range reads, vLLM repo info."""

import asyncio
import logging
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.catalog.gguf import GgufHeader, GgufInvalid, GgufLayout, GgufMeta, GgufTruncated, gguf_layout, parse_gguf, parse_gguf_header

log = logging.getLogger(__name__)

FIRST_RANGE = 2 * 2**20
MAX_RANGE = 32 * 2**20
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class HfNotFound(Exception):
    """Repository or file does not exist (or is not accessible with the configured token)."""


class HfUnavailable(Exception):
    """Hugging Face is unreachable, failed, or returned something unusable."""


@dataclass
class HfFile:
    path: str
    size: int


@dataclass
class GgufChoice:
    file: str  # path of the (first) file, including subfolders
    size_bytes: int  # summed over all shards
    quant: str | None
    shards: list[str]  # all shard paths in order (a single-element list for an unsharded model)
    shard_sizes: list[int] = field(default_factory=list)  # bytes of each shard, same order


@dataclass
class VllmInfo:
    size_bytes: int
    meta: GgufMeta


_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHARD_RE = re.compile(r"^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<total>\d{5})\.gguf$", re.IGNORECASE)
_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:UD-)?(?:IQ\d+_[A-Z0-9_]+|Q\d+_[A-Z0-9_]+|Q\d+|MXFP4(?:_[A-Z0-9]+)*|BF16|F16|F32))"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def parse_quant(path: str) -> str | None:
    name = posixpath.basename(path)
    name = re.sub(r"-\d{5}-of-\d{5}(?=\.gguf$)", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.gguf$", "", name, flags=re.IGNORECASE)
    m = _QUANT_RE.search(name)
    return m.group(1).upper() if m else None


def group_gguf(files: list[HfFile]) -> list[GgufChoice]:
    """Picks model GGUF files (no mmproj/mtp), groups `-0000N-of-0000M` shards under the first shard."""
    singles: list[GgufChoice] = []
    groups: dict[tuple[str, str, str], list[tuple[int, HfFile]]] = {}
    for f in files:
        base = posixpath.basename(f.path)
        if not base.lower().endswith(".gguf") or base.startswith(("mmproj", "mtp-")):
            continue
        m = _SHARD_RE.match(base)
        if m:
            key = (posixpath.dirname(f.path), m["base"], m["total"])
            groups.setdefault(key, []).append((int(m["idx"]), f))
        else:
            singles.append(GgufChoice(f.path, f.size, parse_quant(f.path), [f.path], [f.size]))
    for parts in groups.values():
        parts.sort(key=lambda p: p[0])
        paths = [f.path for _, f in parts]
        sizes = [f.size for _, f in parts]
        singles.append(GgufChoice(paths[0], sum(sizes), parse_quant(paths[0]), paths, sizes))
    singles.sort(key=lambda c: c.file)
    return singles


class HfClient:
    def __init__(self, endpoint: str, token: str | None, transport: httpx.AsyncBaseTransport | None = None):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        return httpx.AsyncClient(
            transport=self.transport, headers=headers, follow_redirects=True, timeout=_TIMEOUT
        )

    def _next_page(self, resp: httpx.Response) -> str | None:
        """The `rel=next` link, only when it stays on the endpoint's host: the client sends the token with every request."""
        link = resp.links.get("next", {}).get("url")
        if not link:
            return None
        url = urljoin(str(resp.url), link)
        if urlsplit(url).netloc.lower() != urlsplit(self.endpoint).netloc.lower():
            log.warning("ignoring a pagination link to another host: %s", url)
            return None
        return url

    @staticmethod
    def _check(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403, 404):
            raise HfNotFound(f"{resp.request.url}: {resp.status_code}")
        if resp.status_code >= 400:
            raise HfUnavailable(f"{resp.request.url}: {resp.status_code}")

    async def list_files(self, repo: str) -> list[HfFile]:
        url: str | None = f"{self.endpoint}/api/models/{repo}/tree/main?recursive=true"
        files: list[HfFile] = []
        try:
            async with self._client() as client:
                while url:
                    resp = await client.get(url)
                    self._check(resp)
                    for entry in resp.json():
                        if entry.get("type") == "file":
                            files.append(HfFile(entry["path"], int(entry.get("size") or 0)))
                    url = self._next_page(resp)
        except httpx.HTTPError as e:
            raise HfUnavailable(str(e)) from e
        except ValueError as e:  # bad JSON
            raise HfUnavailable(f"unexpected Hugging Face response: {e}") from e
        return files

    async def gguf_files(self, repo: str) -> list[GgufChoice]:
        return group_gguf(await self.list_files(repo))

    async def commit(self, repo: str) -> str:
        """The commit sha `main` of the repository points at now (pinned in the deployment, task 2-15)."""
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.endpoint}/api/models/{repo}/revision/main")
                self._check(resp)
                sha = resp.json().get("sha")
        except httpx.HTTPError as e:
            raise HfUnavailable(str(e)) from e
        except (ValueError, AttributeError) as e:  # bad JSON
            raise HfUnavailable(f"unexpected Hugging Face response: {e}") from e
        if not isinstance(sha, str) or not _COMMIT_RE.fullmatch(sha):
            raise HfUnavailable(f"no commit for {repo}: {sha!r}")
        return sha

    async def _read_range(self, client: httpx.AsyncClient, url: str, length: int, start: int = 0) -> bytes:
        """Reads at most `length` bytes from `start` (also when the server ignores Range and sends the whole file)."""
        async with client.stream("GET", url, headers={"Range": f"bytes={start}-{start + length - 1}"}) as resp:
            self._check(resp)
            skip = start if resp.status_code == 200 else 0
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) >= skip + length:
                    break
            return bytes(buf[skip : skip + length])

    async def _full_header(self, client: httpx.AsyncClient, repo: str, file: str) -> GgufHeader:
        """The whole header (tokenizer and tensor table included); each request continues where the last one ended."""
        url = f"{self.endpoint}/{repo}/resolve/main/{quote(file)}"
        data = bytearray()
        want = FIRST_RANGE
        while True:
            chunk = await self._read_range(client, url, want - len(data), len(data))
            data += chunk
            try:
                return parse_gguf(bytes(data))
            except GgufTruncated as e:
                if len(data) < want:
                    raise HfUnavailable(f"{file}: file ended inside the GGUF header") from e
                if want >= MAX_RANGE:
                    raise HfUnavailable(f"{file}: GGUF header is larger than {MAX_RANGE} bytes") from e
                want = min(MAX_RANGE, max(e.needed, 2 * want))

    async def gguf_inspect(self, repo: str, shards: list[HfFile]) -> tuple[GgufMeta, GgufLayout | None]:
        """Model metadata from the first shard and the layer layout over all shards (headers read in parallel).

        The layout is None when a shard size is unknown: the last tensor of a shard is sized by the end of the file."""
        try:
            async with self._client() as client:
                headers = await asyncio.gather(*(self._full_header(client, repo, f.path) for f in shards))
        except httpx.HTTPError as e:
            raise HfUnavailable(str(e)) from e
        meta = headers[0].meta
        if meta is None:
            raise GgufInvalid("GGUF header lacks required architecture keys")
        if any(f.size <= 0 for f in shards):
            return meta, None
        return meta, gguf_layout(list(zip(headers, (f.size for f in shards), strict=True)), meta.n_layers)

    async def gguf_meta(self, repo: str, file: str) -> GgufMeta:
        url = f"{self.endpoint}/{repo}/resolve/main/{quote(file)}"
        length = FIRST_RANGE
        try:
            async with self._client() as client:
                while True:
                    data = await self._read_range(client, url, length)
                    try:
                        return parse_gguf_header(data)
                    except GgufTruncated as e:
                        if len(data) < length:
                            raise HfUnavailable(f"{file}: file ended inside the GGUF header") from e
                        if length >= MAX_RANGE:
                            raise HfUnavailable(f"{file}: GGUF header is larger than {MAX_RANGE} bytes") from e
                        length = min(MAX_RANGE, max(e.needed, 2 * length))
        except httpx.HTTPError as e:
            raise HfUnavailable(str(e)) from e

    async def vllm_info(self, repo: str) -> VllmInfo:
        files = await self.list_files(repo)
        size = sum(f.size for f in files if f.path.endswith(".safetensors"))
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.endpoint}/{repo}/resolve/main/config.json")
                self._check(resp)
                cfg = resp.json()
        except httpx.HTTPError as e:
            raise HfUnavailable(str(e)) from e
        except ValueError as e:
            raise HfUnavailable(f"config.json is not valid JSON: {e}") from e
        return VllmInfo(size, _meta_from_config(cfg))


def _meta_from_config(cfg: dict) -> GgufMeta:
    text = cfg.get("text_config") or {}

    def get(key: str):
        return text[key] if text.get(key) is not None else cfg.get(key)

    try:
        n_layers = int(get("num_hidden_layers"))
        n_heads = int(get("num_attention_heads"))
        hidden = int(get("hidden_size"))
        max_ctx = int(get("max_position_embeddings"))
    except (TypeError, ValueError) as e:
        raise HfUnavailable("config.json lacks the model dimensions") from e
    n_kv_heads = int(get("num_key_value_heads") or n_heads)
    head_dim = int(get("head_dim") or hidden // n_heads)
    arch = str(text.get("model_type") or cfg.get("model_type") or "")
    return GgufMeta(arch, n_layers, n_heads, n_kv_heads, head_dim, hidden, max_ctx)


class MirrorFirstHf:
    """The inspector through the Hugging Face mirror on main (cached metadata, files put there by hand), and Hugging
    Face directly when the mirror cannot answer (its pod is down): a warning, not a 502 for the admin. A "not found"
    from the mirror stands, because the mirror has already asked Hugging Face."""

    def __init__(self, mirror: HfClient, direct: HfClient):
        self.mirror = mirror
        self.direct = direct

    async def _call(self, method: str, *args):
        try:
            return await getattr(self.mirror, method)(*args)
        except HfUnavailable as e:
            log.warning("HF mirror %s unavailable (%s): asking %s directly", self.mirror.endpoint, e, self.direct.endpoint)
            return await getattr(self.direct, method)(*args)

    async def list_files(self, repo: str) -> list[HfFile]:
        return await self._call("list_files", repo)

    async def gguf_files(self, repo: str) -> list[GgufChoice]:
        return await self._call("gguf_files", repo)

    async def commit(self, repo: str) -> str:
        return await self._call("commit", repo)

    async def gguf_inspect(self, repo: str, shards: list[HfFile]) -> tuple[GgufMeta, GgufLayout | None]:
        return await self._call("gguf_inspect", repo, shards)

    async def gguf_meta(self, repo: str, file: str) -> GgufMeta:
        return await self._call("gguf_meta", repo, file)

    async def vllm_info(self, repo: str) -> VllmInfo:
        return await self._call("vllm_info", repo)
