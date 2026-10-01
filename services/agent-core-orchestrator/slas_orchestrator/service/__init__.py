"""The orchestrator's HTTP surface (docs/api-contract-round-2.md §5, ADR-0015).

`create_app()` in `app.py` builds the FastAPI app with every collaborator injectable; the
routers live one per page: `coding.py`, `skills.py`, `tickets.py` (this slice) and
`validation.py`, `factory.py` (the executor slice).
"""
