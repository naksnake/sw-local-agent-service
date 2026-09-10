"""Pydantic schemas shared at every boundary (CLAUDE.md §5.4, §11).

P0 ships the three-part error; Job, Plan, Step, Ticket, Vote, SopModel and Finding arrive in P2.
"""

from slas_schemas.errors import ThreePartError

__all__ = ["ThreePartError"]
