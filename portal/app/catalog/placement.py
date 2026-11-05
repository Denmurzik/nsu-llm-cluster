"""Placement of a split model over the devices of its group: whole layers per device, as `--tensor-split` counts.

Devices are ordered as llama.cpp registers them: the `--rpc` servers in order (the leader's own node first), inside a
server the order of its `-d` list (`CUDA0` before `CPU`). `-ngl` offloads every block and the output layer, so layer
`i` (the output layer is the last one) lands on the first device whose cumulative count exceeds `i`
(docs/research/big-models.md, sections 5 and 11.4). ggml-rpc-server has no memory limit of its own, so the counts are
the only thing that keeps a device within its budget; the memory limit of the rpc pod is the hard backstop.
"""

import math
from dataclasses import dataclass
from typing import Any

from app.catalog.estimate import NVIDIA_RESERVE, cached_layers, dep_extras, kv_elem_bytes, kv_layers_bytes, meta_from_dict

MiB = 2**20
GiB = 2**30
UBATCH = 512  # llama-server's default micro-batch: the output device also holds its logits (n_vocab x UBATCH x f32)
LEADER_OVERHEAD = GiB  # the leader process besides token_embd: compute buffer, logits, the server itself
RPC_MEMORY_STEP = 8 * GiB  # rpc pod limit is rounded up to this: a few hundred MiB of MemTotal drift between boots must
# not change the DaemonSet template, which would recycle the rpc pods and so reload every group
RPC_MEMORY_MARGIN = GiB  # rpc pod limit above its RAM budget, besides one tensor (SET_TENSOR is read into a buffer)
# CUDA compute buffer of one device (activations of a micro-batch), kept free on every GPU besides NVIDIA_RESERVE;
# the floor is the setting GPU_COMPUTE_RESERVE_MIB, a very wide model raises it
DEFAULT_GPU_COMPUTE_RESERVE_MIB = 1024
ACTIVATION_TENSORS = 16  # f32 tensors of [n_ubatch, n_embd] alive at once in one layer's graph (generous)
DEFAULT_RAM_BUDGET_PCT = 70
MIN_RAM_BUDGET_PCT = 10
MAX_RAM_BUDGET_PCT = 90
GPU_DEVICE = "CUDA0"
CPU_DEVICE = "CPU"
SPLIT, AUTO, HOLD = "split", "auto", "hold"  # what a leader gets: our --tensor-split, llama.cpp's own split, nothing


@dataclass(frozen=True)
class NodeMemory:
    name: str
    accel: str  # cpu | nvidia
    vram_mib: int | None
    ram_mib: int | None  # memory the node's WSL/host gives to the node (see `effective_ram_mib`)


@dataclass
class DevicePlan:
    node: str
    device: str  # CUDA0 | CPU
    budget_bytes: int
    layers: int = 0
    bytes: int = 0


@dataclass
class SplitPlan:
    devices: list[DevicePlan]
    fits: bool
    need_bytes: int = 0  # all layers with their KV cache

    @property
    def budget_bytes(self) -> int:
        return sum(d.budget_bytes for d in self.devices)

    @property
    def nodes(self) -> list[str]:
        return list(dict.fromkeys(d.node for d in self.devices))

    @property
    def tensor_split(self) -> list[int]:
        return [d.layers for d in self.devices]

    @property
    def ram_bytes(self) -> int:
        return sum(d.bytes for d in self.devices if d.device == CPU_DEVICE)


def effective_ram_mib(label_mib: int | None, capacity_mib: int | None) -> int | None:
    """RAM of a node: the `llm.nsu/ram-mib` label, never more than the kubelet reports (a label is set only when the
    node registers, so it may be stale)."""
    known = [v for v in (label_mib, capacity_mib) if v is not None]
    return min(known) if known else None


def ram_budget_bytes(ram_mib: int, pct: int) -> int:
    return ram_mib * MiB * pct // 100


def rpc_devices(accel: str, use_node_ram: bool) -> str:
    """The `-d` list of ggml-rpc-server; it fixes the device order of `--tensor-split`."""
    if accel == "nvidia":
        return f"{GPU_DEVICE},{CPU_DEVICE}" if use_node_ram else GPU_DEVICE
    return CPU_DEVICE


def gpu_compute_reserve_bytes(meta: dict[str, Any] | None, setting_mib: int = DEFAULT_GPU_COMPUTE_RESERVE_MIB) -> int:
    """Compute buffer kept free on each GPU: the setting, or more for a model whose micro-batch activations
    (`UBATCH x n_embd x f32 x ACTIVATION_TENSORS`) are larger."""
    embedding = int((meta or {}).get("embedding") or 0)
    return max(setting_mib * MiB, UBATCH * embedding * 4 * ACTIVATION_TENSORS)


def _gpu_budget(node: NodeMemory, reserve: int) -> int | None:
    return None if node.vram_mib is None else max(0, node.vram_mib * MiB - NVIDIA_RESERVE - reserve)


