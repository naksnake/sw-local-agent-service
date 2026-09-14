"""slas_sop: glossary pinned, identifiers protected by code, English kept when a check fails."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from slas_kernel.clock import FakeClock
from slas_kernel.executor import FakeExecutor
from slas_kernel.kernel import Kernel
from slas_kernel.null_agent import NullAgent
from slas_kernel.store import FileTicketStore
from slas_schemas.job import Upload
from slas_schemas.sop import SopModel, SopStep
from slas_sop.glossary import (
    DEFAULT_GLOSSARY,
    GLOSSARY_FILE_HEADER,
    GlossaryError,
    default_glossary,
    glossary_from_mapping,
    render_glossary_yaml,
)
from slas_sop.protect import ProtectionError, identifiers, protect, restore
from slas_sop.render import RenderedSop, UnsupportedLanguageError, render_markdown, render_sop
from slas_sop.translate import (
    FakeTranslator,
    TranslationRequest,
    glossary_references,
    translate_prose,
    translate_sop,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SENTENCE = (
    "After the power cycle, compare LnkSta on GPU3 (0000:8a:00.0) with the baseline; "
    "firmware v1.2.3 is in `sop.en.md` under /AI/Agent/SOP, ticket T-validation-0007, 30 s settle."
)


# --- glossary -----------------------------------------------------------------------------


def test_glossary_yaml_is_rendered_from_code_and_has_the_spec_examples() -> None:
    expected = render_glossary_yaml(DEFAULT_GLOSSARY, header=GLOSSARY_FILE_HEADER)
    assert (REPO_ROOT / "docs" / "glossary.yaml").read_text(encoding="utf-8") == expected
    glossary = default_glossary()
    assert glossary.lookup("power cycle") is not None
    assert glossary.lookup("Power Cycle").zh_hant == "電源循環"  # type: ignore[union-attr]
    assert glossary.lookup("baseline").zh_hant == "基準"  # type: ignore[union-attr]
    assert glossary.lookup("nothing") is None
    assert '  - en: "power cycle"\n    zh_hant: "電源循環"\n' in expected


def test_glossary_finds_terms_longest_first_and_pins_them() -> None:
    glossary = default_glossary()
    terms = [t.en for t in glossary.terms_in("Run the DC cycle and compare with the baseline.")]
    assert terms == ["baseline", "DC cycle"], "same length → alphabetical"
    assert glossary.pinned_sentence("Run the DC cycle and compare with the baseline.") == (
        "Use these translations exactly and no others: baseline → 基準; DC cycle → DC 電源循環."
    )
    assert [t.en for t in glossary.terms_in("Twenty DC cycles and three findings")] == [
        "DC cycle",
        "finding",
    ], "plurals count"
    assert glossary.pinned_sentence("Hello.") == "No glossary term occurs in this text."
    assert "power cycle → 電源循環" in glossary.pinned_sentence()
    assert not glossary.lookup("step").in_english("footsteps")  # type: ignore[union-attr]
    with pytest.raises(GlossaryError) as raised:
        glossary_from_mapping(
            {"terms": [{"en": "a", "zh_hant": "甲"}, {"en": "A", "zh_hant": "乙"}]}, source="g.yaml"
        )
    assert "appears twice" in raised.value.message.likely_cause


# --- protect ------------------------------------------------------------------------------


def test_protect_replaces_every_identifier_and_restores_it() -> None:
    protected = protect(SENTENCE)
    assert list(protected.slots.values()) == [
        "LnkSta",
        "GPU3",
        "0000:8a:00.0",
        "v1.2.3",
        "`sop.en.md`",
        "/AI/Agent/SOP",
        "T-validation-0007",
        "30 s",
    ]
    assert protected.text == (
        "After the power cycle, compare ⟦1⟧ on ⟦2⟧ (⟦3⟧) with the baseline; firmware ⟦4⟧ is in "
        "⟦5⟧ under ⟦6⟧, ticket ⟦7⟧, ⟦8⟧ settle."
    )
    assert restore(protected.text, protected) == SENTENCE
    assert identifiers(SENTENCE) == list(protected.slots.values())
    assert protect("").slots == {} and protect("plain words only").text == "plain words only"
    # An acronym that is part of a glossary term stays visible for the translator, and the
    # same acronym inside the Chinese rendering is left alone too, so both sides agree.
    assert protect("Run 20 DC cycles; check the SOP.").text == "Run ⟦1⟧ DC cycles; check the SOP."
    assert identifiers("Run 20 DC cycles; check the SOP.") == ["20"]
    assert identifiers("執行 20 次 DC 電源循環；檢查標準作業程序。") == ["20"]
    assert identifiers("BMC SEL on GPU3") == ["BMC", "SEL", "GPU3"], "not glossary terms → copied"


@pytest.mark.parametrize(
    ("translated", "problem"),
    [
        ("⟦1⟧ ⟦2⟧ ⟦3⟧ ⟦4⟧ ⟦5⟧ ⟦6⟧ ⟦7⟧", "missing ⟦8⟧"),
        ("⟦1⟧ ⟦2⟧ ⟦3⟧ ⟦4⟧ ⟦5⟧ ⟦6⟧ ⟦7⟧ ⟦8⟧ ⟦9⟧", "invented ⟦9⟧"),
        ("⟦1⟧ ⟦1⟧ ⟦2⟧ ⟦3⟧ ⟦4⟧ ⟦5⟧ ⟦6⟧ ⟦7⟧ ⟦8⟧", "duplicated ⟦1⟧"),
    ],
)
def test_restore_rejects_lost_invented_or_duplicated_placeholders(
    translated: str, problem: str
) -> None:
    protected = protect(SENTENCE)
    with pytest.raises(ProtectionError, match=re.escape(problem)):
        restore(translated, protected)


# --- translate ----------------------------------------------------------------------------


def test_translate_prose_pins_the_glossary_and_keeps_identifiers() -> None:
    translator = FakeTranslator(
        {
            "After the power cycle, compare ⟦1⟧ on ⟦2⟧ (⟦3⟧) with the baseline; firmware ⟦4⟧ is in "
            "⟦5⟧ under ⟦6⟧, ticket ⟦7⟧, ⟦8⟧ settle.": "電源循環後，比較 ⟦2⟧（⟦3⟧）的 ⟦1⟧ 與基準；韌體 ⟦4⟧ 位於 ⟦6⟧ 下的 ⟦5⟧，工單 ⟦7⟧，靜置 ⟦8⟧。"
        }
    )
    outcome = translate_prose(SENTENCE, translator=translator)
    assert outcome.translated and outcome.note is None
    assert outcome.text == (
        "電源循環後，比較 GPU3（0000:8a:00.0）的 LnkSta 與基準；韌體 v1.2.3 位於 /AI/Agent/SOP 下的 "
        "`sop.en.md`，工單 T-validation-0007，靜置 30 s。"
    )
    assert sorted(identifiers(outcome.text)) == sorted(identifiers(SENTENCE)), "reordered, not lost"
    request = translator.requests[0]
    assert request.placeholders == 8 and request.target_lang == "zh-Hant"
    assert (
        "power cycle → 電源循環" in request.glossary_sentence
        and "firmware → 韌體" in request.glossary_sentence
    )
    assert request.instruction().startswith(
        "Translate from en to zh-Hant. Keep every ⟦n⟧ placeholder"
    )


def test_translate_prose_keeps_english_when_a_check_fails() -> None:
    lost = translate_prose(SENTENCE, translator=FakeTranslator(drop_placeholder=True))
    assert not lost.translated and lost.text == SENTENCE
    assert lost.note == "kept in English: the translation lost an identifier (missing ⟦8⟧)"

    wrong = translate_prose(
        "Repeat the power cycle.", translator=FakeTranslator(wrong_term={"power cycle": "重新上電"})
    )
    assert not wrong.translated and wrong.text == "Repeat the power cycle."
    assert (
        wrong.note
        == "kept in English: the translation did not use power cycle → 電源循環 and used 重新上電"
    )

    failed = translate_prose("Repeat the power cycle.", translator=FakeTranslator(fail=True))
    assert (
        failed.note
        == "kept in English: the translator failed (the planner instance did not answer)"
    )

    class Silent:
        def translate(self, request: TranslationRequest) -> str:
            return "   "

    empty = translate_prose("Repeat the power cycle.", translator=Silent())
    assert empty.note == "kept in English: the translator returned nothing"

    assert translate_prose("", translator=FakeTranslator()).text == ""
    only_ids = translate_prose("T-validation-0007: 0000:8a:00.0", translator=FakeTranslator())
    assert not only_ids.translated and only_ids.note is None, "nothing to translate"


def test_translate_sop_copies_identifiers_results_and_evidence() -> None:
    model = SopModel(
        title="DC cycle run — T-validation-0007",
        purpose="Verify the target survives 20 DC cycles without a PCIe link change.",
        prerequisites=["Agent: validation", "Requested by: pat"],
        steps=[
            SopStep(
                n=1,
                action="Take the baseline",
                expected="LnkSta x16 on every GPU.",
                evidence=["step baseline", "exit 0"],
            )
        ],
        checks=["Every step has an observation."],
        results={"ticket": "T-validation-0007", "state": "Needs review"},
        findings=["[Issue] PCIe link lost on GPU3 (0000:8a:00.0) during DC cycle | [Owner] EE"],
        next_actions=["Review the findings and decide whether a bug ticket is needed."],
    )
    translator = FakeTranslator(
        {
            "Verify the target survives ⟦1⟧ DC cycles without a ⟦2⟧ link change.": "驗證受測機在 ⟦1⟧ 次 DC 電源循環後 ⟦2⟧ 連結未改變。",
            "Take the baseline": "擷取基準",
            "⟦1⟧ ⟦2⟧ on every ⟦3⟧.": "每張 ⟦3⟧ 的 ⟦1⟧ 均為 ⟦2⟧。",
            "Every step has an observation.": "每個步驟都有觀察結果。",
            "⟦1⟧ ⟦2⟧ link lost on ⟦3⟧ (⟦4⟧) during DC cycle | ⟦5⟧ ⟦6⟧": "⟦1⟧ DC 電源循環期間 ⟦3⟧（⟦4⟧）的 ⟦2⟧ 連結中斷 | ⟦5⟧ ⟦6⟧",
            "Review the findings and decide whether a bug ticket is needed.": "檢視發現並決定是否需要問題工單。",
            "Agent: validation": "代理：validation",
            "Requested by: pat": "申請人：pat",
        }
    )
    result = translate_sop(model, translator=translator)
    zh = result.model
    assert (
        zh.title == model.title
        and zh.results == model.results
        and zh.steps[0].evidence == ["step baseline", "exit 0"]
    )
    assert zh.purpose == "驗證受測機在 20 次 DC 電源循環後 PCIe 連結未改變。"
    assert (
        zh.steps[0].action == "擷取基準" and zh.steps[0].expected == "每張 GPU 的 LnkSta 均為 x16。"
    )
    assert zh.findings == [
        "[Issue] DC 電源循環期間 GPU3（0000:8a:00.0）的 PCIe 連結中斷 | [Owner] EE"
    ]
    assert zh.prerequisites == ["代理：validation", "申請人：pat"]
    assert result.translated_fields == 8 and result.kept_fields == 0 and result.complete
    assert result.notes == []
    assert glossary_references(model) == [
        "baseline → 基準",
        "bug ticket → 問題工單",
        "DC cycle → DC 電源循環",
        "finding → 發現",
        "step → 步驟",
        "target → 受測機",
        "ticket → 工單",
    ]


# --- render -------------------------------------------------------------------------------


def test_render_sop_with_a_translator_writes_real_chinese_beside_identical_identifiers(
    tmp_path: Path,
) -> None:
    model = SopModel(
        title="Rehearsal — T-null-0001",
        purpose="Rehearse the kernel lifecycle end to end without touching any machine.",
        steps=[
            SopStep(
                n=1,
                action="Say hello",
                expected="Say hello finished as expected.",
                evidence=["step s1", "exit 0"],
            )
        ],
        results={"ticket": "T-null-0001", "state": "Done"},
    )
    translator = FakeTranslator(
        {
            "Rehearse the kernel lifecycle end to end without touching any machine.": "在不接觸任何機器的情況下，端到端演練核心生命週期。",
            "Say hello": "說聲你好",
            "Say hello finished as expected.": "說聲你好如預期完成。",
        }
    )
    rendered = render_sop(model, tmp_path, translator=translator)
    assert isinstance(rendered, RenderedSop) and rendered.translated and rendered.notes == []
    assert (
        rendered.sentence()
        == "The SOP was written in English and Chinese from one structured source."
    )
    en = Path(rendered.refs.en).read_text(encoding="utf-8")
    zh = Path(rendered.refs.zh).read_text(encoding="utf-8")
    assert "> Rendered from one structured source (sop.json)." in en and "佔位版本" not in zh
    assert "本文件與英文版由同一結構化來源（sop.json）產生" in zh
    assert "| 1 | 說聲你好 | 說聲你好如預期完成。 | step s1, exit 0 |" in zh
    assert "| 1 | Say hello | Say hello finished as expected. | step s1, exit 0 |" in en
    assert "- kernel → 核心" in en and "- kernel → 核心" in zh, "glossary references in both"

    def body(text: str) -> str:
        return "\n".join(
            line
            for line in text.splitlines()
            if not line.startswith(">") and not line.startswith(("Language:", "語言"))
        )

    assert identifiers(body(en)) == identifiers(body(zh))
    data = json.loads(Path(rendered.refs.data).read_text(encoding="utf-8"))
    assert data["purpose"].startswith("Rehearse"), "sop.json is the English source"
    assert data["glossary_refs"] == ["kernel → 核心"]


def test_render_sop_reports_items_kept_in_english(tmp_path: Path) -> None:
    model = SopModel(
        title="T", purpose="Repeat the power cycle.", checks=["Compare with the baseline."]
    )
    rendered = render_sop(
        model, tmp_path, translator=FakeTranslator(wrong_term={"power cycle": "重新上電"})
    )
    assert rendered.translated and rendered.notes == [
        "purpose: kept in English: the translation did not use power cycle → 電源循環 and used 重新上電"
    ]
    assert rendered.sentence() == (
        "The SOP was written in English and Chinese; 1 item stayed in English because the translation failed a check."
    )
    zh = Path(rendered.refs.zh).read_text(encoding="utf-8")
    assert "> 下列 1 項未通過翻譯檢查，保留英文：purpose: kept in English" in zh
    assert "Repeat the power cycle." in zh, "the failed field keeps its English"
    assert "基準" in zh, "the other field was translated with the glossary"


def test_render_markdown_without_a_translator_is_honest_and_refuses_zh_hans(tmp_path: Path) -> None:
    model = SopModel(title="Empty")
    assert "No translator is configured" in render_markdown(model, "en")
    assert "尚未設定翻譯模型" in render_markdown(model, "zh-Hant")
    with pytest.raises(UnsupportedLanguageError, match="zh-Hans is not rendered yet"):
        render_markdown(model, "zh-Hans")
    rendered = render_sop(model, tmp_path)
    assert not rendered.translated and rendered.glossary_refs == []
    assert rendered.sentence().endswith("because no translator is configured.")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sop.en.md", "sop.json", "sop.zh-Hant.md"]


# --- the P5 done-when: the NullAgent's SOP in real EN and 中文 side by side --------------------


def test_null_agent_sop_renders_english_and_chinese_side_by_side(tmp_path: Path) -> None:
    translator = FakeTranslator(
        {
            "Rehearse the kernel lifecycle end to end without touching any machine.": "在不接觸任何機器的情況下，端到端演練核心生命週期。",
            "Say hello": "說聲你好",
            "List the inputs": "列出輸入",
            "Count to three": "數到三",
            "Warn on stderr": "在 stderr 發出警告",
            "Finish": "結束",
            "Say hello finished as expected.": "說聲你好如預期完成。",
            "List the inputs finished as expected.": "列出輸入如預期完成。",
            "Count to three finished as expected.": "數到三如預期完成。",
            "Warn on stderr finished as expected.": "在 stderr 發出警告如預期完成。",
            "Finish finished as expected.": "結束如預期完成。",
            "Every step has an observation.": "每個步驟都有觀察結果。",
            "The journal has one observation per step.": "日誌中每個步驟各有一筆觀察結果。",
        }
    )
    kernel = Kernel(
        data_root=tmp_path,
        agent=NullAgent(),
        executor=FakeExecutor(),
        store=FileTicketStore(tmp_path),
        clock=FakeClock(),
        translator=translator,
    )
    ticket = kernel.run(Upload(filename="plan.md", uploaded_by="pat"))
    assert ticket.sop is not None
    en = Path(ticket.sop.en).read_text(encoding="utf-8")
    zh = Path(ticket.sop.zh).read_text(encoding="utf-8")
    assert "| 3 | Count to three | Count to three finished as expected. | step s3, exit 0 |" in en
    assert "| 3 | 數到三 | 數到三如預期完成。 | step s3, exit 0 |" in zh
    assert "- 每個步驟都有觀察結果。" in zh and "- Every step has an observation." in en
    assert f"- ticket: {ticket.id}" in en and f"- ticket: {ticket.id}" in zh
    assert "- state: Done" in en and "- state: Done" in zh

    def body(text: str) -> str:
        return "\n".join(
            line
            for line in text.splitlines()
            if not line.startswith(">") and not line.startswith(("Language:", "語言"))
        )

    assert identifiers(body(en)) == identifiers(body(zh))
    assert "Warn on stderr" not in body(zh).split("## 結果")[0], "every step title was translated"
    journal = (tmp_path / "Tickets" / ticket.id / "journal.jsonl").read_text(encoding="utf-8")
    assert '"translated":true' in journal and '"kept_in_english":[]' in journal
