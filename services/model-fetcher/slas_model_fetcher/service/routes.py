"""The model fetcher's routes, exactly the table of docs/api-contract-round-2.md §3b."""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request

from slas_http.identity import identity_of, require
from slas_model_fetcher.fetcher import FetchManager, StartBody

MODEL_MANAGE: Final = "model:manage"


def build_router(manager: FetchManager) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.post("/fetches", status_code=201)
    def start(request: Request, body: StartBody) -> dict[str, Any]:
        identity = identity_of(request)
        require(identity, MODEL_MANAGE, verb="add a model")
        return manager.start(body, by=identity.user).model_dump(mode="json")

    @router.get("/fetches")
    def list_fetches() -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in manager.records()]

    @router.get("/fetches/{fetch_id}")
    def get_fetch(fetch_id: str) -> dict[str, Any]:
        return manager.get(fetch_id).model_dump(mode="json")

    @router.delete("/fetches/{fetch_id}")
    def cancel_or_remove(request: Request, fetch_id: str) -> dict[str, str]:
        identity = identity_of(request)
        require(identity, MODEL_MANAGE, verb="cancel or remove a model fetch")
        return manager.cancel_or_remove(fetch_id)

    return router
