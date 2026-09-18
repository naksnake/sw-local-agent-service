"""Append one fetched model to `Models/models.yaml` (ADR-0018).

Read → validate with the model manager's loader → add the entry → write atomically. An id
that is already there is never overwritten, and `roles:` and `voters:` are never touched:
the person assigns a role on the Models page, where the fit sentence is. The model manager
re-reads the file on its next reconcile tick (INV-9: no restart).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from slas_model_manager.registry import (
    Registry,
    RegistryError,
    read_registry_file,
    write_registry_file,
)
from slas_schemas.errors import ThreePartMessage

EMPTY_HEADER = (
    "Models/models.yaml for SW Local Agent Service (CLAUDE.md §7), written by the model\n"
    "fetcher (ADR-0018). Every `path` is a directory under Models/, never a URL (INV-1).\n"
    "Change roles and voters on the Models page; no restart is needed."
)


def registered_ids(models_file: Path) -> list[str]:
    """The ids in the registry, or none when the file does not exist yet; a broken file is a
    three-part `RegistryError` (fix it before adding to it)."""
    if not models_file.is_file():
        return []
    data, _ = read_registry_file(models_file)
    return [str(m.get("id", "")) for m in _models_of(data)]


def _models_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    models = data.get("models")
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


def import_model(models_file: Path, entry: dict[str, object]) -> Registry:
    """Add `entry` to the registry file and return the registry as written."""
    if models_file.is_file():
        data, header = read_registry_file(models_file)
    else:
        data, header = {"version": 1, "models": [], "roles": {}, "voters": []}, EMPTY_HEADER
    models = _models_of(data)
    if any(m.get("id") == entry["id"] for m in models):
        raise RegistryError(
            ThreePartMessage(
                f"{entry['id']} is already in the model registry.",
                "A model with this id was registered earlier.",
                "Give the new model another registry id, or remove the old entry from "
                "Models/models.yaml first.",
            )
        )
    data["models"] = [*models, dict(entry)]
    return write_registry_file(models_file, data, header=header)


def ids_in_use(models_file: Path, fetching: Sequence[str]) -> list[str]:
    """Registered ids plus the ids of fetches still running: what a new id must differ from."""
    return [*registered_ids(models_file), *fetching]
