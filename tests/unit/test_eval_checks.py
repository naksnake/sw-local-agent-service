"""slas_eval: local judges only, terminology consistency, back-translation spot check."""

from __future__ import annotations

import pytest

from slas_eval.back_translation import spot_check, translated_pairs
from slas_eval.judges import (
    CloudEndpointError,
    FakeJudge,
    JudgeRequest,
    LocalJudgeEndpoint,
    assert_local_endpoint,
)
from slas_eval.terminology import check_sop_terminology, check_terminology
from slas_schemas.sop import SopModel, SopStep
from slas_sop.translate import FakeTranslator, translate_sop

EN = SopModel(
    title="DC cycle run — T-validation-0007",
    purpose="Verify the target survives 20 DC cycles without a PCIe link change.",
    prerequisites=["Agent: validation"],
    steps=[
        SopStep(
            n=1,
            action="Take the baseline",
            expected="LnkSta x16 on every GPU.",
            evidence=["step baseline", "exit 0"],
        ),
        SopStep(
            n=2,
            action="Run the power cycle",
            expected="The target boots within 900 s.",
            evidence=["step cycle", "exit 0"],
        ),
    ],
    checks=["Every step has an observation."],
    results={"ticket": "T-validation-0007", "state": "Done"},
    findings=[],
    next_actions=["Attach the report to the ticket."],
)
PHRASES = {
    "Verify the target survives ⟦1⟧ DC cycles without a ⟦2⟧ link change.": "驗證受測機在 ⟦1⟧ 次 DC 電源循環後 ⟦2⟧ 連結未改變。",
    "Take the baseline": "擷取基準",
    "⟦1⟧ ⟦2⟧ on every ⟦3⟧.": "每張 ⟦3⟧ 的 ⟦1⟧ 均為 ⟦2⟧。",
    "Run the power cycle": "執行電源循環",
    "The target boots within ⟦1⟧.": "受測機在 ⟦1⟧ 內開機完成。",
    "Every step has an observation.": "每個步驟都有觀察結果。",
    "Attach the report to the ticket.": "將報告附加到工單。",
    "Agent: validation": "代理：validation",
}


# --- judges -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://vllm-planner:8000/v1",
        "http://localhost:8000",
        "https://gateway.slas.internal",
        "http://10.20.30.40:8000",
        "http://[::1]:8000",
        "http://models.lab.local:8000",
    ],
)
def test_local_endpoints_are_accepted(url: str) -> None:
    assert LocalJudgeEndpoint(url=url).host == assert_local_endpoint(url)


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("https://api." + "openai.com/v1", "is a cloud AI service"),
        ("https://api." + "anthropic.com", "is a cloud AI service"),
        ("https://myco.openai." + "azure.com", "is a cloud AI service"),
        ("https://bedrock-runtime.us-east-1." + "amazonaws.com", "is a cloud AI service"),
        ("https://api-inference." + "huggingface.co", "is a cloud AI service"),
        ("https://models.example.com", "is outside the perimeter"),
        ("https://8.8.8.8", "is outside the perimeter"),
        ("ftp://vllm-planner", "is not a usable judge endpoint"),
        ("not a url", "is not a usable judge endpoint"),
    ],
)
def test_cloud_and_outside_endpoints_are_refused(url: str, fragment: str) -> None:
    with pytest.raises(CloudEndpointError) as raised:
        assert_local_endpoint(url)
    assert fragment in raised.value.message.what_happened
    assert "local" in raised.value.message.what_to_do.lower()
    with pytest.raises(Exception, match=r"cannot be a judge|not a usable"):
        LocalJudgeEndpoint(url=url)


def test_fake_judge_scores_overlap_and_honours_overrides() -> None:
    judge = FakeJudge({"nonsense": 0.1})
    same = judge.judge(
        JudgeRequest(task="t", original="the link dropped", candidate="the link dropped")
    )
    assert same.score == 1.0
    pinned = judge.judge(JudgeRequest(task="t", original="a", candidate="utter nonsense"))
    assert pinned.score == 0.1 and "pinned" in pinned.reason
    assert judge.judge(JudgeRequest(task="t", original="", candidate="")).score == 1.0
    assert judge.judge(JudgeRequest(task="t", original="", candidate="x")).score == 0.0
    assert len(judge.requests) == 4


# --- terminology --------------------------------------------------------------------------


def test_terminology_check_passes_for_a_pinned_translation() -> None:
    zh = translate_sop(EN, translator=FakeTranslator(PHRASES)).model
    report = check_sop_terminology(EN, zh)
    assert report.consistent and report.score == 1.0 and report.untranslated == []
    assert report.checked == [
        "DC cycle",
        "target",
        "baseline",
        "power cycle",
        "step",
        "report",
        "ticket",
    ]
    assert report.identifiers_match and "T-validation-0007" in report.identifiers_en
    assert report.sentence().startswith("All 7 glossary terms are rendered as pinned.")


def test_terminology_check_reports_missing_avoided_and_untranslated() -> None:
    wrong = FakeTranslator(PHRASES | {"Run the power cycle": "執行重新上電"})
    zh = translate_sop(EN, translator=wrong).model
    # translate_sop already fell back to English for that field; the check reports it.
    report = check_sop_terminology(EN, zh)
    assert report.untranslated == ["steps[2].action"]
    assert report.missing == ["power cycle → 電源循環"] and not report.consistent
    assert (
        "steps[2].action" not in report.sentence()
        and "1 field is still in English." in report.sentence()
    )

    direct = check_terminology("Repeat the power cycle.", "重複重新上電。")
    assert direct.missing == ["power cycle → 電源循環"] and direct.avoided == ["重新上電"]
    assert direct.score == 0.0
    assert direct.sentence() == (
        "0 of 1 glossary terms are rendered as pinned. Not as pinned: power cycle → 電源循環. "
        "Uses renderings to avoid: 重新上電. 0 identifiers are identical in both texts."
    )


