"""SopModel → sop.en.md + sop.zh-Hant.md + sop.json, always together (CLAUDE.md §5.5, INV-13).

The English model is the source. With a translator, the Chinese rendering comes from
`translate_sop`; without one, the Chinese file keeps the English prose under Chinese
headings and says so. In both cases identifiers, commands, numbers, versions and paths are
copied by code, and the two files carry identical identifier lists (`protect.identifiers`).
PDF output (WeasyPrint) waits on the dependency decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field

from slas_observability import metrics
from slas_schemas.common import Lang, SlasModel
from slas_schemas.envfile import write_atomic
from slas_schemas.sop import SopModel
from slas_schemas.ticket import SopRefs
from slas_sop.glossary import Glossary, default_glossary
from slas_sop.translate import TranslatedSop, Translator, glossary_references, translate_sop

HEADINGS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "language": "Language: English (en)",
        "notice_untranslated": (
            "> Placeholder rendering: structure, identifiers, commands and numbers are copied "
            "by code. No translator is configured, so the Chinese file keeps this English prose."
        ),
        "notice_translated": (
            "> Rendered from one structured source (sop.json). Identifiers, commands, numbers "
            "and versions are copied by code into both languages, never translated."
        ),
        "kept": "> {count} item(s) are shown in English in the Chinese file: {notes}",
        "purpose": "Purpose",
        "prerequisites": "Prerequisites",
        "steps": "Steps",
        "step_columns": "| Step | Action | Expected | Evidence |",
        "checks": "Checks",
        "results": "Results",
        "findings": "Findings",
        "next_actions": "Next actions",
        "glossary": "Glossary references",
        "none": "None.",
    },
    "zh-Hant": {
        "language": "語言：繁體中文（zh-Hant）",
        "notice_untranslated": (
            "> 佔位版本：結構、識別碼、指令與數值由程式直接複製；尚未設定翻譯模型，"
            "英文原文暫予保留。"
        ),
        "notice_translated": (
            "> 本文件與英文版由同一結構化來源（sop.json）產生；識別碼、指令、數值與版本"
            "由程式直接複製，未經翻譯。"
        ),
        "kept": "> 下列 {count} 項未通過翻譯檢查，保留英文：{notes}",
        "purpose": "目的",
        "prerequisites": "前置條件",
        "steps": "步驟",
        "step_columns": "| 步驟 | 動作 | 預期結果 | 證據 |",
        "checks": "檢查項目",
        "results": "結果",
        "findings": "發現",
        "next_actions": "後續行動",
        "glossary": "術語參照",
        "none": "無。",
    },
}


class UnsupportedLanguageError(ValueError):
    def __init__(self, lang: str) -> None:
        super().__init__(
            f"{lang} is not rendered yet: Simplified Chinese needs a conversion table or a "
            "second translation pass behind the Settings toggle (CLAUDE.md §5.5); this build "
            "renders en and zh-Hant."
        )


def _bullets(items: list[str], none: str) -> list[str]:
    return [f"- {item}" for item in items] if items else [none]


def render_markdown(
    model: SopModel,
    lang: Lang,
    *,
    translated: bool = False,
    kept_notes: list[str] | None = None,
) -> str:
    if lang not in HEADINGS:
        raise UnsupportedLanguageError(lang)
    text = HEADINGS[lang]
    notice = text["notice_translated" if translated else "notice_untranslated"]
    lines = [f"# {model.title}", "", text["language"], "", notice, ""]
    if kept_notes:
        lines += [text["kept"].format(count=len(kept_notes), notes="; ".join(kept_notes)), ""]
    lines += [f"## {text['purpose']}", "", model.purpose or text["none"], ""]
    lines += [f"## {text['prerequisites']}", "", *_bullets(model.prerequisites, text["none"]), ""]
    lines += [f"## {text['steps']}", ""]
    if model.steps:
        lines += [text["step_columns"], "|---|---|---|---|"]
        for step in model.steps:
            evidence = ", ".join(step.evidence)
            lines.append(f"| {step.n} | {step.action} | {step.expected} | {evidence} |")
    else:
        lines.append(text["none"])
    lines += ["", f"## {text['checks']}", "", *_bullets(model.checks, text["none"]), ""]
    lines += [f"## {text['results']}", ""]
    lines += [f"- {key}: {value}" for key, value in model.results.items()] or [text["none"]]
    lines += ["", f"## {text['findings']}", "", *_bullets(model.findings, text["none"]), ""]
    lines += [f"## {text['next_actions']}", "", *_bullets(model.next_actions, text["none"]), ""]
    lines += [f"## {text['glossary']}", "", *_bullets(model.glossary_refs, text["none"]), ""]
    return "\n".join(lines)


class RenderedSop(SlasModel):
    refs: SopRefs
    translated: bool
    notes: list[str] = Field(default_factory=list)
    glossary_refs: list[str] = Field(default_factory=list)

    def sentence(self) -> str:
        if not self.translated:
            return (
                "The SOP was written in English and Chinese; the Chinese file keeps the "
                "English prose because no translator is configured."
            )
        if self.notes:
            count = len(self.notes)
            return (
                f"The SOP was written in English and Chinese; {count} "
                f"{'item' if count == 1 else 'items'} stayed in English because the "
                "translation failed a check."
            )
        return "The SOP was written in English and Chinese from one structured source."


def render_sop(
    model: SopModel,
    out_dir: Path,
    *,
    translator: Translator | None = None,
    glossary: Glossary | None = None,
) -> RenderedSop:
    """Write sop.en.md, sop.zh-Hant.md and sop.json together — never one without the others."""
    glossary = glossary or default_glossary()
    refs_list = glossary_references(model, glossary)
    source = model.model_copy(update={"glossary_refs": refs_list}) if refs_list else model
    chinese: TranslatedSop | None = None
    if translator is not None:
        chinese = translate_sop(source, translator=translator, glossary=glossary)
    zh_model = chinese.model if chinese is not None else source
    notes = chinese.notes if chinese is not None else []

    out_dir.mkdir(parents=True, exist_ok=True)
    en = out_dir / "sop.en.md"
    zh = out_dir / "sop.zh-Hant.md"
    data = out_dir / "sop.json"
    translated = chinese is not None
    write_atomic(en, render_markdown(source, "en", translated=translated), mode=0o644)
    write_atomic(
        zh,
        render_markdown(zh_model, "zh-Hant", translated=translated, kept_notes=notes),
        mode=0o644,
    )
    write_atomic(data, source.model_dump_json(indent=2) + "\n", mode=0o644)
    metrics.inc("slas_sop_exports_total", lang="en")
    metrics.inc("slas_sop_exports_total", lang="zh-Hant")
    return RenderedSop(
        refs=SopRefs(en=str(en), zh=str(zh), data=str(data)),
        translated=translated,
        notes=notes,
        glossary_refs=refs_list,
    )
