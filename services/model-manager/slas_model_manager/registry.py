"""`Models/models.yaml`: one registry of models, role assignments and voters (CLAUDE.md §7)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final, Literal

import yaml
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


# --- The registries install.sh ships ------------------------------------------------------
#
# `config/models.<profile>.yaml` is rendered from these and copied to Models/models.yaml by
# install.sh when the data root has none yet. Paths match config/model-sources.txt, so the
# weights scripts/fetch_models.py fetches are the ones these entries name. Sizes come from
# the hub's file listings on 2026-09-16; vram_gib adds headroom for the KV cache at the
# stated context and is an assumption until real instances run (same note as fit.py).
# `quant` stays within fp8 | awq4 | bf16 until an ADR admits fp4.
#
# The model manager starts one vLLM instance per role and one per voter (reconcile.py), so a
# model that is both a role and a voter runs twice; the layouts below count that. `slas model
# fit` compares vram_gib with one GPU and has no tensor-parallel field yet, so the DeepSeek-V4
# Pro entry reads as not fitting until the registry gains one (CLAUDE.md §15, decision 14).
# TODO(SLAS-MODELS): add a gpus/tensor_parallel field with that ADR.

_DEEPSEEK_V4_FLASH: Final[dict[str, object]] = {
    "id": "deepseek-v4-flash",
    "display_name": "DeepSeek-V4 Flash",
    "family": "DeepSeek",
    "path": "deepseek-v4-flash",
    "quant": "fp8",
    "vram_gib": 180.0,  # 149 GiB of FP8 weights on the hub
    "context": 131072,
    "roles": ["triage", "planner"],
}
_QWEN38_27B_FP8: Final[dict[str, object]] = {
    "id": "qwen3.8-27b-fp8",
    "display_name": "Qwen3.8-27B",
    "family": "Qwen",
    "path": "qwen3.8-27b-fp8",
    "quant": "fp8",
    "vram_gib": 40.0,  # 29 GiB of FP8 weights; a vision-language checkpoint served text-only
    "context": 131072,
    "roles": ["coder", "planner"],
}
_BGE_M3: Final[dict[str, object]] = {
    "id": "bge-m3",
    "display_name": "BGE-M3",
    "family": "BAAI",
    "path": "bge-m3",
    "quant": "bf16",
    "vram_gib": 3.0,
    "context": 8192,
    "roles": ["embed"],
}
_BGE_RERANKER_V2_M3: Final[dict[str, object]] = {
    "id": "bge-reranker-v2-m3",
    "display_name": "BGE Reranker v2 M3",
    "family": "BAAI",
    "path": "bge-reranker-v2-m3",
    "quant": "bf16",
    "vram_gib": 2.0,
    "context": 8192,
    "roles": ["rerank"],
}
_DEEPSEEK_V4_PRO: Final[dict[str, object]] = {
    "id": "deepseek-v4-pro",
    "display_name": "DeepSeek-V4 Pro",
    "family": "DeepSeek",
    "path": "deepseek-v4-pro",
    "quant": "fp8",
    "vram_gib": 900.0,  # 805 GiB of FP8 weights on the hub; tensor parallel over four GPUs
    "context": 131072,
    "roles": ["planner"],
}
_MINIMAX_M2_7: Final[dict[str, object]] = {
    "id": "minimax-m2.7",
    "display_name": "MiniMax-M2.7",
    "family": "MiniMax",
    "path": "minimax-m2.7",
    "quant": "fp8",
    "vram_gib": 235.0,  # 214 GiB of FP8 weights on the hub; the third voter family
    "context": 131072,
    "roles": ["planner"],
}
_QWEN38_27B_BF16: Final[dict[str, object]] = {
    "id": "qwen3.8-27b-bf16",
    "display_name": "Qwen3.8-27B (BF16 reference)",
    "family": "Qwen",
    "path": "qwen3.8-27b-bf16",
    "quant": "bf16",
    "vram_gib": 64.0,  # 52 GiB of BF16 weights on the hub
    "context": 131072,
    "roles": [],  # eval regression only (CLAUDE.md §7); the gateway never routes to it
}

#: Quickstart (CLAUDE.md §3): the planner role is a second instance of the small coder, so
#: the instances are Flash ×2 (triage, voter), Qwen ×3 (coder, planner, voter) and the two
#: BGE models: about 490 GiB over three GPUs of the B300 class. Two voters from two families,
#: so every cross-check is reported as a weaker check (CLAUDE.md §15, decision 12).
QUICKSTART_REGISTRY: Final[dict[str, object]] = {
    "version": 1,
    "models": [_DEEPSEEK_V4_FLASH, _QWEN38_27B_FP8, _BGE_M3, _BGE_RERANKER_V2_M3],
    "roles": {
        "coder": "qwen3.8-27b-fp8",
        "planner": "qwen3.8-27b-fp8",
        "triage": "deepseek-v4-flash",
        "embed": "bge-m3",
        "rerank": "bge-reranker-v2-m3",
    },
    "voters": ["deepseek-v4-flash", "qwen3.8-27b-fp8"],
}

#: Prod (docs/runbooks/deploy-hgx-b300.md §4): DeepSeek-V4 Pro as planner on GPUs 0-3, Flash
#: as triage and as a voter (two instances), Qwen as coder and voter, MiniMax-M2.7 as the third
#: voter family, the BF16 reference for eval regression. Eight GPUs, one instance each.
PROD_REGISTRY: Final[dict[str, object]] = {
    "version": 1,
    "models": [
        _DEEPSEEK_V4_PRO,
        _DEEPSEEK_V4_FLASH,
        _QWEN38_27B_FP8,
        _MINIMAX_M2_7,
        _QWEN38_27B_BF16,
        _BGE_M3,
        _BGE_RERANKER_V2_M3,
    ],
    "roles": {
        "coder": "qwen3.8-27b-fp8",
        "planner": "deepseek-v4-pro",
        "triage": "deepseek-v4-flash",
        "embed": "bge-m3",
        "rerank": "bge-reranker-v2-m3",
    },
    "voters": ["deepseek-v4-flash", "qwen3.8-27b-fp8", "minimax-m2.7"],
}

PROFILE_REGISTRIES: Final[dict[str, dict[str, object]]] = {
    "quickstart": QUICKSTART_REGISTRY,
    "prod": PROD_REGISTRY,
}


def profile_registry_header(profile: str) -> str:
    voters = (
        "Two voters from two families: every cross-check is reported as a weaker check until a\n"
        "third family is added (CLAUDE.md §15, decision 12; models.prod.yaml adds MiniMax-M2.7)."
        if profile == "quickstart"
        else "Three voters from three families (DeepSeek, Qwen, MiniMax); DeepSeek-V4 Pro serves\n"
        "the planner only, tensor parallel over four GPUs."
    )
    return (
        f"Models/models.yaml for the {profile} profile of SW Local Agent Service (CLAUDE.md §7).\n"
        f"Rendered from slas_model_manager.registry.PROFILE_REGISTRIES[{profile!r}]; a unit test\n"
        "keeps file and code in step. install.sh copies this file to ${SLAS_DATA_ROOT}/Models/\n"
        "models.yaml when none exists there yet, and never overwrites one (INV-9).\n"
        "Every `path` is a directory under Models/ that scripts/fetch_models.py fetches from the\n"
        "matching line of config/model-sources.txt; never a URL (INV-1).\n"
        "Assumes GPUs of about 288 GB each (HGX B300 class); on smaller GPUs pick smaller or\n"
        "AWQ builds on the Models page. The model manager starts one instance per role and one\n"
        "per voter. vram_gib is the weights on the hub plus headroom for the KV cache at\n"
        "`context`; an assumption until real instances run. Change roles on the Models page or\n"
        f"here; no restart is needed.\n{voters}"
    )


def profile_registry(profile: str) -> Registry:
    return registry_from_mapping(PROFILE_REGISTRIES[profile], source=f"models.{profile}.yaml")


def read_registry_file(path: Path) -> tuple[dict[str, Any], str]:
    """The registry file as a mapping plus its leading comment lines (the header a rewrite
    keeps), or a three-part `RegistryError` when the file is missing, not YAML or invalid.
    The mapping is validated, so a caller may edit and re-validate it."""
    source = str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(
            ThreePartMessage(
                f"The model registry {source} could not be read.",
                f"The file is missing or unreadable ({type(exc).__name__}).",
                "Run `./install.sh` again, which writes the profile's registry when none "
                "exists, or create it from services/model-manager/models.example.yaml.",
            )
        ) from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(
            ThreePartMessage(
                f"The model registry {source} could not be used.",
                f"It is not valid YAML ({str(exc).splitlines()[0][:120]}).",
                f"Fix {source} on the Models page or by hand.",
            )
        ) from exc
    registry_from_mapping(data, source=source)
    header_lines: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        header_lines.append(line[1:].strip())
    if not isinstance(data, dict):  # pragma: no cover — validated above
        raise RegistryError(
            ThreePartMessage(
                f"The model registry {source} could not be used.",
                "Its top level is not a mapping.",
                f"Fix {source} on the Models page or by hand.",
            )
        )
    return dict(data), "\n".join(header_lines)


def write_registry_file(path: Path, data: Mapping[str, object], *, header: str = "") -> Registry:
    """Validate `data`, render it and replace `path` atomically (a reader never sees a half
    file, INV-9: the model manager picks the new file up on its next tick)."""
    registry = registry_from_mapping(data, source=str(path))
    rendered = render_registry_yaml(data, header=header)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(path)
    return registry


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
    # An empty mapping or list is written as such: a bare `roles:` reads back as null.
    lines.append("roles:" if registry.roles else "roles: {}")
    lines.extend(f"  {role}: {model_id}" for role, model_id in registry.roles.items())
    lines.append("voters:" if registry.voters else "voters: []")
    lines.extend(f"  - {model_id}" for model_id in registry.voters)
    return "\n".join(lines) + "\n"
