"""Shared schemas: Job, Plan, Step, Ticket, Vote, SopModel, Finding and three-part errors.

Phase 0 ships only `errors`. The remaining models arrive in Phase 2 (CLAUDE.md §5.4, §11).
"""

from slas_schemas.errors import ThreePartMessage

__version__ = "0.0.1"

__all__ = ["ThreePartMessage", "__version__"]
