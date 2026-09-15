"""Shared schemas: Job, Plan, Step, Ticket, Vote, SopModel, Finding and three-part errors.

Modules and what they need:

- stdlib only, imported here: `errors` (ThreePartMessage), `envfile` (lossless .env writer).
  The host CLI (`install.sh` → `slas doctor`) imports only these, so it runs on a bare host.
- Pydantic v2 (CLAUDE.md §4.3), imported on demand by their users: `common`, `ids`, `job`,
  `plan`, `ticket`, `vote`, `finding`, `sop`, `journal`.
"""

from slas_schemas.errors import ThreePartMessage

__version__ = "0.0.1"

__all__ = ["ThreePartMessage", "__version__"]
