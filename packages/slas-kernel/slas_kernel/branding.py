"""Product naming — the single place the product name lives (CLAUDE.md §0.1).

If the product is renamed, change the constants here and run the one find-replace in
CLAUDE.md. Nothing else in the codebase spells the name out.
"""

from typing import Final

PRODUCT_NAME: Final = "SW Local Agent Service"
SLUG: Final = "slas"
DEFAULT_DATA_ROOT: Final = "/AI/Agent"
AGENT_NAMES: Final[tuple[str, str, str]] = ("coding", "validation", "factory")
