"""Process settings for the model manager, read once from the environment (contract §1, §3).

Plain `os.environ`: the manager has no secret to read and pydantic-settings is not in its
dependency set. Every name and default is listed here so the compose file and this module
cannot drift apart silently.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from slas_container.api import DEFAULT_SOCKET
from slas_http.serve import DEFAULT_BIND
from slas_model_manager.controller import DEFAULT_MODELS_FILE, DEFAULT_RECONCILE_INTERVAL_S
from slas_model_manager.driver import DEFAULT_SHM_BYTES
from slas_model_manager.placement import DEFAULT_GPU_VRAM_GIB

DEFAULT_GATEWAY_URL: Final = "http://llm-gateway:8000"
DEFAULT_GPU_IDS: Final = "0,1,2,3"
DEFAULT_INFERENCE_NETWORK: Final = "slas_slas-inference"
DEFAULT_MODEL_START_TIMEOUT_S: Final = 900.0
DEFAULT_HOST_DATA_ROOT: Final = "/AI/Agent"

_SIZE: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
_UNITS: Final = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(text: str, *, default: int = DEFAULT_SHM_BYTES) -> int:
    """`16g`, `16GiB`, `512m`, `17179869184` → bytes; anything else is the default."""
    match = _SIZE.match(text or "")
    if match is None:
        return default
    number, unit = match.groups()
    return int(float(number) * _UNITS[unit.lower()])


def parse_gpu_ids(text: str) -> list[int]:
    ids: list[int] = []
    for part in (text or "").split(","):
        part = part.strip()
        if part.isdigit() and int(part) not in ids:
            ids.append(int(part))
    return ids


@dataclass(frozen=True)
class Settings:
    runtime_socket: str = DEFAULT_SOCKET
    models_file: Path = Path(DEFAULT_MODELS_FILE)
    gateway_url: str = DEFAULT_GATEWAY_URL
    gpu_ids: tuple[int, ...] = (0, 1, 2, 3)
    gpu_vram_gib: float = DEFAULT_GPU_VRAM_GIB
    host_models_dir: str = f"{DEFAULT_HOST_DATA_ROOT}/Models"
    inference_network: str = DEFAULT_INFERENCE_NETWORK
    vllm_image: str = ""
    vllm_shm_bytes: int = DEFAULT_SHM_BYTES
    reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S
    model_start_timeout_s: float = DEFAULT_MODEL_START_TIMEOUT_S
    #: Start the coder's instance alone and the rest once it answers, so the Coding Agent's
    #: model is not slowed by six other instances reading their weights at the same time.
    start_coder_first: bool = True
    bind: str = DEFAULT_BIND

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def text(name: str, default: str) -> str:
            value = env.get(name, "").strip()
            return value or default

        def number(name: str, default: float) -> float:
            try:
                return float(text(name, str(default)))
            except ValueError:
                return default

        data_root = text("SLAS_DATA_ROOT", DEFAULT_HOST_DATA_ROOT).rstrip("/")
        return cls(
            runtime_socket=text("SLAS_RUNTIME_SOCKET", DEFAULT_SOCKET),
            models_file=Path(text("SLAS_MODELS_FILE", DEFAULT_MODELS_FILE)),
            gateway_url=text("SLAS_GATEWAY_URL", DEFAULT_GATEWAY_URL),
            gpu_ids=tuple(parse_gpu_ids(text("SLAS_GPU_IDS", DEFAULT_GPU_IDS))),
            gpu_vram_gib=number("SLAS_GPU_VRAM_GIB", DEFAULT_GPU_VRAM_GIB),
            host_models_dir=text("SLAS_HOST_MODELS_DIR", f"{data_root}/Models"),
            inference_network=text("SLAS_INFERENCE_NETWORK", DEFAULT_INFERENCE_NETWORK),
            vllm_image=text("SLAS_VLLM_IMAGE", ""),
            vllm_shm_bytes=parse_size(text("SLAS_VLLM_SHM", "16g")),
            reconcile_interval_s=number("SLAS_RECONCILE_INTERVAL_S", DEFAULT_RECONCILE_INTERVAL_S),
            model_start_timeout_s=number(
                "SLAS_MODEL_START_TIMEOUT_S", DEFAULT_MODEL_START_TIMEOUT_S
            ),
            start_coder_first=text("SLAS_START_CODER_FIRST", "1").lower()
            not in ("0", "false", "no"),
            bind=text("SLAS_BIND", DEFAULT_BIND),
        )
