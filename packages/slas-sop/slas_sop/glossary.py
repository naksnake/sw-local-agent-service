"""The project glossary pinned into every translation (CLAUDE.md §5.5, §14).

`docs/glossary.yaml` is rendered from `DEFAULT_GLOSSARY`; a unit test keeps file and code in
step. A term maps one English phrase to exactly one Traditional Chinese phrase (and an
optional Simplified one). The translator is told to use these and nothing else, and the
renderer verifies afterwards that it did (`slas_sop.translate`); the eval suite checks the
same thing across a whole SOP (`slas_eval.terminology`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

from pydantic import Field, ValidationError, model_validator

from slas_schemas.common import SlasModel, validation_sentence
from slas_schemas.errors import ThreePartMessage


class GlossaryTerm(SlasModel):
    en: str = Field(min_length=1)
    zh_hant: str = Field(min_length=1)
    zh_hans: str | None = None
    #: Chinese renderings a translator might reach for that the project does not use.
    avoid: list[str] = Field(default_factory=list)
    note: str = ""

    @property
    def pattern(self) -> re.Pattern[str]:
        """The English term as a whole phrase, case-insensitive, plural tolerated."""
        return re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(self.en)}(?:e?s)?(?![A-Za-z0-9])", re.IGNORECASE
        )

    def in_english(self, text: str) -> bool:
        return self.pattern.search(text) is not None

    def in_chinese(self, text: str) -> bool:
        return self.zh_hant in text


class Glossary(SlasModel):
    version: int = 1
    default_chinese: str = "zh-Hant"
    terms: list[GlossaryTerm]

    @model_validator(mode="after")
    def _unique_english(self) -> Glossary:
        seen: set[str] = set()
        for term in self.terms:
            key = term.en.lower()
            if key in seen:
                raise ValueError(f"the term {term.en!r} appears twice")
            seen.add(key)
        return self

    def lookup(self, english: str) -> GlossaryTerm | None:
        wanted = english.lower()
        for term in self.terms:
            if term.en.lower() == wanted:
                return term
        return None

    def terms_in(self, english_text: str) -> list[GlossaryTerm]:
        """Terms that occur in the text, longest English phrase first."""
        found = [term for term in self.terms if term.in_english(english_text)]
        found.sort(key=lambda term: (-len(term.en), term.en.lower()))
        return found

    def pinned_sentence(self, english_text: str | None = None) -> str:
        """The instruction handed to the translator: exact renderings, nothing else."""
        terms = self.terms_in(english_text) if english_text is not None else list(self.terms)
        if not terms:
            return "No glossary term occurs in this text."
        pairs = "; ".join(f"{term.en} → {term.zh_hant}" for term in terms)
        return f"Use these translations exactly and no others: {pairs}."


class GlossaryError(ValueError):
    def __init__(self, message: ThreePartMessage) -> None:
        super().__init__(message.what_happened)
        self.message = message


def glossary_from_mapping(data: object, *, source: str = "<memory>") -> Glossary:
    try:
        return Glossary.model_validate(data)
    except ValidationError as exc:
        raise GlossaryError(
            ThreePartMessage(
                f"The glossary in {source} could not be used.",
                validation_sentence(exc),
                f"Fix {source}; every term needs `en` and `zh_hant`, and English terms are unique.",
            )
        ) from exc


def _term(
    en: str,
    zh_hant: str,
    *,
    zh_hans: str | None = None,
    avoid: tuple[str, ...] = (),
    note: str = "",
) -> dict[str, object]:
    entry: dict[str, object] = {"en": en, "zh_hant": zh_hant}
    if zh_hans is not None:
        entry["zh_hans"] = zh_hans
    if avoid:
        entry["avoid"] = list(avoid)
    if note:
        entry["note"] = note
    return entry


DEFAULT_GLOSSARY: Final[dict[str, object]] = {
    "version": 1,
    "default_chinese": "zh-Hant",
    "terms": [
        _term("power cycle", "電源循環", zh_hans="电源循环", avoid=("重新上電", "斷電重啟")),
        _term("AC cycle", "AC 電源循環", zh_hans="AC 电源循环", note="The BMC cycles too."),
        _term("DC cycle", "DC 電源循環", zh_hans="DC 电源循环", note="The BMC stays up."),
        _term("warm boot", "暖開機", zh_hans="暖开机", avoid=("熱啟動",)),
        _term("baseline", "基準", zh_hans="基准", avoid=("基線", "底線")),
        _term("target", "受測機", zh_hans="受测机", note="The server under test (SUT)."),
        _term("test station", "測試站", zh_hans="测试站"),
        _term("station runner", "測試站代理程式", zh_hans="测试站代理程序"),
        _term("sandbox", "沙箱", zh_hans="沙箱"),
        _term(
            "firmware", "韌體", zh_hans="固件", avoid=("固件",), note="固件 is Simplified usage."
        ),
        _term("root cause", "根本原因", zh_hans="根本原因"),
        _term("root-cause analysis", "根本原因分析", zh_hans="根本原因分析"),
        _term("finding", "發現", zh_hans="发现"),
        _term("ticket", "工單", zh_hans="工单", avoid=("票",)),
        _term("bug ticket", "問題工單", zh_hans="问题工单"),
        _term("owner", "負責單位", zh_hans="负责单位"),
        _term("severity", "嚴重度", zh_hans="严重度"),
        _term("approval", "核准", zh_hans="核准", avoid=("批准",)),
        _term("cross-check", "交叉檢查", zh_hans="交叉检查"),
        _term("voter", "投票模型", zh_hans="投票模型"),
        _term("skill", "技能", zh_hans="技能"),
        _term("plan", "計畫", zh_hans="计划"),
        _term("step", "步驟", zh_hans="步骤"),
        _term("evidence", "證據", zh_hans="证据"),
        _term("expected result", "預期結果", zh_hans="预期结果"),
        _term("event log", "事件記錄", zh_hans="事件记录", note="SEL stays as SEL."),
        _term("fence marker", "隔離標記", zh_hans="隔离标记"),
        _term("fingerprint", "指紋", zh_hans="指纹"),
        _term("remote", "遠端儲存庫", zh_hans="远程仓库", note="A registered Git remote."),
        _term("bundle", "打包檔", zh_hans="打包文件", note="A `git bundle` file."),
        _term("walkthrough", "程式碼導覽", zh_hans="代码导览"),
        _term("report", "報告", zh_hans="报告"),
        _term("SOP", "標準作業程序", zh_hans="标准作业程序"),
        _term("prerequisite", "前置條件", zh_hans="前置条件"),
        _term("next action", "後續行動", zh_hans="后续行动"),
        _term("kernel", "核心", zh_hans="内核", note="The Agent Kernel."),
    ],
}

GLOSSARY_FILE_HEADER: Final = (
    "Project glossary for SW Local Agent Service (CLAUDE.md §5.5, §14).\n"
    "Rendered from slas_sop.glossary.DEFAULT_GLOSSARY; a unit test keeps file and code in\n"
    "step. Every translation pins these renderings; the renderer and the eval suite verify\n"
    "them. Identifiers, commands, numbers and versions are never translated (INV-13)."
)


def default_glossary() -> Glossary:
    return glossary_from_mapping(DEFAULT_GLOSSARY, source="docs/glossary.yaml")


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_glossary_yaml(data: Mapping[str, object], *, header: str = "") -> str:
    glossary = glossary_from_mapping(data)
    lines: list[str] = []
    if header:
        lines.extend(f"# {line}".rstrip() for line in header.splitlines())
    lines.append(f"version: {glossary.version}")
    lines.append(f"default_chinese: {glossary.default_chinese}")
    lines.append("terms:")
    for term in glossary.terms:
        lines.append(f"  - en: {_quote(term.en)}")
        lines.append(f"    zh_hant: {_quote(term.zh_hant)}")
        if term.zh_hans is not None:
            lines.append(f"    zh_hans: {_quote(term.zh_hans)}")
        if term.avoid:
            lines.append(f"    avoid: [{', '.join(_quote(a) for a in term.avoid)}]")
        if term.note:
            lines.append(f"    note: {_quote(term.note)}")
    return "\n".join(lines) + "\n"
