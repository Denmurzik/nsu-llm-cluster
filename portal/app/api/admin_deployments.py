"""Admin API: deployments, node assignment, Hugging Face inspector and memory estimate."""

import asyncio
import json
import logging
import posixpath
import re
import time
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from kubernetes_asyncio.client.exceptions import ApiException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, settings_store
from app.api import playground
from app.auth.sessions import require_admin, require_csrf
from app.catalog import estimate as est
from app.catalog import placement
from app.catalog.hf import HfNotFound, HfUnavailable, parse_quant
from app.config import Settings
from app.db import get_session
from app.deployments import schedule, service
from app.deployments.service import DeploymentIn, DeploymentPatch
from app.deps import REDIS_ERRORS, get_hf, get_k8s, get_litellm, get_progress, get_settings_dep
from app.k8s import K8sUnavailable
from app.litellm_client import LiteLLMBusy, LiteLLMClient, LiteLLMError, LiteLLMUnavailable
from app.localmodels.store import LocalNotFound, is_local
from app.models import ModelDeployment, NodeAssignment, User
from app.workloads import builders
from app.workloads.builders import RpcWorker, plan_groups

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
csrf = [Depends(require_csrf)]


@contextmanager
def _errors():
    try:
        yield
    except service.DeploymentConflict as e:
        raise HTTPException(409, e.detail) from e
    except service.DeploymentInvalid as e:
        raise HTTPException(422, e.detail) from e
    except service.DeploymentNotFound as e:
        raise HTTPException(404, "Развёртывание не найдено") from e
    except LocalNotFound as e:
        raise HTTPException(404, "Своя модель не найдена") from e
    except HfNotFound as e:
        raise HTTPException(404, "Репозиторий или файл не найден") from e
    except HfUnavailable as e:
        raise HTTPException(502, "Hugging Face недоступен") from e


async def _read_extras(
    hf, repo: str, draft_repo: str | None, draft_file: str | None, mmproj_file: str | None
) -> tuple[dict | None, int]:
    """(meta of the draft model, bytes of the image projector) for the memory estimate. The projector is a file of the
    model repository, or `local/<model>/<file>` of an own model."""
    draft = await service.fetch_meta(hf, "llamacpp", draft_repo, draft_file) if draft_repo and draft_file else None
    size = 0
    if mmproj_file:
        mm_repo, inner = repo, mmproj_file
        if is_local(mmproj_file):
            model, _, inner = mmproj_file[len("local/"):].partition("/")
            mm_repo = f"local/{model}"
        size = next((f.size for f in await hf.list_files(mm_repo) if f.path == inner), 0)
    return draft, size


async def _store_extras(db: AsyncSession, hf, dep: ModelDeployment) -> None:
    """Keeps the sizes of the draft model and the projector in `meta["extras"]`: the placement of the workload
    controller (`placement.plan_for`) needs them and only knows the deployment row. Read when the choice changes;
    Hugging Face being unreachable leaves them out (the estimate then lacks them, nothing else breaks)."""
    if dep.meta is None:
        return
    key = est.extras_key(dep.draft_repo, dep.draft_file, dep.mmproj_file)
    stored = dep.meta.get("extras")
    wanted = bool(dep.draft_repo or dep.mmproj_file)
    if (stored or {}).get("key") == key or (not wanted and stored is None):
        return
    meta = {k: v for k, v in dep.meta.items() if k != "extras"}
    if wanted:
        try:
            draft, size = await _read_extras(hf, dep.repo, dep.draft_repo, dep.draft_file, dep.mmproj_file)
            meta["extras"] = est.stored_extras(draft, size, key)
        except (HfNotFound, HfUnavailable, LocalNotFound, service.DeploymentInvalid) as exc:
            log.warning("deployment %s: sizes of the draft model and the projector not read: %s", dep.name, exc)
            return
    dep.meta = meta
    await db.commit()


