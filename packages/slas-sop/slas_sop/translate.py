"""Prose translation with the glossary pinned and every identifier protected (§5.5, INV-13).

    English prose ──► protect() ──► Translator (planner role, glossary pinned) ──► restore()
                                                                          └─► glossary check
Any failure — a lost placeholder, a glossary term rendered differently, an empty answer —
keeps the English sentence and records a note. The renderer shows the note; nothing is
silently wrong. `Translator` is a protocol: production wires the gateway's `planner` role,
tests use `FakeTranslator`.
"""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import Field

from slas_schemas.common import SlasModel
from slas_schemas.sop import SopModel, SopStep
from slas_sop.glossary import Glossary, default_glossary
from slas_sop.protect import ProtectionError, protect, restore


class TranslationRequest(SlasModel):
    #: The prose with identifiers replaced by ⟦n⟧ placeholders.
    text: str
    source_lang: str = "en"
    target_lang: str = "zh-Hant"
    #: "Use these translations exactly and no others: …" for the terms in this text.
    glossary_sentence: str
    placeholders: int = Field(ge=0)

    def instruction(self) -> str:
        return (
            f"Translate from {self.source_lang} to {self.target_lang}. Keep every ⟦n⟧ "
            f"placeholder exactly as written ({self.placeholders} in total); they stand for "
            f"identifiers, commands and numbers you must not translate. {self.glossary_sentence}"
        )


class Translator(Protocol):
    def translate(self, request: TranslationRequest) -> str: ...


class TranslationOutcome(SlasModel):
    text: str
    translated: bool
    note: str | None = None


def translate_prose(
    english: str,
    *,
    translator: Translator,
    glossary: Glossary | None = None,
    target_lang: str = "zh-Hant",
) -> TranslationOutcome:
    """Translate one field. On any verification failure the English is kept and explained."""
    glossary = glossary or default_glossary()
    if not english.strip():
        return TranslationOutcome(text=english, translated=False)
    protected = protect(english, glossary=glossary)
    if not re.search(r"[A-Za-z]{2,}", protected.text):
        # Nothing but identifiers and punctuation: there is no prose to translate.
        return TranslationOutcome(text=english, translated=False)
    # Terms are looked for in the prose the translator sees, not inside protected identifiers
    # (the SOP in a path such as /AI/Agent/SOP is a directory name, not the term).
    terms = glossary.terms_in(protected.text)
    request = TranslationRequest(
        text=protected.text,
        target_lang=target_lang,
        glossary_sentence=glossary.pinned_sentence(protected.text),
        placeholders=protected.count,
    )
    try:
        answer = translator.translate(request)
    except Exception as exc:  # a model failure never fails the export
        return TranslationOutcome(
            text=english, translated=False, note=f"kept in English: the translator failed ({exc})"
        )
    if not answer.strip():
        return TranslationOutcome(
            text=english, translated=False, note="kept in English: the translator returned nothing"
        )
    try:
        restored = restore(answer.strip(), protected)
    except ProtectionError as exc:
        return TranslationOutcome(
            text=english,
            translated=False,
            note=f"kept in English: the translation lost an identifier ({exc})",
        )
    wrong = [term for term in terms if not term.in_chinese(restored)]
    avoided = [alt for term in terms for alt in term.avoid if alt in restored]
    if wrong or avoided:
        parts: list[str] = []
        if wrong:
            parts.append("did not use " + ", ".join(f"{t.en} → {t.zh_hant}" for t in wrong))
        if avoided:
            parts.append("used " + ", ".join(avoided))
        return TranslationOutcome(
            text=english,
            translated=False,
            note=f"kept in English: the translation {' and '.join(parts)}",
        )
    return TranslationOutcome(text=restored, translated=True)


