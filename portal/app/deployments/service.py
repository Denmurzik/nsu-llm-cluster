"""Model deployments: validation, Hugging Face metadata, node assignment, desired config, seed, status."""

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes_asyncio.client.exceptions import ApiException
from pydantic import BaseModel, Field
from sqlalchemy import Text, cast, func, or_, select, update as sql_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import Hook
from app.catalog import placement
from app.catalog.estimate import meta_from_dict, meta_to_dict, node_capacity_bytes
from app.catalog.gguf import GgufInvalid, GgufLayout, GgufMeta
from app.catalog.hf import HfFile, HfNotFound, HfUnavailable
from app.deployments import llama_args
from app.deployments import schedule as sched
from app.deployments.progress import pod_progress
from app.k8s import MAIN_NODE, K8sUnavailable
from app.litellm_client import LiteLLMError, LiteLLMUnavailable
from app.localmodels.store import is_local, local_repo, name_of, name_problem
from app.models import ModelDeployment, NodeAssignment, PortalFlag, StandaloneNode
from app.workloads.builders import (
    ROLE_INFERENCE,
    ROLE_LEADER,
    RpcWorker,
    leader_groups,
    leader_index,
    llama_settings_env,
    main_disk_problem,
    plan_groups,
)

log = logging.getLogger(__name__)

NAME_RE = r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$"
REPO_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
ENGINES = ("llamacpp", "vllm")
MODES = ("replicas", "split")
ACCELS = ("cpu", "nvidia", "any")
MIN_CTX = 256
MAX_PARALLEL = 16
MAX_SPARE_NODES = 100
REASONING_MODES = ("off", "on", "auto")
KV_CACHE_TYPES = ("f16", "q8_0", "q4_0")
MAX_SYSTEM_PROMPT = 8000
MAX_REGROUP_GRACE_S = 86400
# devices of one split group: llama.cpp is built with 64 backends (node/Dockerfile), one of them the leader's CPU
MAX_SPLIT_DEVICES = 63
SEED_META_TIMEOUT_S = 10.0
META_REFRESH_BUDGET_S = 5.0  # all metadata lookups of one reconcile pass together
META_MIN_ATTEMPT_S = 1.0  # less budget than this left: stop, a lookup cut off this short would only be a false failure
META_BACKOFF_START_S = 30.0
META_BACKOFF_MAX_S = 3600.0
SEED_FLAG = "default_deployment_seeded"
STANDALONE_ONLY_REPLICAS = "Standalone-нода поддерживает только replicas на llama.cpp"
_K8S_ERRORS = (K8sUnavailable, ApiException)

STATUS_READY = "ready"
STATUS_STARTING = "starting"
STATUS_DOWN = "down"
STATUS_UNKNOWN = "unknown"