def _view(dep: ModelDeployment, nodes: list[str], settings: Settings) -> dict[str, Any]:
    return {
        "id": dep.id,
        "name": dep.name,
        "engine": dep.engine,
        "repo": dep.repo,
        "file": dep.file,
        "revision": dep.revision,
        "mode": dep.mode,
        "group_size": dep.group_size,
        "accel": dep.accel,
        "ctx_per_request": dep.ctx_per_request,
        "parallel": dep.parallel,
        "auto_assign": dep.auto_assign,
        "enabled": dep.enabled,
        "spare_nodes": dep.spare_nodes,
        "regroup_grace_s": dep.regroup_grace_s,
        "regroup_grace_effective_s": service.regroup_grace(dep, settings),
        "schedule": dep.schedule,
        "schedule_active": schedule.active(dep.schedule, schedule.now_utc()),
        "use_node_ram": dep.use_node_ram,
        "ram_budget_pct": dep.ram_budget_pct,
        "reasoning": dep.reasoning,
        "kv_cache_type": dep.kv_cache_type,
        "sampling": dep.sampling,
        "draft_repo": dep.draft_repo,
        "draft_file": dep.draft_file,
        "draft_revision": dep.draft_revision,
        "mmproj_file": dep.mmproj_file,
        "extra_args": dep.extra_args,
        "system_prompt": dep.system_prompt,
        "experimental": dep.experimental,
        "schedule_drain_s": settings.schedule_drain_s,
        "meta_missing": dep.meta is None,
        "ctx_exceeds_model": service.ctx_exceeds_model(dep),
        "meta": dep.meta,
        "assigned_nodes": nodes,
        "spec_hash": service.spec_hash(dep),
    }


async def _one(db: AsyncSession, dep_id: int) -> ModelDeployment:
    dep = await service.get(db, dep_id)
    if dep is None:
        raise HTTPException(404, "Развёртывание не найдено")
    return dep


async def _view_of(db: AsyncSession, dep: ModelDeployment, settings: Settings) -> dict[str, Any]:
    return _view(dep, (await service.assigned_nodes(db)).get(dep.id, []), settings)