def split_capacity_bytes(
    node: NodeMemory, use_node_ram: bool, ram_pct: int, gpu_reserve: int = DEFAULT_GPU_COMPUTE_RESERVE_MIB * MiB
) -> int | None:
    """What one node gives a split model: VRAM without the reserves plus, when used, the RAM budget."""
    total = 0
    for device in rpc_devices(node.accel, use_node_ram).split(","):
        if device == GPU_DEVICE:
            budget = _gpu_budget(node, gpu_reserve)
        else:
            budget = None if node.ram_mib is None else ram_budget_bytes(node.ram_mib, ram_pct)
        if budget is None:
            return None
        total += budget
    return total


def layer_costs(meta: dict[str, Any], ctx_total: int, parallel: int = 1, kv_cache_type: str | None = "f16") -> list[int]:
    """Bytes of every layer position, the output layer last: weights plus its KV cache (a sliding-window layer its
    window per slot, a layer sharing an earlier layer's KV none); MTP layers cost nothing.

    The KV bytes follow `kv_cache_type` (f16, q8_0, q4_0). Without a layer layout (a deployment from before it was
    read) the weights are spread evenly."""
    gmeta = meta_from_dict(meta)
    n_blocks = meta["n_layers"]
    n_main = cached_layers(gmeta)
    kv = kv_layers_bytes(gmeta, ctx_total, parallel, kv_elem_bytes(kv_cache_type))
    layers = meta.get("layers")
    if layers and len(layers) == n_blocks:
        weights, output = list(layers), int(meta.get("output_bytes") or 0)
    else:
        per = meta["weights_bytes"] // (n_main + 1)
        weights, output = [per] * n_blocks, per
    costs = [weights[i] + kv[i] if i < n_main else 0 for i in range(n_blocks)]
    return costs + [output + int(meta.get("n_vocab") or 0) * UBATCH * 4]


def _devices(
    nodes: list[NodeMemory], use_node_ram: bool, ram_pct: int, embd_bytes: int, gpu_reserve: int, leader_extra: int = 0,
    leader_on_main: bool = False,
) -> list[DevicePlan] | None:
    devices: list[DevicePlan] = []
    for i, node in enumerate(nodes):
        for device in rpc_devices(node.accel, use_node_ram).split(","):
            if device == GPU_DEVICE:
                budget = _gpu_budget(node, gpu_reserve)
            else:
                budget = None if node.ram_mib is None else ram_budget_bytes(node.ram_mib, ram_pct)
                if budget is not None and i == 0 and not leader_on_main:
                    budget = max(0, budget - embd_bytes - LEADER_OVERHEAD - leader_extra)  # the leader runs on this node
                if budget is not None and i == len(nodes) - 1 and node.accel == "nvidia":
                    budget = 0  # so the output layer lands on the last GPU, not on its CPU
            if budget is None:
                return None
            devices.append(DevicePlan(node.name, device, budget))
    return devices


def _targets(total: int, devices: list[DevicePlan]) -> list[float]:
    """Bytes each device should get: GPUs first, in proportion to their budgets; only what does not fit there goes
    to RAM, again in proportion to the budgets."""
    gpu = sum(d.budget_bytes for d in devices if d.device == GPU_DEVICE)
    cpu = sum(d.budget_bytes for d in devices if d.device == CPU_DEVICE)
    gpu_share = min(1.0, total / gpu) if gpu else 0.0
    rest = max(0, total - gpu)
    cpu_share = rest / cpu if cpu else 0.0
    return [d.budget_bytes * (gpu_share if d.device == GPU_DEVICE else cpu_share) for d in devices]


def _greedy_end(costs: list[int], start: int, budgets: list[int]) -> int:
    """Index of the first layer left over when `budgets` take as many layers from `start` as they can, in order."""
    i = start
    for budget in budgets:
        used = 0
        while i < len(costs) and used + costs[i] <= budget:
            used += costs[i]
            i += 1
    return i


def place(costs: list[int], budgets: list[int], targets: list[float]) -> list[int] | None:
    """Whole layers per device, in order: each device stops at the layer boundary nearest to its cumulative target,
    within its budget and never so early that the rest no longer fits the devices after it. None: does not fit."""
    n = len(costs)
    counts: list[int] = []
    i = 0
    done = 0  # bytes placed so far
    goal = 0.0
    for d, budget in enumerate(budgets):
        goal += targets[d]
        hi, used = i, 0
        while hi < n and used + costs[hi] <= budget:
            used += costs[hi]
            hi += 1
        # fewest layers this device may take: the later devices must hold the rest (monotonic, so binary search)
        rest = budgets[d + 1 :]
        lo_j, hi_j = i, hi
        if _greedy_end(costs, hi, rest) < n:
            return None
        while lo_j < hi_j:
            mid = (lo_j + hi_j) // 2
            if _greedy_end(costs, mid, rest) == n:
                hi_j = mid
            else:
                lo_j = mid + 1
        j, acc = i, done
        while j < hi and acc + costs[j] / 2 <= goal:
            acc += costs[j]
            j += 1
        j = max(j, lo_j)
        counts.append(j - i)
        done += sum(costs[i:j])
        i = j
    return counts if i == n else None


