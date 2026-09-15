"""`Models/models.yaml`: one registry of models, role assignments and voters (CLAUDE.md §7)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator

from slas_llm_gateway.routing import ROLES, Routes
from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage

Quant = Literal["fp8", "awq4", "bf16"]
QUANT_LABELS: Final[dict[str, str]] = {"fp8": "FP8", "awq4": "AWQ 4-bit", "bf16": "BF16"}


class ModelEntry(SlasModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    display_name: str = Field(min_length=1)
    family: str = Field(
        min_length=1, description="Qwen, DeepSeek, Kimi… used to decorrelate voters"
    )
    path: str = Field(min_length=1, description="Directory under Models/, never a URL (INV-1)")
    quant: Quant
    vram_gib: float = Field(gt=0, description="GPU memory the weights and KV cache need")
    context: int = Field(ge=1024)
    roles: list[str] = Field(default_factory=list, description="Roles this model may serve")

    @model_validator(mode="after")
    def _roles_known_and_path_local(self) -> ModelEntry:
        unknown = sorted(set(self.roles) - set(ROLES))
        if unknown:
            raise ValueError(f"{self.id}: unknown roles {', '.join(unknown)}")
        if "://" in self.path:
            raise ValueError(f"{self.id}: path must be a local directory, not a URL")
        return self

    def label(self) -> str:
        return f"{self.display_name} ({QUANT_LABELS[self.quant]})"


class Registry(SlasModel):
    version: int = 1
    models: list[ModelEntry] = Field(min_length=1)
    roles: dict[str, str] = Field(default_factory=dict, description="role → model id")
    voters: list[str] = Field(default_factory=list, description="model ids, different families")

    @model_validator(mode="after")
    def _consistent(self) -> Registry:
        ids = [model.id for model in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("model ids must be unique")
        known = set(ids)
        for role, model_id in self.roles.items():
            if role not in ROLES:
                raise ValueError(f"unknown role {role!r}; roles are {', '.join(ROLES)}")
            if model_id not in known:
                raise ValueError(f"role {role} names an unknown model {model_id!r}")
            if role not in self.model(model_id).roles:
                raise ValueError(f"model {model_id} is not declared for the {role} role")
        for model_id in self.voters:
            if model_id not in known:
                raise ValueError(f"voter {model_id!r} is not a model in this registry")
        if len(set(self.voters)) != len(self.voters):
            raise ValueError("voters must be distinct models")
        return self

    def model(self, model_id: str) -> ModelEntry:
        for entry in self.models:
            if entry.id == model_id:
                return entry
        raise KeyError(model_id)

    def voter_families(self) -> list[str]:
        return [self.model(model_id).family for model_id in self.voters]

    def routes(self) -> Routes:
        """The gateway's routes: instance names are `vllm-<role>` and `vllm-voter-<id>`."""
        return Routes(
            roles={role: instance_name(role) for role in self.roles},
            voters=[voter_instance_name(model_id) for model_id in self.voters],
        )

    def sentence(self) -> str:
        served = ", ".join(
            f"{role} → {self.model(m).display_name}" for role, m in self.roles.items()
        )
        voters = len(self.voters)
        families = len(set(self.voter_families()))
        return (
            f"{len(self.models)} models; {served or 'no roles assigned'}; "
            f"{voters} voters from {families} model {'family' if families == 1 else 'families'}."
        )


def instance_name(role: str) -> str:
    return f"vllm-{role}"


def voter_instance_name(model_id: str) -> str:
    return f"vllm-voter-{model_id}"


class RegistryError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def registry_from_mapping(data: object, *, source: str = "<memory>") -> Registry:
    try:
        return Registry.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(
            ThreePartMessage(
                f"The model registry {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source} on the Models page or by hand; the format is in "
                "services/model-manager/README.md.",
            )
        ) from exc


