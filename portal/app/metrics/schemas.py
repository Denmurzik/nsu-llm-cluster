"""What a node sends (POST /api/nodes/{name}/metrics) and the flat sample the portal keeps."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

MAX_GPUS = 16


class GpuIn(BaseModel):
    util_pct: float = Field(ge=0, le=100)
    vram_used_mib: int = Field(ge=0, le=100_000_000)
    vram_total_mib: int = Field(ge=0, le=100_000_000)
    temp_c: float | None = Field(default=None, ge=0, le=200)
    power_w: float | None = Field(default=None, ge=0, le=5000)


class NodeMetricsIn(BaseModel):
    gpus: list[GpuIn] = Field(default_factory=list, max_length=MAX_GPUS)
    ram_used_mib: int = Field(ge=0, le=100_000_000)
    ram_total_mib: int = Field(ge=0, le=100_000_000)
    cpu_pct: float = Field(ge=0, le=100)
    models_bytes: int | None = Field(default=None, ge=0)  # the model cache of the node (2-26 reports the same number)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    llama_up: bool = False  # a llama-server process runs in the container
    rpc_up: bool = False  # a ggml-rpc-server process runs in the container

    def sample(self, now: datetime | None = None) -> dict[str, Any]:
        """One flat dict: the GPUs of a node are summed (VRAM, power), averaged (utilisation) or maxed (temperature)."""
        now = now or datetime.now(UTC)
        gpus = self.gpus
        temps = [g.temp_c for g in gpus if g.temp_c is not None]
        powers = [g.power_w for g in gpus if g.power_w is not None]
        return {
            "at": now.isoformat(),
            "gpu_count": len(gpus),
            "gpu_util": round(sum(g.util_pct for g in gpus) / len(gpus), 1) if gpus else None,
            "vram_used_mib": sum(g.vram_used_mib for g in gpus) if gpus else None,
            "vram_total_mib": sum(g.vram_total_mib for g in gpus) if gpus else None,
            "gpu_temp_c": max(temps) if temps else None,
            "power_w": round(sum(powers), 1) if powers else None,
            "ram_used_mib": self.ram_used_mib,
            "ram_total_mib": self.ram_total_mib,
            "cpu_pct": round(self.cpu_pct, 1),
            "models_bytes": self.models_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "llama_up": self.llama_up,
            "rpc_up": self.rpc_up,
        }