@router.get("/deployments")
async def list_deployments(
    db: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> list[dict]:
    nodes = await service.assigned_nodes(db)
    return [_view(d, nodes.get(d.id, []), settings) for d in await service.list_all(db)]


@router.post("/deployments", status_code=201, dependencies=csrf)
async def create_deployment(
    data: DeploymentIn,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    hf=Depends(get_hf),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict:

    async def log(dep: ModelDeployment) -> None:
        details = {"engine": dep.engine, "repo": dep.repo, "file": dep.file, "mode": dep.mode}
        await audit.record(db, user, "deployment.create", "deployment", dep.name, details, audit.client_ip(request))

    with _errors():
        dep = await service.create(db, hf, data, k8s, audit=log)
    await _store_extras(db, hf, dep)
    return await _view_of(db, dep, settings)


@router.get("/deployments/{dep_id}")
async def get_deployment(
    dep_id: int, db: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> dict:
    return await _view_of(db, await _one(db, dep_id), settings)


@router.patch("/deployments/{dep_id}", dependencies=csrf)
async def patch_deployment(
    dep_id: int,
    patch: DeploymentPatch,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    hf=Depends(get_hf),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    changes = patch.model_dump(mode="json", exclude_unset=True)

    async def log(dep: ModelDeployment) -> None:
        await audit.record(
            db, user, "deployment.update", "deployment", dep.name, {"changes": changes}, audit.client_ip(request)
        )

    with _errors():
        dep = await service.update(db, hf, dep_id, patch, k8s, audit=log)
    await _store_extras(db, hf, dep)
    return await _view_of(db, dep, settings)


@router.post("/deployments/{dep_id}/refresh-revision", dependencies=csrf)
async def refresh_revision(
    dep_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    hf=Depends(get_hf),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    """«Обновить до последней версии»: pins the current commit of main; a new one reloads the pods as planned."""

    async def log(dep: ModelDeployment) -> None:  # called only when the revision changed: a press that found nothing new logs nothing
        await audit.record(
            db, user, "deployment.refresh_revision", "deployment", dep.name,
            {"revision": dep.revision, "draft_revision": dep.draft_revision}, audit.client_ip(request),
        )

    with _errors():
        dep, changed = await service.refresh_revision(db, hf, dep_id, audit=log)
    return {"changed": changed, "deployment": await _view_of(db, dep, settings)}


@router.delete("/deployments/{dep_id}", status_code=204, dependencies=csrf)
async def delete_deployment(
    dep_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> Response:

    async def log(dep: ModelDeployment) -> None:
        await audit.record(
            db, user, "deployment.delete", "deployment", dep.name, {"repo": dep.repo, "file": dep.file},
            audit.client_ip(request),
        )

    with _errors():
        await service.delete(db, dep_id, audit=log)  # DeploymentNotFound when there is none
    return Response(status_code=204)


@router.get("/deployments/{dep_id}/status")
async def deployment_status(
    dep_id: int,
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
    litellm=Depends(get_litellm),
    progress=Depends(get_progress),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    dep = await _one(db, dep_id)
    return await service.deployment_status(
        db, k8s, litellm, dep, settings.gpu_compute_reserve_mib, progress=progress, infer_port=settings.infer_port,
        leader_on_main=await _leader_on_main(db, settings),
    )


# ---------- the command of llama-server (task 2-22b) ----------


@router.post("/deployments/command-preview", dependencies=csrf)
async def command_preview(
    data: DeploymentIn, db: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> dict:
    """The command a deployment with these settings would start, for the form that is not saved yet. Validated like a
    create (the 422 text is for the admin to read); the pinned commits do not exist yet, a placeholder stands in."""
    fields = data.model_dump(exclude={"nodes"})
    with _errors():
        service._normalize_llm(fields)
        service._check_fields(fields)
    fields["name"] = fields["name"] or "model"
    dep = SimpleNamespace(
        **fields,
        revision=None if is_local(data.repo) else "<commit>",
        draft_revision=None if not fields["draft_repo"] or is_local(fields["draft_repo"]) else "<commit>",
        meta=None,
    )
    return builders.command_preview(dep, settings, await _leader_on_main(db, settings))


@router.get("/deployments/{dep_id}/command")
async def deployment_command(
    dep_id: int, db: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings_dep)
) -> dict:
    """The command of a saved deployment, built by the same code that builds its pods."""
    return builders.command_preview(await _one(db, dep_id), settings, await _leader_on_main(db, settings))


class AssignmentIn(BaseModel):
    deployment_id: int | None = None


@router.put("/nodes/{name}/assignment", dependencies=csrf)
async def put_assignment(
    name: str,
    body: AssignmentIn,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
) -> dict:
    target = await service.get(db, body.deployment_id) if body.deployment_id is not None else None

    async def log(row: NodeAssignment) -> None:
        details = {"deployment_id": row.deployment_id, "deployment": target.name if target is not None else None}
        await audit.record(db, user, "node.assign", "node", row.node_name, details, audit.client_ip(request))

    with _errors():
        row = await service.assign(db, name, body.deployment_id, k8s, audit=log)
    return {"node": row.node_name, "deployment_id": row.deployment_id, "source": row.source, "manual": row.manual}


class MaintenanceIn(BaseModel):
    maintenance: bool


@router.put("/nodes/{name}/maintenance", dependencies=csrf)
async def put_maintenance(
    name: str,
    body: MaintenanceIn,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    k8s=Depends(get_k8s),
) -> dict:
    """On maintenance the node runs nothing and is not auto-assigned; its assignment is kept for its return."""

    async def log(node: str) -> None:
        await audit.record(
            db, user, "node.maintenance", "node", node, {"maintenance": body.maintenance}, audit.client_ip(request)
        )

    with _errors():
        try:
            row = await service.set_maintenance(db, name, body.maintenance, k8s, audit=log)
        except service.NodeNotFound as e:
            raise HTTPException(404, "Нода не найдена") from e
        except (K8sUnavailable, ApiException) as e:
            raise HTTPException(503, "Кластер недоступен") from e
    return {"node": row.node_name, "maintenance": row.maintenance, "deployment_id": row.deployment_id}


def _check_repo_engine(repo: str, engine: str) -> None:
    if not re.fullmatch(service.REPO_RE, repo):
        raise HTTPException(422, "Репозиторий указывается как владелец/название")
    if engine not in service.ENGINES:
        raise HTTPException(422, "Движок: llamacpp или vllm")


@router.get("/hf/files")
async def hf_files(repo: str, engine: str = "llamacpp", hf=Depends(get_hf)) -> Any:
    _check_repo_engine(repo, engine)
    with _errors():
        if engine == "vllm":
            meta = await service.fetch_meta(hf, "vllm", repo, None)
            return {"size_bytes": meta["weights_bytes"], "meta": meta}
        return [
            {"file": c.file, "size_bytes": c.size_bytes, "quant": c.quant, "shards": c.shards}
            for c in await hf.gguf_files(repo)
        ]


@router.get("/hf/mmproj")
async def hf_mmproj(repo: str, hf=Depends(get_hf)) -> list[dict]:
    """The image projector files (`mmproj*.gguf`) of a model repository, for the «Изображения» choice. The model
    listing (`/hf/files`) leaves them out."""
    _check_repo_engine(repo, "llamacpp")
    with _errors():
        files = await hf.list_files(repo)
    found = [f for f in files if posixpath.basename(f.path).lower().startswith("mmproj") and f.path.lower().endswith(".gguf")]
    return [{"file": f.path, "size_bytes": f.size, "quant": parse_quant(f.path)} for f in sorted(found, key=lambda f: f.path)]


def _placement_view(plan: placement.SplitPlan, leader_on_main: bool = False) -> dict:
    """Per node of the group: bytes and layers on its GPU and in its RAM, with the budgets (null: no such device)."""
    nodes: dict[str, dict] = {}
    for d in plan.devices:
        node = nodes.setdefault(
            d.node,
            {
                "name": d.node, "leader": not nodes and not leader_on_main, "vram_bytes": 0, "vram_budget_bytes": None, "gpu_layers": 0,
                "ram_bytes": 0, "ram_budget_bytes": None, "cpu_layers": 0,
            },
        )
        if d.device == placement.GPU_DEVICE:
            node.update(vram_bytes=d.bytes, vram_budget_bytes=d.budget_bytes, gpu_layers=d.layers)
        else:
            node.update(ram_bytes=d.bytes, ram_budget_bytes=d.budget_bytes, cpu_layers=d.layers)
    has_gpu = any(d.device == placement.GPU_DEVICE for d in plan.devices)
    return {
        "fits": plan.fits,
        "tensor_split": plan.tensor_split,
        "ram_bytes": plan.ram_bytes,
        # layers in RAM of GPU nodes are computed by their CPU: the whole group generates much slower
        "speed_warning": plan.fits and has_gpu and plan.ram_bytes > 0,
        "nodes": list(nodes.values()),
    }


def _first_group_plan(
    meta: dict,
    memories: dict[str, placement.NodeMemory],
    group_size: int,
    ctx_total: int,
    use_node_ram: bool,
    pct: int,
    reserve_mib: int,
    parallel: int = 1,
    kv_cache_type: str = "f16",
    leader_extra_bytes: int = 0,
    leader_on_main: bool = False,
) -> placement.SplitPlan | None:
    """Placement of the first group the rules of `plan_groups` form from these nodes. Only an estimate: the
    controller also keeps running groups, spare and held nodes, so it may group them otherwise."""
    workers = [RpcWorker(name, "", 0, m.accel, m.ram_mib) for name, m in memories.items() if m.accel in ("cpu", "nvidia")]
    groups = plan_groups(workers, group_size, mixed=True)  # the nodes given are those of the deployment's accel
    if not groups:
        return None
    first = groups[min(groups)]
    return placement.plan_split(
        meta, [memories[w.node_name] for w in first], ctx_total, use_node_ram, pct, reserve_mib, parallel,
        kv_cache_type, leader_extra_bytes, leader_on_main,
    )


async def _leader_on_main(db: AsyncSession, settings: Settings) -> bool:
    return await settings_store.get(db, "split_leader_on_main", settings)


def _need_bytes(estimate: est.Estimate, accel: str | None) -> int:
    """What one replica needs on a node: on a GPU its VRAM share (the input embeddings stay in RAM), else all of it."""
    if accel == "nvidia" and estimate.device_bytes is not None:
        return estimate.device_bytes
    return estimate.per_node_bytes


def _recommend_split(meta: dict, memories: dict[str, placement.NodeMemory], *args) -> int | None:
    """Smallest group size (from 2) whose first group the placement fits: the table below shows the same plan, so it
    never says «не помещается» for the recommended size (zeroed last CPU and leader overhead included)."""
    for g in range(2, len(memories) + 1):
        plan = _first_group_plan(meta, memories, g, *args)
        if plan is not None and plan.fits:
            return g
    return None


@router.get("/hf/estimate")
async def hf_estimate(
    repo: str,
    file: str | None = None,
    engine: str = "llamacpp",
    ctx_per_request: int = Query(8192, ge=1),
    parallel: int = Query(2, ge=1),
    group_size: int = Query(1, ge=1),
    nodes: str = "",
    mode: str = "replicas",
    use_node_ram: bool = True,
    ram_budget_pct: int = Query(
        placement.DEFAULT_RAM_BUDGET_PCT, ge=placement.MIN_RAM_BUDGET_PCT, le=placement.MAX_RAM_BUDGET_PCT
    ),
    kv_cache_type: str = Query("f16", pattern="^(f16|q8_0|q4_0)$"),
    draft_repo: str | None = None,
    draft_file: str | None = None,
    mmproj_file: str | None = None,
    db: AsyncSession = Depends(get_session),
    hf=Depends(get_hf),
    k8s=Depends(get_k8s),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    """Memory estimate; in split mode node capacity is VRAM plus the RAM budget, and the first group's placement.
    The KV cache follows `kv_cache_type`; a draft model (its weights and its own KV cache) and the image projector
    are parts of it too (on the leader's node in split mode)."""
    _check_repo_engine(repo, engine)
    if bool(draft_repo) != bool(draft_file):
        raise HTTPException(422, "Черновая модель: нужны и репозиторий, и файл")
    if draft_repo and not re.fullmatch(service.REPO_RE, draft_repo):
        raise HTTPException(422, "Репозиторий черновой модели указывается как владелец/название")
    with _errors():
        # the layer layout also tells what a GPU holds: llama.cpp keeps the input embeddings in host RAM
        meta = await service.fetch_meta(hf, engine, repo, file, layout=mode == "split" or engine == "llamacpp")
        draft, mmproj_bytes = (
            await _read_extras(hf, repo, draft_repo, draft_file, mmproj_file) if engine == "llamacpp" else (None, 0)
        )
    estimate = est.estimate(
        est.weights_bytes(meta), service.gguf_meta_from_dict(meta), ctx_per_request, parallel, group_size,
        est.device_weights_bytes(meta), kv_cache_type,
        est.weights_bytes(draft) if draft else 0, service.gguf_meta_from_dict(draft) if draft else None, mmproj_bytes,
    )
    names = [n.strip() for n in nodes.split(",") if n.strip()]
    plan_view = None
    if mode == "split":
        memories = await service.node_memories(k8s, names)
        reserve = placement.gpu_compute_reserve_bytes(meta, settings.gpu_compute_reserve_mib)
        capacities = [
            (name, placement.split_capacity_bytes(m, use_node_ram, ram_budget_pct, reserve) if (m := memories.get(name.lower())) else None)
            for name in names
        ]
        on_main = await _leader_on_main(db, settings)
        args = (
            ctx_per_request * parallel, use_node_ram, ram_budget_pct, settings.gpu_compute_reserve_mib, parallel,
            kv_cache_type, estimate.extras_bytes, on_main,
        )
        plan = _first_group_plan(meta, memories, group_size, *args)
        plan_view = _placement_view(plan, on_main) if plan is not None else None
        recommended = _recommend_split(meta, memories, *args)
    else:
        accels = await service.node_capacities(db, k8s, names, with_accel=True)
        capacities = [(name, cap) for name, cap, _ in accels]
        recommended = est.recommend_group_size(estimate.total_bytes, [cap for _, cap in capacities if cap is not None])
    need = {name: _need_bytes(estimate, accel) for name, _, accel in accels} if mode != "split" else {}
    return {
        "meta": meta,
        "estimate": asdict(estimate),
        "nodes": [
            {
                "name": name,
                "capacity_bytes": cap,
                "need_bytes": need.get(name, estimate.per_node_bytes),
                "fits": cap is not None and need.get(name, estimate.per_node_bytes) <= cap,
            }
            for name, cap in capacities
        ],
        "recommended_group_size": recommended,
        "ctx_ok": service.MIN_CTX <= ctx_per_request <= meta["max_ctx"],
        "placement": plan_view,
    }


# -- probe ("Проверить") ----------------------------------------------------------------------------

PROBE_TIMEOUT_S = 60.0
PROBE_PROMPT = "Ответь одним словом: столица России?"
PROBE_ANSWER_CHARS = 200


def _sse_events(buffer: bytearray) -> list[dict[str, Any] | None]:
    """Complete `data:` lines of the buffer (consumed), parsed; None stands for [DONE] or a line that is not JSON."""
    events: list[dict[str, Any] | None] = []
    while (nl := buffer.find(b"\n")) >= 0:
        line = bytes(buffer[:nl]).decode("utf-8", errors="replace").strip()
        del buffer[: nl + 1]
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        try:
            parsed = json.loads(data)
        except ValueError:
            events.append(None)
            continue
        events.append(parsed if isinstance(parsed, dict) else None)
    return events


async def _probe_stream(stream: Any) -> dict[str, Any]:
    """Reads the answer stream: the text, the time of the first content chunk and the completion token count."""
    started = time.monotonic()
    buffer = bytearray()
    answer: list[str] = []
    first: float | None = None
    last = started
    chunks = 0
    thinking = False
    usage_tokens: int | None = None
    try:
        async for raw in stream:
            buffer += raw
            for event in _sse_events(buffer):
                if event is None:
                    continue
                if isinstance(event.get("error"), dict):
                    message = event["error"].get("message") or "Ошибка модели"
                    raise _ProbeFailed(str(message))
                usage = event.get("usage")
                if isinstance(usage, dict) and isinstance(usage.get("completion_tokens"), int):
                    usage_tokens = usage["completion_tokens"]
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    # a model that thinks first sends its reasoning in its own field: also a token, also a sign of life
                    thought = delta.get("reasoning_content") or delta.get("reasoning")
                    if (isinstance(content, str) and content) or (isinstance(thought, str) and thought):
                        now = time.monotonic()
                        if first is None:
                            first = now
                        last = now
                        chunks += 1
                        if isinstance(content, str) and content:
                            answer.append(content)
                        else:
                            thinking = True
    finally:
        await stream.aclose()  # frees the pooled connection also when the read was cut short
    tokens = usage_tokens if usage_tokens else chunks
    ttft_ms = round((first - started) * 1000) if first is not None else None
    generation_s = last - first if first is not None else 0.0
    tokens_per_s = round(tokens / generation_s, 1) if tokens and generation_s > 0 else None
    return {
        "answer": "".join(answer).strip()[:PROBE_ANSWER_CHARS],
        "ttft_ms": ttft_ms,
        "tokens_per_s": tokens_per_s,
        "thinking": thinking,
    }


class _ProbeFailed(Exception):
    """The stream carried an error event."""


def _probe_result(ok: bool, status: int, error: str | None, **found: Any) -> dict[str, Any]:
    return {
        "ok": ok,
        "status": status,
        "answer": found.get("answer", ""),
        "ttft_ms": found.get("ttft_ms"),
        "tokens_per_s": found.get("tokens_per_s"),
        "error": error,
    }


@router.post("/deployments/{dep_id}/probe", dependencies=csrf)
async def probe_deployment(
    dep_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    litellm: LiteLLMClient = Depends(get_litellm),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """One short question through the gateway with the admin's Playground key: does the model answer, how fast.

    The outcome is always a 200 with `ok`; only a deployment that does not exist (404) or is switched off (409) is
    refused up front."""
    dep = await _one(db, dep_id)
    if not dep.enabled:
        raise HTTPException(409, "Развёртывание выключено")
    payload = {
        "model": dep.name,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": 16,
        "stream": True,
    }
    stale: str | None = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_S):
            for attempt in (1, 2):
                key = await playground._playground_key(request, user, db, litellm, settings, stale)
                # ends the transaction of the lookups above (a commit: it does not expire `user`, a rollback would):
                # none stays open ("idle in transaction") while the model thinks
                await db.commit()
                try:
                    found = await _probe_stream(litellm.stream_chat(key, payload))
                    break
                except LiteLLMError as exc:
                    if exc.status == 401 and attempt == 1:
                        stale = key
                        continue
                    raise
            else:  # pragma: no cover - the second attempt either returns or raises
                raise LiteLLMUnavailable("no attempt left")
    except TimeoutError:
        return _probe_result(False, 0, f"Нет ответа за {int(PROBE_TIMEOUT_S)} с")
    except LiteLLMError as exc:
        return _probe_result(False, exc.status, playground._upstream_error(exc).detail)
    except LiteLLMBusy:
        return _probe_result(False, 429, playground.BUSY)
    except LiteLLMUnavailable:
        return _probe_result(False, 503, "Шлюз моделей недоступен")
    except _ProbeFailed as exc:
        return _probe_result(False, 502, str(exc))
    except REDIS_ERRORS:
        return _probe_result(False, 503, "Шлюз моделей недоступен")
    finally:
        await db.rollback()
    if not found["answer"] and not found["thinking"]:
        return _probe_result(False, 200, "Модель вернула пустой ответ", **found)
    # an empty answer with reasoning: the model works, 16 tokens were spent on thinking (the screen says so)
    return _probe_result(True, 200, None, **found)
