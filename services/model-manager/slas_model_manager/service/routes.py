"""The model manager's routes, exactly the table of docs/api-contract-round-2.md §3."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import Field

from slas_http.identity import identity_of, require
from slas_model_manager.controller import MODEL_MANAGE, Controller
from slas_schemas.common import SlasModel


class SwapBody(SlasModel):
    role: str = Field(min_length=1)
    candidate: str = Field(min_length=1)


class RollbackBody(SlasModel):
    role: str = Field(min_length=1)


def build_router(controller: Controller) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/status")
    def status() -> dict[str, Any]:
        return controller.status()

    @router.post("/reconcile")
    def reconcile(body: dict[str, Any] | None = None) -> dict[str, Any]:
        report = controller.reconcile()
        return report.model_dump()

    @router.post("/swap")
    def swap(request: Request, body: SwapBody) -> dict[str, Any]:
        identity = identity_of(request)
        require(identity, MODEL_MANAGE, verb="swap a model")
        return controller.begin_swap(body.role, body.candidate).model_dump(mode="json")

    @router.post("/rollback")
    def rollback(request: Request, body: RollbackBody) -> dict[str, Any]:
        identity = identity_of(request)
        require(identity, MODEL_MANAGE, verb="roll a model swap back")
        return controller.begin_rollback(body.role).model_dump(mode="json")

    @router.get("/fit")
    def fit(model: str = Query(min_length=1)) -> dict[str, Any]:
        return controller.fit(model)

    return router