class TranslatedSop(SlasModel):
    """The Chinese SopModel derived from the English one, plus what could not be translated."""

    model: SopModel
    notes: list[str] = Field(default_factory=list)
    translated_fields: int = Field(ge=0)
    kept_fields: int = Field(ge=0)

    @property
    def complete(self) -> bool:
        return self.kept_fields == 0


def translate_sop(
    model: SopModel, *, translator: Translator, glossary: Glossary | None = None
) -> TranslatedSop:
    """Translate the prose fields; copy identifiers, results and glossary references by code."""
    glossary = glossary or default_glossary()
    notes: list[str] = []
    counts = {"translated": 0, "kept": 0}

    def one(field_name: str, english: str) -> str:
        outcome = translate_prose(english, translator=translator, glossary=glossary)
        if outcome.translated:
            counts["translated"] += 1
        elif english.strip():
            counts["kept"] += 1
        if outcome.note:
            notes.append(f"{field_name}: {outcome.note}")
        return outcome.text

    def many(field_name: str, items: list[str]) -> list[str]:
        return [one(f"{field_name}[{i}]", item) for i, item in enumerate(items, start=1)]

    steps = [
        SopStep(
            n=step.n,
            action=one(f"steps[{step.n}].action", step.action),
            expected=one(f"steps[{step.n}].expected", step.expected),
            evidence=list(step.evidence),  # evidence is identifiers: copied
        )
        for step in model.steps
    ]
    translated = SopModel(
        title=model.title,  # the title carries the ticket id and the job name: copied
        purpose=one("purpose", model.purpose),
        prerequisites=many("prerequisites", model.prerequisites),
        steps=steps,
        checks=many("checks", model.checks),
        results=dict(model.results),  # identifiers and states: copied
        findings=many("findings", model.findings),
        next_actions=many("next_actions", model.next_actions),
        glossary_refs=list(model.glossary_refs),
    )
    return TranslatedSop(
        model=translated,
        notes=notes,
        translated_fields=counts["translated"],
        kept_fields=counts["kept"],
    )


def glossary_references(model: SopModel, glossary: Glossary | None = None) -> list[str]:
    """`en → zh` for every glossary term the English prose uses; shown in both renderings."""
    glossary = glossary or default_glossary()
    prose = "\n".join(
        [
            model.purpose,
            *model.prerequisites,
            *(f"{s.action}\n{s.expected}" for s in model.steps),
            *model.checks,
            *model.findings,
            *model.next_actions,
        ]
    )
    visible = protect(prose, glossary=glossary).text
    terms = sorted(glossary.terms_in(visible), key=lambda t: t.en.lower())
    return [f"{term.en} → {term.zh_hant}" for term in terms]


class FakeTranslator:
    """A dictionary translator for tests.

    Known English phrases (with identifiers protected) map to Chinese; otherwise glossary
    terms are substituted and the rest is marked with a 譯 prefix. `drop_placeholder` and
    `wrong_term` make it misbehave so the verification path can be tested.
    """

    def __init__(
        self,
        phrases: dict[str, str] | None = None,
        *,
        glossary: Glossary | None = None,
        drop_placeholder: bool = False,
        wrong_term: dict[str, str] | None = None,
        fail: bool = False,
    ) -> None:
        self.phrases = dict(phrases or {})
        self.glossary = glossary or default_glossary()
        self.drop_placeholder = drop_placeholder
        self.wrong_term = dict(wrong_term or {})
        self.fail = fail
        self.requests: list[TranslationRequest] = []

    def translate(self, request: TranslationRequest) -> str:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("the planner instance did not answer")
        text = request.text
        if text in self.phrases:
            out = self.phrases[text]
        else:
            out = text
            for term in self.glossary.terms_in(text):
                out = term.pattern.sub(self.wrong_term.get(term.en, term.zh_hant), out)
            if out == text:
                out = f"譯：{text}"
        if self.drop_placeholder and request.placeholders:
            out = out.replace(f"⟦{request.placeholders}⟧", "")
        return out
