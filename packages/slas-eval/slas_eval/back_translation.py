"""Back-translation spot check (CLAUDE.md §5.5): zh → en through a local model, then a local
judge scores whether the round trip kept the meaning. A sample, not every field: the
terminology check is exhaustive and cheap; this one costs two model calls per item.
"""

from __future__ import annotations

import random

from pydantic import Field

from slas_eval.judges import Judge, JudgeRequest
from slas_schemas.common import SlasModel
from slas_schemas.sop import SopModel
from slas_sop.glossary import Glossary, default_glossary
from slas_sop.protect import ProtectionError, protect, restore
from slas_sop.translate import TranslationRequest, Translator


class SampleResult(SlasModel):
    field: str
    english: str
    chinese: str
    back_translation: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    passed: bool


class BackTranslationReport(SlasModel):
    threshold: float = Field(ge=0.0, le=1.0)
    samples: list[SampleResult] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)

    @property
    def mean(self) -> float:
        return sum(s.score for s in self.samples) / len(self.samples) if self.samples else 1.0

    @property
    def pass_rate(self) -> float:
        return sum(1 for s in self.samples if s.passed) / len(self.samples) if self.samples else 1.0

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.samples)

    def sentence(self) -> str:
        n = len(self.samples)
        if not n:
            return "Nothing to spot-check: no translated field was sampled."
        failed = [s.field for s in self.samples if not s.passed]
        head = (
            f"{n - len(failed)} of {n} sampled fields kept their meaning through "
            f"back-translation (mean {self.mean:.2f})."
        )
        if failed:
            return head + " Check: " + ", ".join(failed) + "."
        return head


def translated_pairs(english: SopModel, chinese: SopModel) -> list[tuple[str, str, str]]:
    """(field, en, zh) for every prose field whose Chinese differs from the English."""
    pairs: list[tuple[str, str, str]] = []

    def add(name: str, en: str, zh: str) -> None:
        if en.strip() and zh.strip() and en.strip() != zh.strip():
            pairs.append((name, en, zh))

    add("purpose", english.purpose, chinese.purpose)
    for i, (en, zh) in enumerate(zip(english.prerequisites, chinese.prerequisites, strict=True), 1):
        add(f"prerequisites[{i}]", en, zh)
    for en_step, zh_step in zip(english.steps, chinese.steps, strict=True):
        add(f"steps[{en_step.n}].action", en_step.action, zh_step.action)
        add(f"steps[{en_step.n}].expected", en_step.expected, zh_step.expected)
    for i, (en, zh) in enumerate(zip(english.checks, chinese.checks, strict=True), 1):
        add(f"checks[{i}]", en, zh)
    for i, (en, zh) in enumerate(zip(english.findings, chinese.findings, strict=True), 1):
        add(f"findings[{i}]", en, zh)
    for i, (en, zh) in enumerate(zip(english.next_actions, chinese.next_actions, strict=True), 1):
        add(f"next_actions[{i}]", en, zh)
    return pairs


def spot_check(
    pairs: list[tuple[str, str, str]],
    *,
    back_translator: Translator,
    judge: Judge,
    sample: int = 5,
    seed: int = 0,
    threshold: float = 0.7,
    glossary: Glossary | None = None,
) -> BackTranslationReport:
    """Sample `sample` pairs deterministically, back-translate each, and let the judge score."""
    if sample < 1:
        raise ValueError("sample must be positive")
    glossary = glossary or default_glossary()
    chosen = list(pairs)
    if len(chosen) > sample:
        chosen = random.Random(seed).sample(chosen, sample)  # noqa: S311 — sampling, not security
        chosen.sort(key=lambda p: pairs.index(p))
    report = BackTranslationReport(threshold=threshold)
    for field, english, chinese in chosen:
        protected = protect(chinese, glossary=glossary)
        request = TranslationRequest(
            text=protected.text,
            source_lang="zh-Hant",
            target_lang="en",
            glossary_sentence=_reverse_glossary(glossary, chinese),
            placeholders=protected.count,
        )
        try:
            back = restore(back_translator.translate(request).strip(), protected)
        except (ProtectionError, Exception) as exc:  # a bad round trip is a finding, not a crash
            report.samples.append(
                SampleResult(
                    field=field,
                    english=english,
                    chinese=chinese,
                    back_translation="",
                    score=0.0,
                    reason=f"back-translation failed: {exc}",
                    passed=False,
                )
            )
            continue
        verdict = judge.judge(
            JudgeRequest(task="back-translation equivalence", original=english, candidate=back)
        )
        report.samples.append(
            SampleResult(
                field=field,
                english=english,
                chinese=chinese,
                back_translation=back,
                score=verdict.score,
                reason=verdict.reason,
                passed=verdict.score >= threshold,
            )
        )
    return report


def _reverse_glossary(glossary: Glossary, chinese: str) -> str:
    terms = [term for term in glossary.terms if term.zh_hant in chinese]
    if not terms:
        return "No glossary term occurs in this text."
    pairs = "; ".join(f"{term.zh_hant} → {term.en}" for term in terms)
    return f"Use these translations exactly and no others: {pairs}."