class DeploymentInvalid(Exception):
    """The deployment (or assignment) breaks a rule; `detail` is the Russian message for the admin (HTTP 422)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class DeploymentConflict(DeploymentInvalid):
    """The name is already taken (HTTP 409)."""


class DeploymentNotFound(Exception):
    """No deployment with the requested id (HTTP 404)."""


class NodeNotFound(Exception):
    """No node of that name in the cluster, the standalone registry or the assignments (HTTP 404)."""


class Schedule(BaseModel):
    """Working window, see app.deployments.schedule; checked by `_check_fields` (Russian messages)."""

    days: list[int]
    start: str
    end: str


class DeploymentIn(BaseModel):
    name: str
    engine: str = "llamacpp"
    repo: str
    file: str | None = None
    mode: str = "replicas"
    group_size: int = 2
    accel: str = "any"
    ctx_per_request: int = 8192
    parallel: int = 2
    auto_assign: bool = False
    enabled: bool = True
    spare_nodes: int = 0
    regroup_grace_s: int | None = None
    schedule: Schedule | None = None
    use_node_ram: bool = True
    ram_budget_pct: int = placement.DEFAULT_RAM_BUDGET_PCT
    reasoning: str = "off"
    kv_cache_type: str = "q8_0"  # new deployments; the rows made before the setting keep f16
    sampling: Any = None  # an object; checked by `_check_llm` (Russian messages), not by pydantic
    draft_repo: str | None = None
    draft_file: str | None = None
    mmproj_file: str | None = None
    extra_args: str | None = None
    system_prompt: str | None = None
    experimental: bool = False
    nodes: list[str] = Field(default_factory=list)


class DeploymentPatch(BaseModel):
    """Every field of `DeploymentIn` except `name`; only the fields that were sent are changed."""

    engine: str | None = None
    repo: str | None = None
    file: str | None = None
    mode: str | None = None
    group_size: int | None = None
    accel: str | None = None
    ctx_per_request: int | None = None
    parallel: int | None = None
    auto_assign: bool | None = None
    enabled: bool | None = None
    spare_nodes: int | None = None
    regroup_grace_s: int | None = None  # null: back to the default of the mode
    schedule: Schedule | None = None  # null: around the clock
    use_node_ram: bool | None = None
    ram_budget_pct: int | None = None
    reasoning: str | None = None
    kv_cache_type: str | None = None
    sampling: Any = None  # null or {}: no defaults of its own
    draft_repo: str | None = None  # null: no draft model (clear `draft_file` with it)
    draft_file: str | None = None
    mmproj_file: str | None = None  # null: no images
    extra_args: str | None = None  # null: no own flags
    system_prompt: str | None = None  # null: none
    experimental: bool | None = None
    nodes: list[str] | None = None


@dataclass
class NodeCandidate:
    name: str
    accel: str
    source: str  # "k8s" | "standalone"
    vram_mib: int | None
    mem_mib: int | None


@dataclass
class DesiredConfig:
    deployment: str
    model_repo: str
    model_file: str
    public_model_name: str
    ctx_size: int
    parallel: int
    spec_hash: str
    model_revision: str = ""  # the pinned Hugging Face commit; empty: main (serve-llama.sh MODEL_REVISION)
    backend_api_key: str = ""  # shared backend secret; filled in by the heartbeat route, not part of the spec hash
    # the Hugging Face mirror of main and its key (heartbeat route, not part of the spec hash); empty: the node's own
    # HF_ENDPOINT / HF_TOKEN. The node falls back to those while the mirror does not answer (serve-llama.sh).
    hf_endpoint: str = ""
    hf_token: str = ""
    # the llama.cpp settings of the deployment (reasoning, KV cache, sampling, draft, projector, own flags) as the
    # environment of serve-llama.sh, the same one the k3s pods get (builders.llama_settings_env); only the settings
    # that differ from the defaults. They are part of the spec hash, so a change restarts the server.
    llm_env: dict[str, str] = field(default_factory=dict)


_FIELDS = (
    "engine", "repo", "file", "mode", "group_size", "accel", "ctx_per_request", "parallel", "use_node_ram",
    "ram_budget_pct",
)
_OPERATION_FIELDS = ("spare_nodes", "regroup_grace_s", "schedule")  # never change the pods
# llama.cpp settings (task 2-22): the first group changes the pods, the second only the gateway
_LLM_POD_FIELDS = ("reasoning", "kv_cache_type", "sampling", "draft_repo", "draft_file", "mmproj_file", "extra_args")
_LLM_GATEWAY_FIELDS = ("system_prompt", "experimental")
_LLM_FIELDS = _LLM_POD_FIELDS + _LLM_GATEWAY_FIELDS
_NULLABLE = (
    "file", "regroup_grace_s", "schedule", "sampling", "draft_repo", "draft_file", "mmproj_file", "extra_args",
    "system_prompt",
)  # an explicit null in a PATCH clears these


def _llm_hash_part(dep: Any) -> str:
    """The llama.cpp settings that change the pods, only those that are set: a deployment made before them (reasoning
    off, KV f16, nothing else) keeps its hash and its pods. `system_prompt` and `experimental` are the gateway's:
    they never restart anything."""
    parts = []
    if (reasoning := getattr(dep, "reasoning", None)) and reasoning != "off":
        parts.append(f"reasoning={reasoning}")
    if (kv := getattr(dep, "kv_cache_type", None)) and kv != "f16":
        parts.append(f"kv={kv}")
    if sampling := getattr(dep, "sampling", None):
        parts.append("sampling=" + json.dumps({k: float(v) for k, v in sorted(sampling.items())}, separators=(",", ":")))
    if draft_repo := getattr(dep, "draft_repo", None):
        parts.append(f"draft={draft_repo}|{getattr(dep, 'draft_file', None) or ''}|{getattr(dep, 'draft_revision', None) or ''}")
    if mmproj := getattr(dep, "mmproj_file", None):
        parts.append(f"mmproj={mmproj}")
    if extra := getattr(dep, "extra_args", None):
        parts.append(f"args={extra}")
    return "".join(f"|{p}" for p in parts)


def spec_hash(dep: Any) -> str:
    """Deployment-level hash over the fields that change the pods (not `enabled`, `auto_assign`, name, accel).
    `group_size` and the RAM settings count only in `split` mode."""
    group_size = dep.group_size if dep.mode == "split" else ""  # only a split deployment depends on it
    raw = (
        f"{dep.engine}|{dep.repo}|{dep.file or ''}|{dep.mode}|{group_size}|{dep.ctx_per_request}|{dep.parallel}"
    )
    if dep.mode == "split":
        raw += f"|{dep.use_node_ram}|{dep.ram_budget_pct}"
    revision = getattr(dep, "revision", None)
    if revision:  # only a pinned deployment: one made before the pin (NULL) keeps its hash and its pods
        raw += f"|rev={revision}"
    raw += _llm_hash_part(dep)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ---------- validation and Hugging Face metadata ----------


def _check_fields(f: dict[str, Any]) -> None:
    """Static rules over a full deployment state (no Hugging Face needed)."""
    if not re.fullmatch(NAME_RE, f["name"]):
        raise DeploymentInvalid("Имя: строчные латинские буквы, цифры и дефис, не длиннее 40 символов")
    if not re.fullmatch(REPO_RE, f["repo"]):
        raise DeploymentInvalid("Репозиторий указывается как владелец/название")
    if is_local(f["repo"]) and f["engine"] != "llamacpp":
        raise DeploymentInvalid("Свои файлы моделей запускаются только на llama.cpp")
    if f["engine"] not in ENGINES:
        raise DeploymentInvalid("Движок: llamacpp или vllm")
    if f["mode"] not in MODES:
        raise DeploymentInvalid("Режим: replicas или split")
    if f["accel"] not in ACCELS:
        raise DeploymentInvalid("Ускоритель: cpu, nvidia или any")
    if f["engine"] == "vllm":
        if f["accel"] != "nvidia":
            raise DeploymentInvalid("vLLM работает только на NVIDIA")
        if f["mode"] != "replicas":
            raise DeploymentInvalid("vLLM поддерживает только режим replicas")
        if f["file"] is not None:
            raise DeploymentInvalid("Для vLLM файл не указывается")
    elif not f["file"]:
        raise DeploymentInvalid("Не указан файл модели")
    if f["ctx_per_request"] < MIN_CTX:
        raise DeploymentInvalid(f"Контекст на запрос не меньше {MIN_CTX} токенов")
    if not 1 <= f["parallel"] <= MAX_PARALLEL:
        raise DeploymentInvalid(f"Параллельных запросов от 1 до {MAX_PARALLEL}")
    if f["mode"] == "split":
        if f["group_size"] < 2:
            raise DeploymentInvalid("Размер группы для split не меньше 2")
        # accel any: GPU and CPU nodes in one group, any member may be a GPU node with its devices
        widest = "nvidia" if f["accel"] == "any" else f["accel"]
        per_node = len(placement.rpc_devices(widest, f["use_node_ram"]).split(","))
        if f["group_size"] * per_node > MAX_SPLIT_DEVICES:
            raise DeploymentInvalid(
                f"В группе не больше {MAX_SPLIT_DEVICES} устройств: не больше {MAX_SPLIT_DEVICES // per_node} нод"
                + (" при использовании RAM нод" if per_node > 1 else "")
            )
    if not placement.MIN_RAM_BUDGET_PCT <= f["ram_budget_pct"] <= placement.MAX_RAM_BUDGET_PCT:
        raise DeploymentInvalid(
            f"Доля RAM ноды для модели от {placement.MIN_RAM_BUDGET_PCT} до {placement.MAX_RAM_BUDGET_PCT} %"
        )
    if not 0 <= f["spare_nodes"] <= MAX_SPARE_NODES:
        raise DeploymentInvalid(f"Резервных нод от 0 до {MAX_SPARE_NODES}")
    if f["regroup_grace_s"] is not None and not 0 <= f["regroup_grace_s"] <= MAX_REGROUP_GRACE_S:
        raise DeploymentInvalid(f"Пауза перед пересборкой от 0 до {MAX_REGROUP_GRACE_S} секунд")
    if f["schedule"] is not None and (reason := sched.problem(f["schedule"])):
        raise DeploymentInvalid(reason)
    _check_llm(f)


_FILE_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,250}")  # one part of a file path on the mirror (store._PART)


def _gguf_path_problem(path: str) -> str | None:
    """A `.gguf` file of a repository as a relative posix path of plain parts: letters, digits, `.`, `_`, `+`, `-`;
    no spaces, quotes, `$`, `;`, backticks, `..`, backslashes or absolute paths (the name reaches the node's script)."""
    if not path.lower().endswith(".gguf"):
        return "имя файла заканчивается на .gguf"
    if len(path) > 512 or any(not _FILE_PART.fullmatch(part) or ".." in part for part in path.split("/")):
        return "в имени файла только латинские буквы, цифры, «.», «_», «+» и «-», путь внутри репозитория без «..»"
    return None


def _local_file_problem(repo: str, path: str) -> bool:
    """An own model's file (`local/<model>` + a path in its directory) breaks the rules of the own-model store."""
    return bool(name_problem(name_of(repo), path))


def _check_mmproj(path: str) -> None:
    if is_local(path):  # local/<model>/<path inside the model's directory>: the rules of an own model's file
        model, _, inner = path[len("local/"):].partition("/")
        if not inner or len(path) > 512 or _local_file_problem(local_repo(model), inner) or _gguf_path_problem(inner):
            raise DeploymentInvalid("Файл проектора изображений: local/<модель>/<файл>.gguf без «..» и абсолютных путей")
    elif reason := _gguf_path_problem(path):
        raise DeploymentInvalid(f"Файл проектора изображений: {reason}")


async def _require_file(hf, repo: str, path: str, what: str) -> None:
    """The file is in the repository (Hugging Face or own models): as for the main model, a typo is a 422 now, not a
    restart of every pod into a failed download."""
    if path not in {f.path for f in await hf.list_files(repo)}:
        raise DeploymentInvalid(f"{what} не найден в репозитории")


async def _check_llm_files(hf, merged: dict[str, Any], dep: ModelDeployment | None) -> None:
    """The draft file and the mmproj file exist; only what is new or changed is looked up."""
    if merged["draft_repo"] and (
        dep is None or (merged["draft_repo"], merged["draft_file"]) != (dep.draft_repo, dep.draft_file)
    ):
        await _require_file(hf, merged["draft_repo"], merged["draft_file"], "Файл черновой модели")
    mmproj = merged["mmproj_file"]
    if mmproj and merged["engine"] == "llamacpp" and (dep is None or mmproj != dep.mmproj_file or merged["repo"] != dep.repo):
        if is_local(mmproj):
            model, _, inner = mmproj[len("local/"):].partition("/")
            await _require_file(hf, local_repo(model), inner, "Файл проектора изображений")
        else:
            await _require_file(hf, merged["repo"], mmproj, "Файл проектора изображений")


def _check_llm(f: dict[str, Any]) -> None:
    """The llama.cpp settings of a full deployment state (static rules, Russian messages)."""
    if f.get("reasoning", "off") not in REASONING_MODES:
        raise DeploymentInvalid("Рассуждения: off, on или auto")
    if f.get("kv_cache_type", "f16") not in KV_CACHE_TYPES:
        raise DeploymentInvalid("Тип KV-кэша: f16, q8_0 или q4_0")
    try:
        llama_args.validate_sampling(f.get("sampling"))
        llama_args.validate_extra_args(f.get("extra_args"))
    except (llama_args.SamplingInvalid, llama_args.ExtraArgsInvalid) as e:
        raise DeploymentInvalid(str(e)) from e
    draft_repo, draft_file = f.get("draft_repo"), f.get("draft_file")
    if (draft_repo is None) != (draft_file is None):
        raise DeploymentInvalid("Черновая модель: нужны и репозиторий, и файл")
    if draft_repo is not None:
        if len(draft_repo) > 512 or not re.fullmatch(REPO_RE, draft_repo):
            raise DeploymentInvalid("Репозиторий черновой модели указывается как владелец/название, не длиннее 512 символов")
        if (reason := _gguf_path_problem(draft_file)) or (is_local(draft_repo) and _local_file_problem(draft_repo, draft_file)):
            raise DeploymentInvalid(f"Файл черновой модели: {reason or 'недопустимое имя файла своей модели'}")
    if (mmproj := f.get("mmproj_file")) is not None:
        _check_mmproj(mmproj)
    if len(f.get("system_prompt") or "") > MAX_SYSTEM_PROMPT:
        raise DeploymentInvalid(f"Системный промпт не длиннее {MAX_SYSTEM_PROMPT} символов")


def _normalize_llm(d: dict[str, Any]) -> None:
    """Blank text and an empty sampling object are stored as null (in place, only the keys present)."""
    for key in ("draft_repo", "draft_file", "mmproj_file", "extra_args"):
        if isinstance(d.get(key), str):
            d[key] = d[key].strip() or None
    if isinstance(d.get("system_prompt"), str) and not d["system_prompt"].strip():
        d["system_prompt"] = None
    if "sampling" in d:
        try:
            d["sampling"] = llama_args.validate_sampling(d["sampling"])
        except llama_args.SamplingInvalid as e:
            raise DeploymentInvalid(str(e)) from e


def _meta_dict(weights_bytes: int, meta: GgufMeta, layout: GgufLayout | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"weights_bytes": weights_bytes, **meta_to_dict(meta)}
    if layout is not None:  # bytes per layer for the split placement (app.catalog.placement)
        result |= {
            "layers": layout.layers,
            "output_bytes": layout.output_bytes,
            "embd_bytes": layout.embd_bytes,
            "max_tensor_bytes": layout.max_tensor_bytes,
            "n_vocab": layout.n_vocab,
        }
    return result


gguf_meta_from_dict = meta_from_dict


async def fetch_meta(hf, engine: str, repo: str, file: str | None, layout: bool = False) -> dict[str, Any]:
    """Stored `meta` of a model. Raises DeploymentInvalid for a missing/non-GGUF file; HfNotFound/HfUnavailable propagate.

    With `layout` (split deployments) the whole headers of all shards are read for the bytes of every layer
    (tokenizer and tensor tables, several MB); otherwise only the first window of the first file."""
    if engine == "vllm":
        info = await hf.vllm_info(repo)
        return _meta_dict(info.size_bytes, info.meta)
    if engine != "llamacpp":
        raise DeploymentInvalid("Движок: llamacpp или vllm")
    if not file:
        raise DeploymentInvalid("Не указан файл модели")
    choice = next((c for c in await hf.gguf_files(repo) if c.file == file), None)
    if choice is None:
        raise DeploymentInvalid("Файл не найден в репозитории")
    sizes = choice.shard_sizes or [0] * len(choice.shards)
    try:
        if layout:
            meta, layers = await hf.gguf_inspect(repo, [HfFile(p, n) for p, n in zip(choice.shards, sizes, strict=True)])
        else:
            meta, layers = await hf.gguf_meta(repo, file), None
    except GgufInvalid as e:
        raise DeploymentInvalid("Файл не является GGUF") from e
    return _meta_dict(choice.size_bytes, meta, layers)


def _lacks_layout(dep_or_fields: dict[str, Any], meta: dict[str, Any] | None) -> bool:
    """A llama.cpp split deployment whose stored meta has no layer layout (made before it was read, or in replicas)."""
    return (
        dep_or_fields["mode"] == "split" and dep_or_fields["engine"] == "llamacpp" and meta is not None and "layers" not in meta
    )


def _check_ctx(ctx: int, meta: dict[str, Any] | None) -> None:
    if meta is not None and ctx > meta["max_ctx"]:
        raise DeploymentInvalid(f"Контекст на запрос больше предела модели: {meta['max_ctx']} токенов")


def _state(dep: ModelDeployment) -> dict[str, Any]:
    return {k: getattr(dep, k) for k in ("name", *_FIELDS, *_OPERATION_FIELDS, *_LLM_FIELDS)}


def regroup_grace(dep: ModelDeployment, settings) -> float:
    """Seconds a lost node keeps its place: its deployment label and, in split mode, its group."""
    if dep.regroup_grace_s is not None:
        return float(dep.regroup_grace_s)
    return settings.split_regroup_grace_s if dep.mode == "split" else settings.node_notready_grace_s


def split_group_limit(dep: ModelDeployment, assigned: int) -> int | None:
    """Most split groups `assigned` nodes may form while `spare_nodes` of them stay in reserve; None: no limit."""
    if not dep.spare_nodes:
        return None
    return max(assigned - dep.spare_nodes, 0) // dep.group_size


# ---------- CRUD ----------


async def list_all(db: AsyncSession) -> list[ModelDeployment]:
    return list((await db.scalars(select(ModelDeployment).order_by(ModelDeployment.id))).all())


async def get(db: AsyncSession, dep_id: int) -> ModelDeployment | None:
    return await db.get(ModelDeployment, dep_id)


async def get_by_name(db: AsyncSession, name: str) -> ModelDeployment | None:
    return await db.scalar(select(ModelDeployment).where(ModelDeployment.name == name))


async def assigned_nodes(db: AsyncSession) -> dict[int, list[str]]:
    """deployment id -> sorted names of the nodes assigned to it."""
    rows = await db.execute(
        select(NodeAssignment.deployment_id, NodeAssignment.node_name)
        .where(NodeAssignment.deployment_id.is_not(None))
        .order_by(NodeAssignment.node_name)
    )
    result: dict[int, list[str]] = {}
    for dep_id, node in rows:
        result.setdefault(dep_id, []).append(node)
    return result


async def create(db: AsyncSession, hf, data: DeploymentIn, k8s=None, audit: Hook | None = None) -> ModelDeployment:
    """`audit(dep)` is awaited just before the commit: its audit row is committed with the deployment or not at all."""
    fields = data.model_dump(exclude={"nodes"})
    _normalize_llm(fields)
    _check_fields(fields)
    fields["schedule"] = sched.normalized(fields["schedule"])
    if await get_by_name(db, data.name) is not None:
        raise DeploymentConflict("Развёртывание с таким именем уже есть")
    meta = await fetch_meta(hf, data.engine, data.repo, data.file, layout=data.mode == "split")
    _check_ctx(data.ctx_per_request, meta)
    await _check_llm_files(hf, fields, None)
    revision = await hf.commit(data.repo)
    draft_revision = await hf.commit(fields["draft_repo"]) if fields["draft_repo"] else None
    dep = ModelDeployment(**fields, meta=meta, revision=revision, draft_revision=draft_revision)
    db.add(dep)
    try:
        await db.flush()
    except IntegrityError as e:  # a concurrent request took the name after the check above
        await db.rollback()
        raise DeploymentConflict("Развёртывание с таким именем уже есть") from e
    try:
        accels = await _node_accels(db, k8s) if data.nodes else {}
        for node in sorted({n.strip().lower() for n in data.nodes}):  # one lock order, same as auto_assign
            await _assign(db, node, dep, accels)
        if audit is not None:
            await audit(dep)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return dep


async def update(
    db: AsyncSession, hf, dep_id: int, patch: DeploymentPatch, k8s=None, audit: Hook | None = None
) -> ModelDeployment:
    """`audit(dep)` is awaited just before the commit (one transaction with the change)."""
    dep = await get(db, dep_id)
    if dep is None:
        raise DeploymentNotFound(dep_id)
    sent = patch.model_dump(exclude_unset=True)
    nodes = sent.pop("nodes", None)
    changes = {k: v for k, v in sent.items() if v is not None or k in _NULLABLE}
    _normalize_llm(changes)
    merged = {**_state(dep), **changes}
    _check_fields(merged)
    if "schedule" in changes:
        changes["schedule"] = sched.normalized(changes["schedule"])
    meta = dep.meta
    revision = dep.revision
    draft_revision = dep.draft_revision
    if any(k in changes and changes[k] != getattr(dep, k) for k in ("draft_repo", "draft_file")):
        draft_revision = await hf.commit(merged["draft_repo"]) if merged["draft_repo"] else None
    if any(k in changes and changes[k] != getattr(dep, k) for k in ("engine", "repo", "file")):
        meta = await fetch_meta(hf, merged["engine"], merged["repo"], merged["file"], layout=merged["mode"] == "split")
        revision = await hf.commit(merged["repo"])  # another model: pinned at the commit of main now
    elif _lacks_layout(merged, meta):
        try:  # switched to split: the layer layout is read now; without it the placement assumes equal layers
            meta = await fetch_meta(hf, merged["engine"], merged["repo"], merged["file"], layout=True)
        except (HfNotFound, HfUnavailable, DeploymentInvalid) as e:
            log.warning("layer layout of %s unavailable, split placement assumes equal layers: %s", merged["repo"], e)
    _check_ctx(merged["ctx_per_request"], meta)
    await _check_llm_files(hf, merged, dep)
    accel_changed = "accel" in changes and changes["accel"] != dep.accel
    try:
        for k, v in changes.items():
            setattr(dep, k, v)
        dep.meta = meta
        dep.revision = revision
        dep.draft_revision = draft_revision
        accels = await _node_accels(db, k8s) if nodes or accel_changed else {}
        if nodes is not None:
            await _set_nodes(db, dep, nodes, accels)
        if accel_changed:
            await _release_mismatched(db, dep, accels)
        if dep.mode != "replicas" or dep.engine != "llamacpp":
            standalone = await db.scalar(
                select(func.count()).select_from(NodeAssignment).where(
                    NodeAssignment.deployment_id == dep.id, NodeAssignment.source == "standalone"
                )
            )
            if standalone:
                raise DeploymentInvalid(STANDALONE_ONLY_REPLICAS)
        if audit is not None:
            await audit(dep)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return dep


async def refresh_revision(
    db: AsyncSession, hf, dep_id: int, audit: Hook | None = None
) -> tuple[ModelDeployment, bool]:
    """«Обновить до последней версии»: pins the commit `main` of the repository points at now. A new commit changes
    the spec hash, so the pods reload one node at a time (a split group restarts) and download what changed;
    (deployment, whether the commit changed)."""
    dep = await get(db, dep_id)
    if dep is None:
        raise DeploymentNotFound(dep_id)
    revision = await hf.commit(dep.repo)
    draft_revision = await hf.commit(dep.draft_repo) if dep.draft_repo else None  # the draft model is pinned the same way
    if revision == dep.revision and draft_revision == dep.draft_revision:
        return dep, False
    log.info(
        "deployment %s: revision %s -> %s, draft %s -> %s",
        dep.name, dep.revision or "main", revision or "main", dep.draft_revision or "main", draft_revision or "main",
    )
    dep.revision = revision
    dep.draft_revision = draft_revision
    if audit is not None:  # only when the commit changed: awaited in the transaction of the change
        await audit(dep)
    await db.commit()
    return dep, True


async def delete(db: AsyncSession, dep_id: int, audit: Hook | None = None) -> None:
    dep = await get(db, dep_id)
    if dep is None:
        raise DeploymentNotFound(dep_id)
    # freed nodes go back to auto-assignment: a manual pin to a deployment that no longer exists means nothing
    await db.execute(
        sql_update(NodeAssignment).where(NodeAssignment.deployment_id == dep_id).values(deployment_id=None, manual=False)
    )
    await db.delete(dep)
    if audit is not None:
        await audit(dep)
    await db.commit()


# ---------- assignment ----------


class NodeAccels(dict):
    """Node name -> accel for the nodes known right now, and `cluster`: the names the cluster has. A cluster node wins
    over a standalone record of the same name (left behind by a PC that moved to k3s)."""

    def __init__(self, known: dict[str, str | None], cluster: set[str]):
        super().__init__(known)
        self.cluster = cluster


async def _source_of(db: AsyncSession, node: str, cluster: set[str] = frozenset()) -> str:
    if node in cluster:
        return "k8s"
    return "standalone" if await db.get(StandaloneNode, node) is not None else "k8s"


async def _node_accels(db: AsyncSession, k8s) -> NodeAccels:
    """The nodes known right now: the standalone registry, then the cluster (when reachable), which wins."""
    known: dict[str, str | None] = {n.name.lower(): n.accel for n in (await db.scalars(select(StandaloneNode))).all()}
    cluster: set[str] = set()
    if k8s is not None:
        try:
            for n in await k8s.list_nodes():
                known[n.name.lower()] = n.accel
                cluster.add(n.name.lower())
        except _K8S_ERRORS:
            log.warning("cluster unavailable while checking node accelerators")
    return NodeAccels(known, cluster)


def _accel_mismatch(dep: ModelDeployment, accels: dict[str, str | None], node: str) -> bool:
    accel = accels.get(node)
    return dep.accel != "any" and accel is not None and accel != dep.accel


async def _release_mismatched(db: AsyncSession, dep: ModelDeployment, accels: dict[str, str | None]) -> None:
    """After an accel change: auto-assigned nodes that no longer fit are released, manual ones block the change."""
    rows = (await db.scalars(select(NodeAssignment).where(NodeAssignment.deployment_id == dep.id))).all()
    pinned = []
    for row in rows:
        if not _accel_mismatch(dep, accels, row.node_name):
            continue
        if row.manual:
            pinned.append(row.node_name)
        else:
            row.deployment_id = None
    if pinned:
        raise DeploymentInvalid(
            f"Ускоритель {dep.accel} не подходит назначенным вручную нодам: {', '.join(sorted(pinned))}. "
            "Снимите назначение с этих нод или выберите другой ускоритель"
        )


async def _assign(
    db: AsyncSession, node_name: str, dep: ModelDeployment | None, accels: dict[str, str | None] | None = None
) -> NodeAssignment:
    """Manual (admin) assignment without commit. `accels` (node -> accel) enables the accelerator check; a node the
    cluster has (`NodeAccels.cluster`) is a cluster node even with a standalone record of the same name."""
    node = node_name.strip().lower()
    source = await _source_of(db, node, getattr(accels, "cluster", frozenset()))
    if dep is not None and source == "standalone" and (dep.mode != "replicas" or dep.engine != "llamacpp"):
        raise DeploymentInvalid(STANDALONE_ONLY_REPLICAS)
    if dep is not None and accels is not None and _accel_mismatch(dep, accels, node):
        raise DeploymentInvalid(f"Нода {node} имеет ускоритель {accels[node]}, а развёртывание требует {dep.accel}")
    dep_id = dep.id if dep is not None else None
    # Lock and re-read the row so a concurrent auto_assign write is seen, not overwritten from a stale copy.
    row = await db.get(NodeAssignment, node, with_for_update=True, populate_existing=True)
    if row is None:
        # Insert-or-take-over: auto_assign may insert the same node between the read and this write.
        await db.execute(
            pg_insert(NodeAssignment)
            .values(node_name=node, deployment_id=dep_id, source=source, manual=True)
            .on_conflict_do_update(
                index_elements=["node_name"], set_={"deployment_id": dep_id, "source": source, "manual": True}
            )
        )
        return await db.get(NodeAssignment, node, populate_existing=True)
    row.deployment_id, row.source, row.manual = dep_id, source, True
    return row


async def _set_nodes(
    db: AsyncSession, dep: ModelDeployment, nodes: list[str], accels: dict[str, str | None] | None = None
) -> None:
    wanted = {n.strip().lower() for n in nodes}
    current = await db.scalars(select(NodeAssignment).where(NodeAssignment.deployment_id == dep.id))
    for row in current.all():
        if row.node_name not in wanted:
            row.deployment_id, row.manual = None, True
    for node in sorted(wanted):
        await _assign(db, node, dep, accels)


async def assign(
    db: AsyncSession, node_name: str, deployment_id: int | None, k8s=None, audit: Hook | None = None
) -> NodeAssignment:
    """Admin action: pins the node to a deployment (or to none); auto-assignment leaves it alone afterwards."""
    dep = None
    if deployment_id is not None:
        dep = await get(db, deployment_id)
        if dep is None:
            raise DeploymentNotFound(deployment_id)
    try:
        accels = await _node_accels(db, k8s) if dep is not None or k8s is not None else None
        row = await _assign(db, node_name, dep, accels)
        if audit is not None:
            await audit(row)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return row


def _accepts(dep: ModelDeployment, c: NodeCandidate) -> bool:
    if dep.accel not in ("any", c.accel):
        return False
    return c.source != "standalone" or (dep.mode == "replicas" and dep.engine == "llamacpp")


async def auto_assign(db: AsyncSession, candidates: list[NodeCandidate]) -> list[tuple[str, int]]:
    """Creates missing assignment rows and places idle non-manual nodes on the first fitting auto-assign deployment.

    The rows are read first, but every write re-checks its premise in the database (insert-if-absent, update only a
    row that is still idle and non-manual), so a concurrent admin assignment always wins.
    """
    deployments = list(
        (
            await db.scalars(
                select(ModelDeployment)
                .where(ModelDeployment.enabled.is_(True), ModelDeployment.auto_assign.is_(True))
                .order_by(ModelDeployment.id)
            )
        ).all()
    )
    rows = {r.node_name: r for r in (await db.scalars(select(NodeAssignment))).all()}
    changes: list[tuple[str, int]] = []
    for c in sorted(candidates, key=lambda c: c.name):
        name = c.name.lower()
        row = rows.get(name)
        if row is not None and (row.manual or row.deployment_id is not None or row.maintenance):
            continue
        dep = next((d for d in deployments if _accepts(d, c)), None)
        if row is not None and dep is None:
            continue
        dep_id = dep.id if dep else None
        if row is None:
            stmt = (
                pg_insert(NodeAssignment)
                .values(node_name=name, deployment_id=dep_id, source=c.source, manual=False)
                .on_conflict_do_nothing(index_elements=["node_name"])
            )
        else:
            stmt = (
                sql_update(NodeAssignment)
                .where(
                    NodeAssignment.node_name == name,
                    NodeAssignment.manual.is_(False),
                    NodeAssignment.deployment_id.is_(None),
                    NodeAssignment.maintenance.is_(False),
                )
                .values(deployment_id=dep_id)
            )
        try:
            async with db.begin_nested():  # the deployment may have been deleted since it was read
                written = (await db.execute(stmt)).rowcount
        except IntegrityError:
            continue
        if written and dep_id is not None:
            changes.append((name, dep_id))
    await db.commit()
    # Core writes bypass the identity map: reload the rows this session already holds
    await db.execute(select(NodeAssignment).execution_options(populate_existing=True))
    return changes


async def _known_node(db: AsyncSession, node: str, k8s) -> bool:
    """The node has an assignment row, is a registered standalone node or is in the cluster (K8sUnavailable when
    only the cluster could tell)."""
    if await db.get(NodeAssignment, node) is not None or await db.get(StandaloneNode, node) is not None:
        return True
    if k8s is None:
        return False
    return any(n.name.lower() == node for n in await k8s.list_nodes())


async def set_maintenance(
    db: AsyncSession, node_name: str, on: bool, k8s=None, audit: Hook | None = None
) -> NodeAssignment:
    """Admin action: the node is taken off (or put back on) its deployment; its assignment itself stays.
    The main node runs no models and is refused; an unknown node is NodeNotFound."""
    node = node_name.strip().lower()
    if node == MAIN_NODE:
        raise DeploymentInvalid("Главный узел нельзя перевести на обслуживание")
    if not await _known_node(db, node, k8s):
        raise NodeNotFound(node)
    source = await _source_of(db, node)
    try:
        await db.execute(
            pg_insert(NodeAssignment)
            .values(node_name=node, deployment_id=None, source=source, manual=False, maintenance=on)
            .on_conflict_do_update(index_elements=["node_name"], set_={"maintenance": on})
        )
        if audit is not None:  # the node's name, normalized
            await audit(node)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return await db.get(NodeAssignment, node, populate_existing=True)


def effective_deployment(
    row: NodeAssignment | None,
    deployments: list[ModelDeployment],
    accel: str | None,
    source: str,
    now: datetime,
    drain_s: float = 0.0,
) -> ModelDeployment | None:
    """The deployment a node runs right now (`deployments` ordered by id, as `list_all` returns them).

    Nothing on maintenance or for a disabled deployment. Outside its deployment's working window the node is lent to
    the first enabled auto-assign deployment inside its own window that accepts it, as an idle node would be; the
    assignment in the database does not change, so the node goes back when the window opens. For `drain_s` after
    the end of the window the node keeps its deployment, so the streams in progress finish (`schedule.running`).
    """
    if row is None or row.maintenance or row.deployment_id is None:
        return None
    dep = next((d for d in deployments if d.id == row.deployment_id), None)
    if dep is None or not dep.enabled:
        return None
    if sched.running(dep.schedule, now, drain_s):
        return dep
    if accel is None:
        return None
    candidate = NodeCandidate(row.node_name, accel, source, None, None)
    return next(
        (
            d
            for d in deployments
            if d is not dep and d.enabled and d.auto_assign and sched.active(d.schedule, now) and _accepts(d, candidate)
        ),
        None,
    )


async def purge_standalone(db: AsyncSession, cluster: list, purge_after_s: float) -> list[str]:
    """Deletes the standalone records that shadow a Ready cluster node of the same name (a PC moved from standalone to
    k3s keeps its name) and those silent for longer than `purge_after_s`; returns the names, each logged with why.

    A shadowed node's assignment becomes a cluster one (its deployment and pin stay). A silent node keeps its
    assignment row too: when it comes back the portal answers its heartbeat with 404 and it registers again.
    `cluster` must come from a successful node listing."""
    ready = {n.name.lower() for n in cluster if n.ready}
    cutoff = datetime.now(UTC) - timedelta(seconds=purge_after_s)
    purged: list[str] = []
    for node in (await db.scalars(select(StandaloneNode).order_by(StandaloneNode.name))).all():
        if node.name in ready:
            log.info("standalone record of %s removed: a Ready cluster node has the same name", node.name)
            await db.execute(
                sql_update(NodeAssignment).where(NodeAssignment.node_name == node.name).values(source="k8s")
            )
        elif node.last_seen_at < cutoff:
            log.info("standalone record of %s removed: no heartbeat since %s", node.name, node.last_seen_at.isoformat())
        else:
            continue
        await db.delete(node)
        purged.append(node.name)
    if purged:
        await db.commit()
    return purged


async def live_nodes(db: AsyncSession, ttl_s: float) -> list[StandaloneNode]:
    cutoff = datetime.now(UTC) - timedelta(seconds=ttl_s)
    result = await db.scalars(
        select(StandaloneNode)
        .where(StandaloneNode.last_seen_at >= cutoff, StandaloneNode.stopped_at.is_(None))
        .order_by(StandaloneNode.name)
    )
    return list(result.all())


async def desired_for_node(db: AsyncSession, node_name: str, drain_s: float = 0.0) -> DesiredConfig | None:
    """What a standalone node should serve. The heartbeat passes SCHEDULE_DRAIN_S so the node keeps its model a
    little past the end of its window; the backend controller passes nothing, so the node leaves LiteLLM at once."""
    row = await db.get(NodeAssignment, node_name.lower())
    if row is None or row.deployment_id is None:
        return None
    node = await db.get(StandaloneNode, node_name.lower())
    dep = effective_deployment(
        row, await list_all(db), node.accel if node else None, "standalone", sched.now_utc(), drain_s
    )
    if dep is None or dep.mode != "replicas" or dep.engine != "llamacpp" or not dep.file:
        return None
    return DesiredConfig(
        deployment=dep.name,
        model_repo=dep.repo,
        model_file=dep.file,
        public_model_name=dep.name,
        ctx_size=dep.ctx_per_request * dep.parallel,
        parallel=dep.parallel,
        spec_hash=spec_hash(dep),
        model_revision=dep.revision or "",
        llm_env=dict(llama_settings_env(dep)),
    )


# ---------- seed and meta refresh ----------


def _now() -> float:
    return time.monotonic()


async def _meta_or_none(
    hf, engine: str, repo: str, file: str | None, timeout_s: float = SEED_META_TIMEOUT_S, layout: bool = False
) -> dict[str, Any] | None:
    try:
        return await asyncio.wait_for(fetch_meta(hf, engine, repo, file, layout), timeout_s)
    except (HfNotFound, HfUnavailable, DeploymentInvalid, TimeoutError) as e:
        log.warning("model metadata for %s unavailable: %s", repo, e)
        return None


async def _commit_or_none(hf, repo: str, timeout_s: float = SEED_META_TIMEOUT_S) -> str | None:
    """The commit of main for the seed; None (main, «Обновить до последней версии» pins it later) when offline."""
    try:
        return await asyncio.wait_for(hf.commit(repo), timeout_s)
    except (HfNotFound, HfUnavailable, TimeoutError) as e:
        log.warning("commit of %s unavailable, the seed follows main: %s", repo, e)
        return None


async def seed_default(db: AsyncSession, settings, hf) -> None:
    """Creates the default deployment once per installation: a deployment the admin deleted does not come back."""
    if await db.get(PortalFlag, SEED_FLAG) is not None:
        return
    if await db.scalar(select(func.count()).select_from(ModelDeployment)):
        db.add(PortalFlag(key=SEED_FLAG, value="existing"))  # an installation that predates the flag
        await db.commit()
        return
    meta = await _meta_or_none(hf, "llamacpp", settings.default_model_repo, settings.default_model_file)
    revision = await _commit_or_none(hf, settings.default_model_repo) if meta is not None else None
    db.add(
        ModelDeployment(
            name=settings.default_model_name,
            engine="llamacpp",
            repo=settings.default_model_repo,
            file=settings.default_model_file,
            mode="replicas",
            accel="any",
            ctx_per_request=settings.default_ctx_per_request,
            parallel=settings.default_parallel,
            auto_assign=True,
            enabled=True,
            meta=meta,
            revision=revision,
        )
    )
    db.add(PortalFlag(key=SEED_FLAG, value="seeded"))
    await db.commit()


def ctx_exceeds_model(dep: ModelDeployment) -> bool:
    """The context per request is above the model's own limit (known only once `meta` is there)."""
    return dep.meta is not None and dep.ctx_per_request > dep.meta["max_ctx"]


async def refresh_missing_meta(db: AsyncSession, hf, backoff: dict[int, tuple[float, float]] | None = None) -> None:
    """Fills `meta` of deployments created while Hugging Face was unreachable.

    One call never spends more than META_REFRESH_BUDGET_S in total. `backoff` (deployment id -> (retry not before,
    last delay), monotonic seconds) is kept by the caller: a failed lookup is retried after 30 s, 60 s, ... up to 1 h.
    """
    backoff = {} if backoff is None else backoff
    deployments = await list_all(db)
    for dep_id in set(backoff) - {d.id for d in deployments if d.meta is None}:
        del backoff[dep_id]
    deadline = _now() + META_REFRESH_BUDGET_S
    for dep in deployments:
        if dep.meta is not None:
            continue
        now = _now()
        retry_at, delay = backoff.get(dep.id, (0.0, 0.0))
        if now < retry_at:
            continue
        if deadline - now < META_MIN_ATTEMPT_S:
            break  # not recorded as a failure: the next pass tries this deployment first
        engine, repo, file = dep.engine, dep.repo, dep.file
        meta = await _meta_or_none(
            hf, engine, repo, file, min(SEED_META_TIMEOUT_S, deadline - now), layout=dep.mode == "split"
        )
        if meta is None:
            delay = min(delay * 2 if delay else META_BACKOFF_START_S, META_BACKOFF_MAX_S)
            backoff[dep.id] = (_now() + delay, delay)
            continue
        backoff.pop(dep.id, None)
        # only if the row still describes the model that was looked up: an admin PATCH meanwhile wins
        written = (
            await db.execute(
                sql_update(ModelDeployment)
                .where(
                    ModelDeployment.id == dep.id,
                    # a None written through the ORM is stored as JSON null, not SQL NULL
                    or_(ModelDeployment.meta.is_(None), cast(ModelDeployment.meta, Text) == "null"),
                    ModelDeployment.engine == engine,
                    ModelDeployment.repo == repo,
                    ModelDeployment.file.is_not_distinct_from(file),
                )
                .values(meta=meta)
                .execution_options(synchronize_session=False)
            )
        ).rowcount
        await db.commit()
        await db.refresh(dep)  # the Core write bypassed the identity map; a lost race shows the admin's state
        if not written:
            continue
        if ctx_exceeds_model(dep):
            log.warning(
                "deployment %s: context per request %d is above the model limit %d", dep.name, dep.ctx_per_request, meta["max_ctx"]
            )


# ---------- catalog and status ----------


async def ready_backends(litellm) -> dict[str, int] | None:
    """Registered LiteLLM backends per model name; None when the gateway is unreachable."""
    try:
        return await litellm.model_group_status()
    except (LiteLLMUnavailable, LiteLLMError):
        return None


def catalog_status(ready: int | None, assigned: int) -> str:
    if ready is None:
        return STATUS_UNKNOWN
    if ready >= 1:
        return STATUS_READY
    return STATUS_STARTING if assigned else STATUS_DOWN


async def catalog(db: AsyncSession, counts_source) -> list[tuple[ModelDeployment, int | None, str]]:
    """Enabled deployments with their ready backend count and catalog status.

    `counts_source` is an async callable (`BackendCounts.get`). The database is read first and its transaction
    ended before the gateway is asked, so no pooled connection waits on the network.
    """
    deployments = [d for d in await list_all(db) if d.enabled]
    nodes = await assigned_nodes(db)
    await db.commit()
    counts = await counts_source()
    now = sched.now_utc()
    result = []
    for dep in deployments:
        ready = None if counts is None else counts.get(dep.name, 0)
        assigned = len(nodes.get(dep.id, [])) if sched.active(dep.schedule, now) else 0  # off: nothing is starting
        result.append((dep, ready, catalog_status(ready, assigned)))
    return result


def _split_groups(
    dep: ModelDeployment,
    names: list[str],
    accels: dict[str, str | None],
    existing: dict[int, list[str]],
    away: set[str] = frozenset(),
    memory: dict[str, placement.NodeMemory] | None = None,
) -> list[list[str]]:
    """Node names of the split groups the workload controller forms, by the same rules (`plan_groups`): the groups of
    the running leaders (`existing`) stay, the other nodes pair up. Nodes `away` (maintenance) count for the number of
    groups the spare nodes leave room for, but are never placed. `memory` (by lower-case node name) gives the RAM the
    controller orders a new group by (the node with the most RAM leads), so the page shows the group it really forms."""
    if dep.mode != "split":
        return []
    workers = [
        RpcWorker(n, "", 0, accels[n], getattr(memory.get(n.lower()), "ram_mib", None) if memory else None)
        for n in names
        if accels.get(n) is not None and n not in away
    ]
    planned = plan_groups(workers, dep.group_size, existing, limit=split_group_limit(dep, len(names)))
    return [[w.node_name for w in group] for group in planned.values()]


async def _load_progress(dep: ModelDeployment, pods: list, node_ips: dict[str, str], progress, port: int) -> dict[str, Any]:
    """pod name -> load progress of the inference pods (progress.pod_progress); /health is probed concurrently."""
    inference = [p for p in pods if p.role in (ROLE_INFERENCE, ROLE_LEADER)]
    probed = [p for p in inference if not p.ready and p.phase == "Running" and node_ips.get(p.node_name or "")]
    if not any(not p.ready for p in inference):
        return {p.name: pod_progress(dep, ready=True, phase=p.phase, health=None, transfers=None) for p in inference}
    transfers, *codes = await asyncio.gather(
        progress.transfers(), *(progress.health(f"http://{node_ips[p.node_name]}:{p.port or port}/health") for p in probed)
    )
    health = {p.name: code for p, code in zip(probed, codes, strict=True)}
    return {
        p.name: pod_progress(
            dep, ready=p.ready, phase=p.phase, health=health.get(p.name),
            transfers=None if transfers is None else transfers.get(p.node_name or "", []),
        )
        for p in inference
    }


def node_memory(n) -> placement.NodeMemory:
    return placement.NodeMemory(n.name.lower(), n.accel or "", n.vram_mib, placement.effective_ram_mib(n.ram_mib, n.mem_mib))


def placement_problems(
    dep: ModelDeployment,
    groups: list[list[str]],
    memory: dict[str, placement.NodeMemory],
    reserve_mib: int,
    leader_on_main: bool = False,
) -> list[str]:
    """Why split groups have no leader: those the controller holds because the model does not fit (same decision)."""
    problems = []
    for names in groups:
        if any(n not in memory for n in names):
            continue
        plan = placement.plan_for(dep, [memory[n] for n in names], reserve_mib, leader_on_main)
        if placement.decide(plan) == placement.HOLD:
            problems.append(placement.misfit_message(plan))
    if leader_on_main and groups and (problem := main_disk_problem(dep)):
        problems.append(problem)
    return problems


async def deployment_status(
    db: AsyncSession,
    k8s,
    litellm,
    dep: ModelDeployment,
    reserve_mib: int = placement.DEFAULT_GPU_COMPUTE_RESERVE_MIB,
    progress=None,
    infer_port: int = 18080,
    leader_on_main: bool = False,
) -> dict[str, Any]:
    """Pods per assigned node; with a `progress` source (ProgressSource) each inference pod also carries its load
    stage: downloading with a percentage, loading the weights, ready. A split leader on main is no node's pod: it is
    listed with the first node of its group, where it would run otherwise, and says `on_main`."""
    h = spec_hash(dep)
    rows = (
        await db.scalars(
            select(NodeAssignment).where(NodeAssignment.deployment_id == dep.id).order_by(NodeAssignment.node_name)
        )
    ).all()
    standalone = {n.name: n for n in (await db.scalars(select(StandaloneNode))).all()}
    pods_by_node: dict[str, list] = {}
    accels: dict[str, str | None] = {}
    memory: dict[str, placement.NodeMemory] = {}
    node_ips: dict[str, str] = {}
    existing: dict[int, list[str]] = {}
    try:
        for pod in await k8s.list_pods(dep.name):
            pods_by_node.setdefault(pod.node_name or "", []).append(pod)
        cluster = await k8s.list_nodes()
        accels = {n.name: n.accel for n in cluster}
        memory = {n.name.lower(): node_memory(n) for n in cluster if not n.is_main}
        node_ips = {
            n.name: n.internal_ip if n.is_main else n.external_ip or n.internal_ip
            for n in cluster
            if n.external_ip or n.internal_ip
        }
        if dep.mode == "split":
            existing = leader_groups(await k8s.list_owned(), dep.name)
    except _K8S_ERRORS:
        log.warning("cluster unavailable while reading status of %s", dep.name)
    loads: dict[str, Any] = {}
    if progress is not None:
        loads = await _load_progress(dep, [p for ps in pods_by_node.values() for p in ps], node_ips, progress, infer_port)
    for pod in pods_by_node.pop(MAIN_NODE, []):
        members = existing.get(leader_index(dep.name, pod.workload))
        if members:
            pods_by_node.setdefault(members[0], []).append(pod)
    nodes = []
    for row in rows:
        pods = []
        for pod in pods_by_node.get(row.node_name, []):
            last_log = None
            if not pod.ready:
                try:
                    tail = await k8s.pod_log_tail(pod.name, 1)
                    last_log = tail.strip() or None
                except _K8S_ERRORS:
                    pass
            pods.append(
                {
                    "name": pod.name,
                    "role": pod.role or "",
                    "phase": pod.phase or "",
                    "ready": pod.ready,
                    "restarts": pod.restarts,
                    "message": pod.message or "",
                    "last_log": last_log,
                    "progress": loads.get(pod.name),
                    **({"on_main": True} if pod.node_name == MAIN_NODE else {}),
                }
            )
        sa = None if row.node_name in accels else standalone.get(row.node_name)  # a cluster node wins over the record
        if sa is not None:
            applied = sa.applied_spec_hash == h
        else:
            applied = bool(pods) and all(p["ready"] for p in pods)
        nodes.append(
            {"name": row.node_name, "source": "standalone" if sa is not None else "k8s", "pods": pods, "applied": applied}
        )
    counts = await ready_backends(litellm)
    groups = _split_groups(
        dep,
        [n["name"] for n in nodes if n["source"] == "k8s"],
        accels,
        existing,
        {r.node_name for r in rows if r.maintenance},
        memory,
    )
    return {
        "spec_hash": h,
        "nodes": nodes,
        "groups": groups,
        "placement_problems": placement_problems(dep, groups, memory, reserve_mib, leader_on_main),
        "ready_backends": None if counts is None else counts.get(dep.name, 0),
    }


async def node_memories(k8s, names: list[str]) -> dict[str, placement.NodeMemory]:
    """Memory of the requested cluster nodes that are known (split runs only on cluster nodes)."""
    wanted = {n.lower() for n in names}
    try:
        return {n.name.lower(): node_memory(n) for n in await k8s.list_nodes() if n.name.lower() in wanted and not n.is_main}
    except _K8S_ERRORS:
        log.warning("cluster unavailable while reading node memory")
        return {}


async def node_capacities(db: AsyncSession, k8s, names: list[str], with_accel: bool = False) -> list[tuple]:
    """(name, usable memory in bytes or None when unknown) for each requested node, in order; `with_accel` adds the
    node's accelerator (None when unknown) as a third item."""
    known: dict[str, tuple[int | None, str | None]] = {}
    for n in (await db.scalars(select(StandaloneNode))).all():
        known[n.name] = (node_capacity_bytes(n.accel, n.vram_mib, n.ram_mib), n.accel)
    try:  # a cluster node wins over a standalone record of the same name
        for n in await k8s.list_nodes():
            known[n.name] = (node_capacity_bytes(n.accel or "", n.vram_mib, n.mem_mib), n.accel)
    except _K8S_ERRORS:
        log.warning("cluster unavailable while reading node capacities")
    rows = [(name, *known.get(name.lower(), (None, None))) for name in names]
    return rows if with_accel else [(name, cap) for name, cap, _ in rows]
