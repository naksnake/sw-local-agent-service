"""`GET /api/v1/models`: the registry file read on every request (CLAUDE.md §7, INV-9).

`Models/models.yaml` is parsed and validated with the model manager's own
`registry_from_mapping`; `present` is whether `Models/<path>/SHA256SUMS` exists, which is
what `install.sh --fetch-models` writes after verifying the weights. A missing or invalid
file is not an error status: the page still renders, with `problem` filled in.
"""

from __future__ import annotations

from typing import Any

import yaml

from slas_api.errors import ThreePartProblem
from slas_api.settings import Settings
from slas_model_manager.registry import RegistryError, registry_from_mapping
from slas_schemas.errors import ThreePartMessage


def _empty(problem: ThreePartMessage) -> dict[str, Any]:
    return {
        "sentence": problem.what_happened,
        "models": [],
        "roles": {},
        "voters": [],
        "problem": ThreePartProblem.from_message(problem).body(),
    }


def models_view(settings: Settings) -> dict[str, Any]:
    path = settings.models_file
    shown = "Models/models.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty(
            ThreePartMessage(
                f"The model registry {shown} is missing.",
                "install.sh has not copied it into the data root yet, or it was removed.",
                f"Run `slas model scan` on the host, or copy config/models.{settings.slas_profile}"
                f".yaml to ${{SLAS_DATA_ROOT}}/{shown}.",
            )
        )
    except OSError as exc:
        return _empty(
            ThreePartMessage(
                f"The model registry {shown} could not be read.",
                f"{type(exc).__name__}: {exc}".splitlines()[0],
                "Check the file's permissions on the host; the api runs as the data root's owner.",
            )
        )
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return _empty(
            ThreePartMessage(
                f"The model registry {shown} is not valid YAML.",
                str(exc).splitlines()[0] if str(exc) else "A syntax error in the file.",
                f"Fix {shown} on the Models page or by hand.",
            )
        )
    try:
        registry = registry_from_mapping(data, source=shown)
    except RegistryError as exc:
        return _empty(exc.message)
    models = [
        {
            "id": model.id,
            "display_name": model.display_name,
            "family": model.family,
            "path": model.path,
            "quant": model.quant,
            "vram_gib": model.vram_gib,
            "context": model.context,
            "roles": list(model.roles),
            "present": (settings.models_dir / model.path / "SHA256SUMS").is_file(),
        }
        for model in registry.models
    ]
    return {
        "sentence": registry.sentence(),
        "models": models,
        "roles": dict(registry.roles),
        "voters": list(registry.voters),
        "problem": None,
    }
