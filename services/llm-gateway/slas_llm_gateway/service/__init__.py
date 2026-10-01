"""The llm-gateway service: `create_app()`, its settings and routes (contract round 2 §2)."""

from slas_llm_gateway.service.app import create_app, route_table
from slas_llm_gateway.service.settings import Settings

__all__ = ["Settings", "create_app", "route_table"]
