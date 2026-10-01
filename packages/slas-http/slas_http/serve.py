"""`run(app, bind)`: uvicorn on the container's own interface, quiet logs (events are JSON)."""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI

DEFAULT_BIND: Final = "0.0.0.0:8000"  # the container's own address, as compose probes it


def split_bind(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host or "0.0.0.0", int(port or 8000)  # noqa: S104


def run(app: FastAPI, bind: str = DEFAULT_BIND) -> None:  # pragma: no cover — starts a server
    import uvicorn

    host, port = split_bind(bind)
    uvicorn.run(app, host=host, port=port, log_level="warning")
