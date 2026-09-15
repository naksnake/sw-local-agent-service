"""The deterministic fingerprint that deduplicates findings (CLAUDE.md §5.4, §10.2).

Numbers, hex addresses and PCI addresses are masked before hashing, so "PCIe link width
changed on GPU3 (0000:8a:00.0): x16 → x8 during DC cycle 14" and the same failure on the
next cycle, or on another slot, share one fingerprint. Code, never a model, decides dedup.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: PCI addresses, hex addresses and every run of digits, including ones glued to words (GPU3).
NOISE: Final = re.compile(
    r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}(?:\.[0-7])?\b|0x[0-9a-fA-F]+|\d+"
)


def normalise(text: str) -> str:
    return NOISE.sub("#", text).strip().lower()


def fingerprint(text: str) -> str:
    """A stable 16-hex fingerprint of a failure text with numbers and addresses masked."""
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()[:16]
