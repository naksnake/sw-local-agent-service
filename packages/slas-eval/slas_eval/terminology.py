"""Terminology consistency check (CLAUDE.md §8.1 gate: 100%).

For every glossary term the English text uses, the Chinese text must use the pinned
rendering and none of the renderings the glossary says to avoid. Independently, the list
of protected identifiers (ticket ids, BDFs, paths, versions, numbers…) must be identical in
both texts — INV-13's "copied by code" made checkable.
"""

from __future__ import annotations

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.sop import SopModel
from slas_sop.glossary import Glossary, default_glossary
from slas_sop.protect import identifiers, protect


class TerminologyReport(SlasModel):
    #: Glossary terms present in the English text.
    checked: list[str] = Field(default_factory=list)
    #: `en → zh` pairs the Chinese text does not use.
    missing: list[str] = Field(default_factory=list)
    #: Renderings the glossary says to avoid that the Chinese text uses.
    avoided: list[str] = Field(default_factory=list)
    identifiers_en: list[str] = Field(default_factory=list)
    identifiers_zh: list[str] = Field(default_factory=list)
    #: Fields whose Chinese text is the English text unchanged (kept in English on purpose).
    untranslated: list[str] = Field(default_factory=list)

    @property
    def identifiers_match(self) -> bool:
        """Same identifiers, same counts; order may differ because word order does."""
        return sorted(self.identifiers_en) == sorted(self.identifiers_zh)

    @property
    def consistent(self) -> bool:
        return not self.missing and not self.avoided and self.identifiers_match

    @property
    def score(self) -> float:
        if not self.checked:
            return 1.0 if self.identifiers_match else 0.0
        good = len(self.checked) - len({m.split(" → ")[0] for m in self.missing})
        return max(0.0, good / len(self.checked)) if self.identifiers_match else 0.0

    def sentence(self) -> str:
        parts: list[str] = []
        count = len(self.checked)
        if not count:
            parts.append("No glossary term occurs in the English text.")
        elif self.missing or self.avoided:
            parts.append(
                f"{count - len(self.missing)} of {count} glossary terms are rendered as pinned."
            )
            if self.missing:
                parts.append("Not as pinned: " + "; ".join(self.missing) + ".")
            if self.avoided:
                parts.append("Uses renderings to avoid: " + ", ".join(self.avoided) + ".")
        else:
            parts.append(f"All {count} glossary terms are rendered as pinned.")
        if self.identifiers_match:
            parts.append(f"{len(self.identifiers_en)} identifiers are identical in both texts.")
        else:
            only_en = sorted(set(self.identifiers_en) - set(self.identifiers_zh))
            only_zh = sorted(set(self.identifiers_zh) - set(self.identifiers_en))
            detail = []
            if only_en:
                detail.append("only in English: " + ", ".join(only_en))
            if only_zh:
                detail.append("only in Chinese: " + ", ".join(only_zh))
            parts.append(
                "The identifiers differ between the two texts"
                + (f" ({'; '.join(detail)})." if detail else " in how often they occur.")
            )
        if self.untranslated:
            n = len(self.untranslated)
            parts.append(f"{n} {'field is' if n == 1 else 'fields are'} still in English.")
        return " ".join(parts)


def check_terminology(
    english: str, chinese: str, glossary: Glossary | None = None
) -> TerminologyReport:
    glossary = glossary or default_glossary()
    # Terms are looked for in the prose, not inside identifiers (a path may contain "SOP").
    terms = glossary.terms_in(protect(english, glossary=glossary).text)
    report = TerminologyReport(
        checked=[term.en for term in terms],
        identifiers_en=identifiers(english, glossary),
        identifiers_zh=identifiers(chinese, glossary),
    )
    if chinese.strip() == english.strip():
        report.untranslated = ["text"] if english.strip() and terms else []
    for term in terms:
        if not term.in_chinese(chinese):
            report.missing.append(f"{term.en} → {term.zh_hant}")
        report.avoided.extend(alt for alt in term.avoid if alt in chinese)
    return report


def _prose_fields(model: SopModel) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [("purpose", model.purpose)]
    fields += [(f"prerequisites[{i}]", p) for i, p in enumerate(model.prerequisites, start=1)]
    for step in model.steps:
        fields.append((f"steps[{step.n}].action", step.action))
        fields.append((f"steps[{step.n}].expected", step.expected))
    fields += [(f"checks[{i}]", c) for i, c in enumerate(model.checks, start=1)]
    fields += [(f"findings[{i}]", f) for i, f in enumerate(model.findings, start=1)]
    fields += [(f"next_actions[{i}]", a) for i, a in enumerate(model.next_actions, start=1)]
    return fields


def check_sop_terminology(
    english: SopModel, chinese: SopModel, glossary: Glossary | None = None
) -> TerminologyReport:
    """Field by field over two SopModels; structure must match or the check fails outright."""
    glossary = glossary or default_glossary()
    en_fields = _prose_fields(english)
    zh_fields = _prose_fields(chinese)
    if [name for name, _ in en_fields] != [name for name, _ in zh_fields]:
        raise ValueError("the English and Chinese SOPs do not have the same fields")
    report = TerminologyReport()
    seen: set[str] = set()
    for (name, en), (_, zh) in zip(en_fields, zh_fields, strict=True):
        part = check_terminology(en, zh, glossary)
        for term in part.checked:
            if term not in seen:
                seen.add(term)
                report.checked.append(term)
        report.missing.extend(m for m in part.missing if m not in report.missing)
        report.avoided.extend(a for a in part.avoided if a not in report.avoided)
        if part.untranslated:
            report.untranslated.append(name)
    # Identifiers are compared over the whole document, evidence and results included.
    report.identifiers_en = identifiers(_flatten(english), glossary)
    report.identifiers_zh = identifiers(_flatten(chinese), glossary)
    return report


def _flatten(model: SopModel) -> str:
    return "\n".join(
        [
            model.title,
            *(text for _, text in _prose_fields(model)),
            *(", ".join(step.evidence) for step in model.steps),
            *(f"{k}: {v}" for k, v in model.results.items()),
            *model.glossary_refs,
        ]
    )
