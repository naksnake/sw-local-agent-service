"""Reconcile the registry (desired) against what is running (actual) into plain actions.

Pure: takes the registry and the running containers, returns start/stop/keep actions with a
reason each. The manager applies them through `ContainerRuntime`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from slas_model_manager.registry import Registry, instance_name, voter_instance_name
from slas_model_manager.runtime import ContainerRef
from slas_schemas.common import SlasModel


class Action(SlasModel):
    kind: Literal["start", "stop", "keep"]
    name: str = Field(min_length=1)
    model_id: str | None = None
    reason: str = Field(min_length=1)


def desired_instances(registry: Registry) -> dict[str, str]:
    """instance name → model id for every role and every voter in the registry."""
    desired = {instance_name(role): model_id for role, model_id in registry.roles.items()}
    for model_id in registry.voters:
        desired[voter_instance_name(model_id)] = model_id
    return desired


def plan_reconcile(
    registry: Registry,
    running: list[ContainerRef],
    *,
    outdated: Mapping[str, str] | None = None,
) -> list[Action]:
    """`outdated` names containers that serve the right model but were created with other
    flags or another image than the manager would use now (a container's command is fixed
    at creation): each is stopped and started again, with the given reason."""
    desired = desired_instances(registry)
    actual = {ref.name: ref for ref in running if ref.name.startswith("vllm-")}
    stale = dict(outdated or {})
    actions: list[Action] = []
    for name, model_id in desired.items():
        ref = actual.get(name)
        if ref is None:
            actions.append(
                Action(kind="start", name=name, model_id=model_id, reason="not running yet")
            )
        elif ref.spec.model_id == model_id and name in stale:
            actions.append(Action(kind="stop", name=name, model_id=model_id, reason=stale[name]))
            actions.append(
                Action(
                    kind="start",
                    name=name,
                    model_id=model_id,
                    reason="started again as it should be",
                )
            )
        elif ref.spec.model_id != model_id:
            actions.append(
                Action(
                    kind="stop",
                    name=name,
                    model_id=ref.spec.model_id,
                    reason=f"serves {ref.spec.model_id} but the registry says {model_id}",
                )
            )
            actions.append(
                Action(kind="start", name=name, model_id=model_id, reason="registry changed")
            )
        else:
            actions.append(Action(kind="keep", name=name, model_id=model_id, reason="matches"))
    for name, ref in sorted(actual.items()):
        if name not in desired and "-swap-" not in name and not _is_swap_candidate(name, desired):
            actions.append(
                Action(
                    kind="stop",
                    name=name,
                    model_id=ref.spec.model_id,
                    reason="not in the registry",
                )
            )
    return actions


def _is_swap_candidate(name: str, desired: dict[str, str]) -> bool:
    """`vllm-<role>-<model>` containers belong to an in-flight swap; leave them alone."""
    return any(name.startswith(f"{base}-") for base in desired)


def sentence(actions: list[Action]) -> str:
    starts = [a.name for a in actions if a.kind == "start"]
    stops = [a.name for a in actions if a.kind == "stop"]
    if not starts and not stops:
        return "Every model instance matches the registry; nothing to do."
    parts: list[str] = []
    if starts:
        parts.append(f"start {', '.join(starts)}")
    if stops:
        parts.append(f"stop {', '.join(stops)}")
    return "To match the registry: " + "; ".join(parts) + "."