def test_terminology_check_catches_a_changed_identifier() -> None:
    report = check_terminology("Set the timeout to 900 s on GPU3.", "將 GPU3 的逾時設為 90 s。")
    assert not report.identifiers_match and not report.consistent
    assert report.identifiers_en == ["900 s", "GPU3"] and report.identifiers_zh == ["GPU3", "90 s"]
    assert "only in English: 900 s; only in Chinese: 90 s" in report.sentence()
    reordered = check_terminology("GPU3 then GPU4", "GPU4 然後 GPU3")
    assert reordered.identifiers_match, "word order differs between languages; that is fine"
    repeated = check_terminology("GPU3 and GPU3", "GPU3")
    assert "differ between the two texts in how often they occur." in repeated.sentence()
    assert (
        check_terminology("no terms here", "這裡沒有術語")
        .sentence()
        .startswith("No glossary term occurs")
    )
    with pytest.raises(ValueError, match="same fields"):
        check_sop_terminology(EN, SopModel(title="x"))


# --- back-translation ---------------------------------------------------------------------


def test_spot_check_samples_deterministically_and_judges_the_round_trip() -> None:
    zh = translate_sop(EN, translator=FakeTranslator(PHRASES)).model
    pairs = translated_pairs(EN, zh)
    assert [p[0] for p in pairs] == [
        "purpose",
        "prerequisites[1]",
        "steps[1].action",
        "steps[1].expected",
        "steps[2].action",
        "steps[2].expected",
        "checks[1]",
        "next_actions[1]",
    ]
    back = FakeTranslator(
        {
            "代理：validation": "Agent: validation",
            "驗證受測機在 ⟦1⟧ 次 DC 電源循環後 ⟦2⟧ 連結未改變。": "Verify the target survives ⟦1⟧ DC cycles without a ⟦2⟧ link change.",
            "擷取基準": "Take the baseline",
            "每張 ⟦1⟧ 的 ⟦2⟧ 均為 ⟦3⟧。": "⟦2⟧ ⟦3⟧ on every ⟦1⟧.",
            "執行電源循環": "Run the power cycle",
            "受測機在 ⟦1⟧ 內開機完成。": "The target boots within ⟦1⟧.",
            "每個步驟都有觀察結果。": "Every step has an observation.",
            "將報告附加到工單。": "Attach the report to the ticket.",
        }
    )
    judge = FakeJudge()
    report = spot_check(pairs, back_translator=back, judge=judge, sample=3, seed=1)
    assert len(report.samples) == 3 and report.passed and report.mean == 1.0
    assert [s.field for s in report.samples] == [
        s.field
        for s in spot_check(
            pairs, back_translator=back, judge=FakeJudge(), sample=3, seed=1
        ).samples
    ]
    assert all(s.back_translation == s.english for s in report.samples)
    assert (
        report.sentence()
        == "3 of 3 sampled fields kept their meaning through back-translation (mean 1.00)."
    )
    assert back.requests[0].source_lang == "zh-Hant" and back.requests[0].target_lang == "en"
    reversed_hints = [r.glossary_sentence for r in back.requests]
    assert any("受測機 → target" in hint for hint in reversed_hints)

    everything = spot_check(pairs, back_translator=back, judge=judge, sample=50)
    assert len(everything.samples) == 8 and everything.passed


def test_spot_check_flags_a_drifted_or_broken_round_trip() -> None:
    zh = translate_sop(EN, translator=FakeTranslator(PHRASES)).model
    pairs = [
        p for p in translated_pairs(EN, zh) if p[0] in ("steps[1].action", "steps[2].expected")
    ]
    drifting = FakeTranslator(
        {
            "擷取基準": "Delete the baseline",  # meaning changed
            # "受測機在 ⟦1⟧ 內開機完成。" is unknown → the fake echoes with a 譯 prefix and keeps ⟦1⟧
        }
    )
    judge = FakeJudge({"Delete": 0.2})
    report = spot_check(pairs, back_translator=drifting, judge=judge, sample=5, threshold=0.7)
    assert [s.passed for s in report.samples] == [False, False]
    assert report.samples[0].score == 0.2 and report.samples[1].back_translation.startswith("譯：")
    assert report.sentence().endswith("Check: steps[1].action, steps[2].expected.")
    assert report.pass_rate == 0.0 and not report.passed

    broken = spot_check(pairs[:1], back_translator=FakeTranslator(fail=True), judge=judge)
    assert broken.samples[0].score == 0.0 and "back-translation failed" in broken.samples[0].reason
    lost = spot_check(pairs[1:], back_translator=FakeTranslator(drop_placeholder=True), judge=judge)
    assert "missing ⟦1⟧" in lost.samples[0].reason
    assert (
        spot_check([], back_translator=drifting, judge=judge)
        .sentence()
        .startswith("Nothing to spot-check")
    )
    with pytest.raises(ValueError, match="sample must be positive"):
        spot_check(pairs, back_translator=drifting, judge=judge, sample=0)
