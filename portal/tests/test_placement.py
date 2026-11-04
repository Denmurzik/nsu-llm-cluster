"""Layer placement of a split group over GPU and RAM of its nodes (`--tensor-split` counts)."""

from app.catalog import placement as pl
from app.catalog.estimate import NVIDIA_RESERVE

MiB = 2**20
GiB = 2**30


def meta(n_layers=10, layer=GiB, output=GiB // 2, embd=GiB // 4, nextn=0, n_vocab=1000, **kw):
    """A stored deployment meta with a known layer layout; KV is tiny (1 head of 8) unless overridden."""
    base = {
        "weights_bytes": n_layers * layer + output + embd, "arch": "llama", "n_layers": n_layers, "n_heads": 1,
        "n_kv_heads": 1, "head_dim": 8, "max_ctx": 8192, "value_dim": None, "mla": False, "nextn_layers": nextn,
        "layers": [layer] * n_layers, "output_bytes": output, "embd_bytes": embd, "max_tensor_bytes": layer // 2,
        "n_vocab": n_vocab,
    }
    return {**base, **kw}


def gpu(name, vram_mib=12282, ram_mib=28000):
    return pl.NodeMemory(name, "nvidia", vram_mib, ram_mib)


def cpu(name, ram_mib=28000):
    return pl.NodeMemory(name, "cpu", None, ram_mib)


def test_devices_follow_rpc_then_server_order_and_the_last_cpu_gets_nothing():
    plan = pl.plan_split(meta(), [gpu("a"), gpu("b")], ctx_total=1024, use_node_ram=True, ram_pct=70)
    assert [(d.node, d.device) for d in plan.devices] == [("a", "CUDA0"), ("a", "CPU"), ("b", "CUDA0"), ("b", "CPU")]
    assert plan.devices[-1].budget_bytes == 0 and plan.devices[-1].layers == 0
    assert plan.devices[0].budget_bytes == 12282 * MiB - NVIDIA_RESERVE - pl.DEFAULT_GPU_COMPUTE_RESERVE_MIB * MiB
    leader_ram = 28000 * MiB * 70 // 100 - (GiB // 4 + pl.LEADER_OVERHEAD)
    assert plan.devices[1].budget_bytes == leader_ram  # the leader's own process lives in that RAM too
    assert len(plan.tensor_split) == 4 and sum(plan.tensor_split) == 10 + 1  # every block plus the output layer


def test_a_model_that_fits_in_vram_leaves_the_ram_empty_and_balances_the_gpus():
    plan = pl.plan_split(meta(n_layers=10), [gpu("a"), gpu("b")], ctx_total=1024, use_node_ram=True, ram_pct=70)
    assert plan.fits and plan.ram_bytes == 0
    assert plan.tensor_split == [5, 0, 6, 0]  # the output layer goes with the last blocks to b's GPU
    assert all(d.bytes <= d.budget_bytes for d in plan.devices)


def test_overflow_goes_to_ram_in_proportion_to_the_budgets():
    # 34 GiB of blocks on two GPUs of 9.5 GiB budget: the rest lives in RAM, all of it on the first node's CPU
    plan = pl.plan_split(meta(n_layers=34), [gpu("a"), gpu("b")], ctx_total=1024, use_node_ram=True, ram_pct=70)
    assert plan.fits and plan.ram_bytes > 16 * GiB
    g1, c1, g2, c2 = plan.devices
    assert (g1.layers, c1.layers, g2.layers, c2.layers) == (9, 17, 9, 0)  # b's GPU: 8 blocks and the output
    assert all(d.bytes <= d.budget_bytes for d in plan.devices)


def test_three_nodes_spread_the_ram_share_over_all_but_the_last_cpu():
    plan = pl.plan_split(meta(n_layers=60), [gpu("a"), gpu("b"), gpu("c")], 1024, True, 70)
    assert plan.fits
    a_cpu, b_cpu, c_cpu = plan.devices[1], plan.devices[3], plan.devices[5]
    assert a_cpu.layers > 0 and b_cpu.layers > 0 and c_cpu.layers == 0
    assert b_cpu.layers >= a_cpu.layers  # the leader's RAM budget is smaller


def test_without_node_ram_only_the_gpus_count():
    nodes = [gpu("a"), gpu("b")]
    plan = pl.plan_split(meta(n_layers=10), nodes, 1024, use_node_ram=False, ram_pct=70)
    assert [d.device for d in plan.devices] == ["CUDA0", "CUDA0"] and plan.fits and plan.tensor_split == [5, 6]
    too_big = pl.plan_split(meta(n_layers=40), nodes, 1024, use_node_ram=False, ram_pct=70)
    assert not too_big.fits and too_big.tensor_split == [0, 0]


def test_cpu_nodes_always_use_their_ram_and_the_last_one_holds_the_output():
    plan = pl.plan_split(meta(n_layers=10), [cpu("a"), cpu("b")], 1024, use_node_ram=False, ram_pct=70)
    assert [d.device for d in plan.devices] == ["CPU", "CPU"]
    assert plan.fits and plan.devices[1].layers > 0 and sum(plan.tensor_split) == 11


def test_mtp_layers_cost_nothing_but_keep_their_position():
    m = meta(n_layers=10, nextn=1)
    plan = pl.plan_split(m, [gpu("a"), gpu("b")], 1024, True, 70)
    costs = pl.layer_costs(m, 1024)
    assert costs[9] == 0 and costs[8] > 0  # block 9 is the MTP layer: not loaded, no KV cache
    assert sum(plan.tensor_split) == 11 and plan.fits
    assert sum(d.bytes for d in plan.devices) == sum(costs)


def test_kv_cache_is_part_of_each_layer():
    m = meta(n_layers=4, layer=GiB, n_kv_heads=8, head_dim=128)
    costs = pl.layer_costs(m, ctx_total=65536)
    assert costs[0] == GiB + (128 + 128) * 8 * 65536 * 2
    assert costs[-1] == GiB // 2 + 1000 * pl.UBATCH * 4  # output head plus the logits of a micro-batch


def test_sliding_window_and_shared_layers_carry_their_own_kv():
    """Gemma 4: global layers cache the whole context, sliding-window layers the window per slot, shared layers none."""
    pattern = [True, True, False, True, True, False]
    m = meta(n_layers=6, layer=GiB, n_kv_heads=2, head_dim=512, value_dim=512, n_swa=512, swa_layers=pattern,
             head_dim_swa=256, value_dim_swa=256, kv_layers=4)
    costs = pl.layer_costs(m, ctx_total=65536, parallel=2)
    full, swa = (512 + 512) * 2 * 65536 * 2, (256 + 256) * 2 * 1024 * 2 * 2
    assert costs[:6] == [GiB + swa, GiB + swa, GiB + full, GiB + swa, GiB, GiB]


def test_a_meta_without_layout_falls_back_to_equal_layers():
    m = meta(n_layers=10)
    for key in ("layers", "output_bytes", "embd_bytes", "max_tensor_bytes", "n_vocab"):
        m.pop(key)
    costs = pl.layer_costs(m, 1024)
    assert len(costs) == 11 and len(set(costs[:10])) == 1
    assert pl.plan_split(m, [gpu("a"), gpu("b")], 1024, True, 70).fits


def test_unknown_memory_gives_no_plan():
    assert pl.plan_split(meta(), [gpu("a", vram_mib=None), gpu("b")], 1024, False, 70) is None
    assert pl.plan_split(meta(), [gpu("a", ram_mib=None), gpu("b")], 1024, True, 70) is None
    assert pl.plan_split(meta(), [gpu("a", ram_mib=None), gpu("b")], 1024, False, 70) is not None
    assert pl.plan_split(None, [gpu("a"), gpu("b")], 1024, True, 70) is None


def test_a_model_larger_than_the_group_does_not_fit():
    plan = pl.plan_split(meta(n_layers=200), [gpu("a"), gpu("b")], 1024, True, 70)
    assert not plan.fits and plan.tensor_split == [0, 0, 0, 0]


def test_ram_budget_and_effective_ram():
    assert pl.ram_budget_bytes(28000, 70) == 28000 * MiB * 70 // 100
    assert pl.effective_ram_mib(28000, 27900) == 27900  # the label never claims more than the kubelet sees
    assert pl.effective_ram_mib(None, 27900) == 27900 and pl.effective_ram_mib(28000, None) == 28000
    assert pl.effective_ram_mib(None, None) is None


def test_rpc_memory_limit_covers_the_largest_budget_and_one_tensor():
    m = meta()
    nodes = [gpu("a", ram_mib=16000), gpu("b", ram_mib=28000)]
    limit = pl.rpc_memory_limit_mib(m, nodes, "nvidia", use_node_ram=True, ram_pct=70)
    want = pl.ram_budget_bytes(28000, 70) + m["max_tensor_bytes"] + pl.RPC_MEMORY_MARGIN
    assert limit == -(-want // (8 * GiB)) * 8192  # 8 GiB steps: a small MemTotal change does not restart the pods
    assert pl.rpc_memory_limit_mib(m, nodes, "nvidia", use_node_ram=False, ram_pct=70) is None
    assert pl.rpc_memory_limit_mib(m, [cpu("c", ram_mib=None)], "cpu", True, 70) is None
    assert pl.rpc_memory_limit_mib(None, [cpu("c")], "cpu", True, 70) is not None


def test_rpc_memory_limit_steps_in_8_gib_so_a_small_memtotal_change_keeps_it():
    m = meta()
    base = pl.rpc_memory_limit_mib(m, [gpu("a", ram_mib=28000)], "nvidia", True, 70)
    assert base % 8192 == 0
    for ram in (27900, 27500, 28300):  # the kubelet's MemTotal drifts by a few hundred MiB between boots
        assert pl.rpc_memory_limit_mib(m, [gpu("a", ram_mib=ram)], "nvidia", True, 70) == base
    assert pl.rpc_memory_limit_mib(m, [gpu("a", ram_mib=64000)], "nvidia", True, 70) > base


def test_rpc_devices_per_accel():
    assert pl.rpc_devices("nvidia", True) == "CUDA0,CPU"
    assert pl.rpc_devices("nvidia", False) == "CUDA0"
    assert pl.rpc_devices("cpu", False) == "CPU" == pl.rpc_devices("cpu", True)


def test_split_capacity_adds_the_ram_budget():
    vram = 12282 * MiB - NVIDIA_RESERVE - pl.DEFAULT_GPU_COMPUTE_RESERVE_MIB * MiB
    assert pl.split_capacity_bytes(gpu("a"), True, 70) == vram + pl.ram_budget_bytes(28000, 70)
    assert pl.split_capacity_bytes(gpu("a"), False, 70) == vram
    assert pl.split_capacity_bytes(cpu("c"), False, 70) == pl.ram_budget_bytes(28000, 70)
    assert pl.split_capacity_bytes(gpu("a", vram_mib=None), True, 70) is None


def test_gpu_compute_reserve_keeps_a_margin_when_the_gpus_fill():
    # 34 GiB of blocks on two 12 GB cards: both GPUs are filled, each still leaves the compute buffer free
    plan = pl.plan_split(meta(n_layers=34), [gpu("a"), gpu("b")], 1024, True, 70, gpu_compute_reserve_mib=1024)
    assert plan.fits
    for d in plan.devices:
        if d.device == "CUDA0":
            assert d.budget_bytes == 12282 * MiB - NVIDIA_RESERVE - 1024 * MiB
            assert 12282 * MiB - NVIDIA_RESERVE - d.bytes >= 1024 * MiB
    assert pl.DEFAULT_GPU_COMPUTE_RESERVE_MIB == 1024


def test_compute_reserve_grows_for_a_wide_model():
    assert pl.gpu_compute_reserve_bytes(meta(embedding=4096), 1024) == 1024 * MiB  # the setting is the floor
    wide = pl.gpu_compute_reserve_bytes(meta(embedding=65536), 1024)
    assert wide == pl.UBATCH * 65536 * 4 * pl.ACTIVATION_TENSORS > 1024 * MiB
    assert pl.gpu_compute_reserve_bytes(meta(), 512) == 512 * MiB  # no embedding stored: the setting


def test_a_group_with_ram_that_does_not_fit_is_held_with_the_reason():
    nodes = [gpu("a"), gpu("b")]
    fits = pl.plan_split(meta(n_layers=10), nodes, 1024, True, 70)
    assert pl.decide(fits) == pl.SPLIT
    big = pl.plan_split(meta(n_layers=200), nodes, 1024, True, 70)
    assert pl.decide(big) == pl.HOLD
    assert big.need_bytes > big.budget_bytes
    assert pl.misfit_message(big) == (
        f"Группа a, b не запущена: модель не помещается в память нод (нужно {pl.fmt_gb(big.need_bytes)}, "
        f"есть {pl.fmt_gb(big.budget_bytes)})"
    )
    assert pl.fmt_gb(250 * GiB + GiB // 2) == "250,5 ГБ"
    # no RAM devices (VRAM only) or memory unknown: llama.cpp splits by itself and reports the error itself
    assert pl.decide(pl.plan_split(meta(n_layers=200), nodes, 1024, False, 70)) == pl.AUTO
    assert pl.decide(None) == pl.AUTO
