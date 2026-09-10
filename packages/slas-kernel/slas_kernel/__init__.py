"""Agent Kernel: the lifecycle every agent runs (CLAUDE.md §5.1).

P0 ships only the branding constants; the lifecycle, journal and ticket code arrive in P2.
"""

from slas_kernel.branding import PRODUCT_NAME, SLUG

__all__ = ["PRODUCT_NAME", "SLUG"]
