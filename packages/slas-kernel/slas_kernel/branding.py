"""Product naming (CLAUDE.md §0.1).

If the product is renamed, this module is the one constant to change, plus one
find-and-replace in CLAUDE.md.
"""

from typing import Final

PRODUCT_NAME: Final = "SW Local Agent Service"
SLUG: Final = "slas"  # code, paths, metrics, containers; lowercase, always
METRIC_PREFIX: Final = "slas_"
NETWORK_PREFIX: Final = "slas-"
CLI_NAME: Final = "slas"
DEFAULT_DATA_ROOT: Final = "/AI/Agent"
KERNEL_NAME: Final = "Agent Kernel"
AGENT_NAMES: Final = ("Coding Agent", "Validation Agent", "Factory Agent")
