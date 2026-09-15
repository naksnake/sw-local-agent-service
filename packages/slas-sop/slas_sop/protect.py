"""Identifiers, commands, numbers, paths, versions and BDFs are copied by code (INV-13).

Before any prose reaches a translation model, every such token is replaced by a numbered
placeholder ⟦n⟧; after translation the placeholders are swapped back. A translation that
drops, duplicates or invents a placeholder is rejected and the English is kept. The model
therefore never has the chance to translate `sop.en.md`, `0000:8a:00.0` or `v1.2.3`.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_sop.glossary import Glossary, default_glossary

PLACEHOLDER: Final = re.compile(r"⟦(\d+)⟧")

#: Ordered: at any position the first shape that matches wins, so a BDF is not eaten by the
#: number rule and a version is not split into two numbers. Compiled into one alternation.
PROTECTED_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("code", r"`[^`\n]+`"),
    ("ticket", r"\bT-[a-z]+-\d{4,}\b"),
    ("bdf", r"\b[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]\b"),
    ("tag", r"\[(?:Issue|Owner)\]"),
    ("path", r"(?<![\w/])(?:/|\./|~/)[\w.\-]+(?:/[\w.\-]+)*/?"),
    (
        "filename",
        r"\b[\w-]+(?:\.[\w-]+)*\.(?:md|json|yaml|yml|log|jsonl|py|sh|pdf|zip|bundle|xlsx)\b",
    ),
    ("version", r"\bv?\d+\.\d+(?:\.\d+)*(?:[-+][\w.]+)?\b"),
    ("hex", r"\b0x[0-9a-fA-F]+\b"),
    ("snake", r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"),
    ("acronym_digit", r"\b[A-Z][A-Za-z]*\d[A-Za-z0-9]*\b"),
    ("acronym", r"\b[A-Z]{2,}[a-z]{0,2}\b"),
    ("camel", r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+\b"),
    ("lane", r"\bx\d+\b"),
    (
        "number",
        r"(?<![\w.])[-+]?\d+(?:[.,]\d+)*(?:\s?%|\s?(?:s|ms|GB|MB|KB|W|V|A|°C|Hz|MHz|GHz))?"
        r"(?!\w|\.\d)",
    ),
)
_PROTECTED: Final = re.compile(
    "|".join(f"(?P<{name}>{pattern})" for name, pattern in PROTECTED_PATTERNS)
)


class Protected(SlasModel):
    text: str
    slots: dict[str, str] = Field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.slots)


class ProtectionError(ValueError):
    """The translated text does not carry exactly the placeholders it was given."""


def identifiers(text: str, glossary: Glossary | None = None) -> list[str]:
    """Every protected token in reading order — what must be identical in both renderings."""
    return list(protect(text, glossary=glossary).slots.values())


def _glossary_spans(text: str, glossary: Glossary) -> list[tuple[int, int]]:
    """Where glossary terms sit, in English or in their Chinese rendering. An acronym inside
    one — the DC of "DC cycle", the SOP of "SOP" — stays visible so the translator can apply
    the pinned rendering, and the check afterwards can see that it did."""
    spans: list[tuple[int, int]] = []
    for term in glossary.terms:
        spans.extend(match.span() for match in term.pattern.finditer(text))
        start = text.find(term.zh_hant)
        while start != -1:
            spans.append((start, start + len(term.zh_hant)))
            start = text.find(term.zh_hant, start + 1)
    return spans


def protect(text: str, *, glossary: Glossary | None = None) -> Protected:
    """One left-to-right pass: the translator sees ⟦1⟧ … ⟦n⟧ in reading order."""
    glossary = glossary or default_glossary()
    spans = _glossary_spans(text, glossary)
    slots: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        start, end = match.span()
        if any(a <= start and end <= b for a, b in spans):
            return match.group(0)
        key = str(len(slots) + 1)
        slots[key] = match.group(0)
        return f"⟦{key}⟧"

    return Protected(text=_PROTECTED.sub(replace, text), slots=slots)


def restore(translated: str, protected: Protected) -> str:
    """Put the originals back; raise if the placeholders do not match one for one."""
    found = PLACEHOLDER.findall(translated)
    expected = list(protected.slots)
    if sorted(found, key=int) != sorted(expected, key=int):
        missing = sorted(set(expected) - set(found), key=int)
        extra = sorted(set(found) - set(expected), key=int)
        duplicated = sorted({k for k in found if found.count(k) > 1}, key=int)
        problems: list[str] = []
        if missing:
            problems.append(f"missing {', '.join(f'⟦{k}⟧' for k in missing)}")
        if extra:
            problems.append(f"invented {', '.join(f'⟦{k}⟧' for k in extra)}")
        if duplicated:
            problems.append(f"duplicated {', '.join(f'⟦{k}⟧' for k in duplicated)}")
        raise ProtectionError("; ".join(problems))
    return PLACEHOLDER.sub(lambda m: protected.slots[m.group(1)], translated)
