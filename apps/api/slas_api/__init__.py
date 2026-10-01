"""slas_api: the api of SW Local Agent Service (CLAUDE.md §4.2, §11; ADR-0005 to ADR-0008).

Sign-in and sessions, people, runtime settings mirrored to `.env`, the model registry view,
health and metrics — the routes of docs/api-contract.md. No hardware, no LLM. Import the
submodules you need; this package imports nothing so `slas_api.cli` starts quickly.
"""

__version__ = "0.0.1"

__all__ = ["__version__"]
