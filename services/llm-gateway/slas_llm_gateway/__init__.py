"""LLM gateway: role routing, redaction, guided_json enforcement, circuit breaker, consensus.

The only component that talks to vLLM (CLAUDE.md §11); hosts the Consensus Router (§5.3).
"""

__version__ = "0.0.1"
