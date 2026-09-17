"""The gateway's routes (docs/api-contract-round-2.md §2).

| Route | Answer |
|---|---|
| `POST /v1/complete` | `CompletionResponse` |
| `POST /v1/generate` | `{"object", "tier", "response"}` |
| `POST /v1/cross-check` | `ConsensusVerdict` |
| `GET /v1/routes` · `PUT /v1/instances` | `{"roles", "voters", "instances"}` |
| `GET /v1/status` | `{"sentence", "roles", "budget"}` for the Models page |

Every failure is a three-part `ServiceError`. Before the model manager has announced an
instance for a role, a completion is a 503 that says so; nothing here ever blocks on a model
that is not there. No capability is checked: the contract lists none for the gateway, and
the api never reaches it directly (the orchestrator does, on the person's behalf).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Final

from fastapi import APIRouter
from pydantic import Field

from slas_http.errors import ServiceError
from slas_llm_gateway.consensus import UnknownDecisionError
from slas_llm_gateway.gateway import Gateway
from slas_llm_gateway.instances import InstanceAnnouncement, InstanceTable
from slas_llm_gateway.routing import NoInstanceForRoleError, RoleRouter, Routes
from slas_llm_gateway.structured import SchemaViolationError, VoterUnavailableError
from slas_llm_gateway.vllm import Message
from slas_observability.events import EventLog
from slas_schemas.common import SlasModel
from slas_schemas.errors import ThreePartMessage

#: Answers over the wire may be this long at most; longer plans are split by the caller.
MAX_TOKENS_CEILING: Final = 32_768


@dataclass
class GatewayService:
    gateway: Gateway
    instances: InstanceTable
    log: EventLog


# --- wire shapes ---------------------------------------------------------------------------


class CompleteRequest(SlasModel):
    role: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=MAX_TOKENS_CEILING)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    guided_json: dict[str, Any] | None = None


class GenerateRequest(SlasModel):
    role: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    schema_: dict[str, Any] = Field(alias="schema")
    max_tokens: int = Field(default=1024, ge=1, le=MAX_TOKENS_CEILING)
    #: Tier-1 retries (CLAUDE.md §11); an addition to the contract's body, optional.
    max_retries: int = Field(default=2, ge=0, le=5)


class CrossCheckRequest(SlasModel):
    decision: str = Field(min_length=1)
    evidence: list[Message] = Field(min_length=1)


class InstancesRequest(SlasModel):
    """`PUT /v1/instances` from the model manager. `routes` absent keeps the current routes."""

    instances: list[InstanceAnnouncement]
    routes: Routes | None = None


# --- errors --------------------------------------------------------------------------------


def no_instance_yet(role: str, instance: str) -> ThreePartMessage:
    return ThreePartMessage(
        f"No instance serves the role {role} yet.",
        f"The model manager is still starting {instance}, or has not reported it healthy.",
        "Watch the Models page; the role becomes available when its instance is healthy.",
    )


@contextmanager
def translated() -> Iterator[None]:
    """The gateway's own exceptions, each with a `.message`, as three-part answers."""
    try:
        yield
    except NoInstanceForRoleError as exc:
        raise ServiceError(503, exc.message) from exc
    except VoterUnavailableError as exc:
        raise ServiceError(503, exc.message) from exc
    except SchemaViolationError as exc:
        raise ServiceError(422, exc.message) from exc
    except UnknownDecisionError as exc:
        raise ServiceError(400, exc.message) from exc


# --- views ---------------------------------------------------------------------------------


def routes_view(service: GatewayService) -> dict[str, Any]:
    routes = service.gateway.router.routes
    return {
        "roles": dict(sorted(routes.roles.items())),
        "voters": list(routes.voters),
        "instances": service.instances.view(),
    }


def status_view(service: GatewayService) -> dict[str, Any]:
    routes = service.gateway.router.routes
    budget = service.gateway.budget
    roles: list[dict[str, Any]] = []
    for role, instance in sorted(routes.roles.items()):
        record = service.instances.get(instance)
        roles.append(
            {
                "role": role,
                "instance": instance,
                "model_id": record.model_id if record is not None else None,
                "healthy": record is not None and record.healthy,
            }
        )
    healthy_roles = sum(1 for row in roles if row["healthy"])
    ready_voters = sum(1 for voter in routes.voters if service.instances.is_healthy(voter))
    if service.instances.empty():
        sentence = "No model instance has reported yet; the model manager is still starting them."
    else:
        sentence = (
            f"{healthy_roles} of {len(roles)} roles have a healthy instance; "
            f"{ready_voters} of {len(routes.voters)} voters are ready. {budget.sentence()}"
        )
    return {
        "sentence": sentence,
        "roles": roles,
        "budget": {
            "used_pct": round(budget.spent_today() / budget.daily_tokens * 100, 3),
            "limit_pct": budget.pct,
        },
    }


# --- the router ----------------------------------------------------------------------------


def build_router(service: GatewayService) -> APIRouter:
    router = APIRouter(prefix="/v1")
    gateway = service.gateway
    instances = service.instances

    def served_instance(role: str) -> str:
        """The instance behind `role`, or a 503 that says the model manager is not done."""
        with translated():
            instance = gateway.router.instance_for(role)
        if not instances.is_healthy(instance):
            raise ServiceError(503, no_instance_yet(role, instance))
        return instance

    @router.post("/complete")
    def complete(body: CompleteRequest) -> dict[str, Any]:
        served_instance(body.role)
        with translated():
            response = gateway.complete(
                body.role,
                body.messages,
                max_tokens=body.max_tokens,
                temperature=body.temperature,
                guided_json=body.guided_json,
            )
        return response.model_dump()

    @router.post("/generate")
    def generate(body: GenerateRequest) -> dict[str, Any]:
        served_instance(body.role)
        with translated():
            result = gateway.generate_json(
                body.role,
                body.messages,
                body.schema_,
                max_tokens=body.max_tokens,
                max_retries=body.max_retries,
            )
        return {
            "object": result.value,
            "tier": result.tier,
            "response": result.response.model_dump(),
        }

    @router.post("/cross-check")
    def cross_check(body: CrossCheckRequest) -> dict[str, Any]:
        with translated():
            verdict = gateway.cross_check(body.decision, body.evidence)
        return verdict.model_dump()

    @router.get("/routes")
    def routes() -> dict[str, Any]:
        return routes_view(service)

    @router.put("/instances")
    def put_instances(body: InstancesRequest) -> dict[str, Any]:
        newly_healthy = instances.replace(body.instances)
        if body.routes is not None:
            gateway.router = RoleRouter(body.routes)
        # An instance the model manager now reports healthy starts with a closed breaker:
        # the failures it collected while absent were not invalid answers.
        for name in newly_healthy:
            gateway.breaker.record_success(name)
        current = gateway.router.routes
        service.log.info(
            "instances.replaced",
            instances=instances.names(),
            healthy=[name for name in instances.names() if instances.is_healthy(name)],
            roles=dict(sorted(current.roles.items())),
            voters=list(current.voters),
        )
        return routes_view(service)

    @router.get("/status")
    def status() -> dict[str, Any]:
        return status_view(service)

    return router