EXAMPLE_REGISTRY: Final[dict[str, object]] = {
    "version": 1,
    "models": [
        {
            "id": "qwen2.5-coder-32b-awq",
            "display_name": "Qwen2.5-Coder-32B",
            "family": "Qwen",
            "path": "qwen2.5-coder-32b-awq",
            "quant": "awq4",
            "vram_gib": 22.0,
            "context": 32768,
            "roles": ["coder", "planner"],
        },
        {
            "id": "deepseek-v3-fp8",
            "display_name": "DeepSeek-V3",
            "family": "DeepSeek",
            "path": "deepseek-v3-fp8",
            "quant": "fp8",
            "vram_gib": 70.0,
            "context": 65536,
            "roles": ["planner"],
        },
        {
            "id": "kimi-k2-awq",
            "display_name": "Kimi K2",
            "family": "Kimi",
            "path": "kimi-k2-awq",
            "quant": "awq4",
            "vram_gib": 60.0,
            "context": 65536,
            "roles": ["planner"],
        },
        {
            "id": "qwen2.5-7b-awq",
            "display_name": "Qwen2.5-7B",
            "family": "Qwen",
            "path": "qwen2.5-7b-awq",
            "quant": "awq4",
            "vram_gib": 7.0,
            "context": 32768,
            "roles": ["triage"],
        },
        {
            "id": "bge-m3",
            "display_name": "BGE-M3",
            "family": "BAAI",
            "path": "bge-m3",
            "quant": "bf16",
            "vram_gib": 3.0,
            "context": 8192,
            "roles": ["embed"],
        },
        {
            "id": "bge-reranker-v2-m3",
            "display_name": "BGE Reranker v2 M3",
            "family": "BAAI",
            "path": "bge-reranker-v2-m3",
            "quant": "bf16",
            "vram_gib": 2.0,
            "context": 8192,
            "roles": ["rerank"],
        },
    ],
    "roles": {
        "coder": "qwen2.5-coder-32b-awq",
        "planner": "deepseek-v3-fp8",
        "triage": "qwen2.5-7b-awq",
        "embed": "bge-m3",
        "rerank": "bge-reranker-v2-m3",
    },
    "voters": ["qwen2.5-coder-32b-awq", "deepseek-v3-fp8", "kimi-k2-awq"],
}

REGISTRY_FILE_HEADER: Final = (
    "Example Models/models.yaml for SW Local Agent Service (CLAUDE.md §7).\n"
    "Rendered from slas_model_manager.registry.EXAMPLE_REGISTRY; a unit test keeps file and\n"
    "code in step. Paths are directories under ${SLAS_DATA_ROOT}/Models, never URLs (INV-1).\n"
    "Voters should come from different model families so their errors decorrelate (§5.3).\n"
    "Model ids, sizes and quantisations here are illustrative; `slas model scan` fills in a\n"
    "real installation's registry from the weights on disk."
)


def example_registry() -> Registry:
    return registry_from_mapping(EXAMPLE_REGISTRY, source="models.example.yaml")


def render_registry_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    registry = registry_from_mapping(data)
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {registry.version}")
    lines.append("models:")
    for model in registry.models:
        lines.append(f"  - id: {model.id}")
        lines.append(f"    display_name: {json.dumps(model.display_name, ensure_ascii=False)}")
        lines.append(f"    family: {json.dumps(model.family, ensure_ascii=False)}")
        lines.append(f"    path: {json.dumps(model.path, ensure_ascii=False)}")
        lines.append(f"    quant: {model.quant}")
        lines.append(f"    vram_gib: {model.vram_gib:g}")
        lines.append(f"    context: {model.context}")
        lines.append(f"    roles: [{', '.join(model.roles)}]")
    lines.append("roles:")
    lines.extend(f"  {role}: {model_id}" for role, model_id in registry.roles.items())
    lines.append("voters:")
    lines.extend(f"  - {model_id}" for model_id in registry.voters)
    return "\n".join(lines) + "\n"