def plan_split(
    meta: dict[str, Any] | None,
    nodes: list[NodeMemory],
    ctx_total: int,
    use_node_ram: bool,
    ram_pct: int,
    gpu_compute_reserve_mib: int = DEFAULT_GPU_COMPUTE_RESERVE_MIB,
    parallel: int = 1,
    kv_cache_type: str | None = "f16",
    leader_extra_bytes: int = 0,
    leader_on_main: bool = False,
) -> SplitPlan | None:
    """Placement of one group (`nodes` in `--rpc` order, leader first). None when the model or a needed memory size
    is unknown; a plan that does not fit has `fits = False` and zero counts.

    `leader_extra_bytes`: the draft model (weights and KV) and the image projector, which only the leader loads, into
    the RAM of the leader's node (the leader has no GPU of its own: its draft runs with `-ngld 0`). `leader_on_main` (setting
    split_leader_on_main): the leader is on main, so all of that stays out of the budget of the group's first node."""
    if meta is None or not nodes:
        return None
    reserve = gpu_compute_reserve_bytes(meta, gpu_compute_reserve_mib)
    devices = _devices(
        nodes, use_node_ram, ram_pct, int(meta.get("embd_bytes") or 0), reserve, leader_extra_bytes, leader_on_main
    )
    if devices is None:
        return None
    costs = layer_costs(meta, ctx_total, parallel, kv_cache_type)
    counts = place(costs, [d.budget_bytes for d in devices], _targets(sum(costs), devices))
    if counts is None:
        return SplitPlan(devices, fits=False, need_bytes=sum(costs))
    i = 0
    for device, count in zip(devices, counts, strict=True):
        device.layers, device.bytes = count, sum(costs[i : i + count])
        i += count
    return SplitPlan(devices, fits=True, need_bytes=sum(costs))


def plan_for(
    dep, nodes: list[NodeMemory], gpu_compute_reserve_mib: int = DEFAULT_GPU_COMPUTE_RESERVE_MIB, leader_on_main: bool = False
) -> SplitPlan | None:
    """`plan_split` with the settings of a deployment; the controller and the status page use the same one."""
    return plan_split(
        dep.meta, nodes, dep.ctx_per_request * dep.parallel, dep.use_node_ram, dep.ram_budget_pct,
        gpu_compute_reserve_mib, dep.parallel, getattr(dep, "kv_cache_type", None), sum(dep_extras(dep)), leader_on_main,
    )


def decide(plan: SplitPlan | None) -> str:
    """SPLIT: start the leader with the plan. HOLD: the group uses RAM and still does not fit, the leader is not
    started (it would fill the RAM devices until the OOM killer stops it). AUTO: no plan (memory unknown) or no RAM
    device in the group: llama.cpp splits by itself and a failed GPU allocation ends the load with a clear error."""
    if plan is None:
        return AUTO
    if plan.fits:
        return SPLIT
    return HOLD if any(d.device == CPU_DEVICE for d in plan.devices) else AUTO


def fmt_gb(n: int) -> str:
    return f"{n / GiB:.1f}".replace(".", ",") + " ГБ"


def misfit_message(plan: SplitPlan) -> str:
    return (
        f"Группа {', '.join(plan.nodes)} не запущена: модель не помещается в память нод "
        f"(нужно {fmt_gb(plan.need_bytes)}, есть {fmt_gb(plan.budget_bytes)})"
    )


def rpc_memory_limit_mib(
    meta: dict[str, Any] | None, nodes: list[NodeMemory], accel: str, use_node_ram: bool, ram_pct: int
) -> int | None:
    """Memory limit of the rpc pods of one accel: the largest RAM budget among `nodes` (all cluster nodes of that
    accel, so membership changes do not change it) plus one tensor and a margin, in steps of RPC_MEMORY_STEP (one DaemonSet serves
    all those nodes). None: the pods use no RAM device, or no RAM size is known.

    The pods request only RPC_MEMORY_REQUEST (builders): a request at the budget would leave smaller nodes
    unschedulable. The price: under node memory pressure the kubelet evicts pods that use more than they request
    first, so an rpc pod is the first to go; the RAM budget (70 % by default) leaves the rest of the node's memory
    so that this pressure does not arise."""
    if CPU_DEVICE not in rpc_devices(accel, use_node_ram).split(","):
        return None
    known = [n.ram_mib for n in nodes if n.accel == accel and n.ram_mib is not None]
    if not known:
        return None
    biggest = int((meta or {}).get("max_tensor_bytes") or 0)
    limit = ram_budget_bytes(max(known), ram_pct) + biggest + RPC_MEMORY_MARGIN
    return math.ceil(limit / RPC_MEMORY_STEP) * (RPC_MEMORY_STEP // MiB)
