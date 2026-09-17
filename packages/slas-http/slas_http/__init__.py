"""The HTTP surface every service shares (ADR-0015, docs/api-contract-round-2.md)."""

from slas_http.client import ServiceClient, ServiceUnreachableError
from slas_http.errors import ServiceError, ThreePartProblem, problem_response
from slas_http.identity import Identity

__all__ = [
    "Identity",
    "ServiceClient",
    "ServiceError",
    "ServiceUnreachableError",
    "ThreePartProblem",
    "problem_response",
]
