"""The model registry, the fit sentence and the vLLM container spec."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from slas_model_manager.fit import GpuFacts, fit, needed_gib, recommended_quant
from slas_model_manager.registry import (
    EXAMPLE_REGISTRY,
    RegistryError,
    example_registry,
    registry_from_mapping,
)
from slas_model_manager.runtime import (
    AIRGAP_ENV,
    MANDATORY_GENERATE_FLAGS,
    ContainerSpec,
    vllm_spec,
)

REGISTRY = example_registry()
IMAGE = "registry.internal/vllm/vllm-openai@sha256:" + "a" * 64


def mapping() -> dict[str, Any]:
    return copy.deepcopy(EXAMPLE_REGISTRY)


def test_example_registry_is_consistent() -> None:
    assert [m.id for m in REGISTRY.models][:2] == ["qwen2.5-coder-32b-awq", "deepseek-v3-fp8"]
    assert REGISTRY.roles["coder"] == "qwen2.5-coder-32b-awq"
    assert REGISTRY.voter_families() == ["Qwen", "DeepSeek", "Kimi"], "voters decorrelate by family"
    routes = REGISTRY.routes()
    assert routes.roles["coder"] == "vllm-coder"
    assert routes.voters == [
        "vllm-voter-qwen2.5-coder-32b-awq",
        "vllm-voter-deepseek-v3-fp8",
        "vllm-voter-kimi-k2-awq",
    ]
    assert REGISTRY.sentence().startswith("6 models; coder → Qwen2.5-Coder-32B")
    assert REGISTRY.sentence().endswith("3 voters from 3 model families.")
    assert REGISTRY.model("bge-m3").label() == "BGE-M3 (BF16)"
    with pytest.raises(KeyError):
        REGISTRY.model("nope")


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda d: d["roles"].__setitem__("coder", "missing"), "unknown model 'missing'"),
        (lambda d: d["roles"].__setitem__("dj", "bge-m3"), "unknown role 'dj'"),
        (lambda d: d["roles"].__setitem__("coder", "bge-m3"), "not declared for the coder role"),
        (lambda d: d["voters"].append("ghost"), "voter 'ghost' is not a model"),
        (lambda d: d["voters"].append("bge-m3") or d["voters"].append("bge-m3"), "distinct"),
        (lambda d: d["models"].append(dict(d["models"][0])), "unique"),
        (lambda d: d["models"][0].__setitem__("path", "https://hf.co/x"), "not a URL"),
        (lambda d: d["models"][0].__setitem__("roles", ["dj"]), "unknown roles dj"),
    ],
)
def test_registry_validation_speaks_in_sentences(mutate: Any, fragment: str) -> None:
    data = mapping()
    mutate(data)
    with pytest.raises(RegistryError) as raised:
        registry_from_mapping(data, source="Models/models.yaml")
    assert (
        raised.value.message.what_happened
        == "The model registry Models/models.yaml could not be used."
    )
    assert fragment in raised.value.message.likely_cause


def test_fit_sentences() -> None:
    coder = REGISTRY.model("qwen2.5-coder-32b-awq")
    assert needed_gib(coder) == 24.0
    h100 = GpuFacts(
        index=0, name="NVIDIA H100 80GB HBM3", arch="hopper", total_gib=79.6, free_gib=61.0
    )
    small = GpuFacts(index=1, name="NVIDIA L4", arch="ada", total_gib=22.5, free_gib=12.0)
    result = fit(coder, [small, h100])
    assert result.fits and result.gpu_index == 0
    assert result.sentence == (
        "Qwen2.5-Coder-32B (AWQ 4-bit) needs about 24 GiB of GPU memory; GPU 0 "
        "(NVIDIA H100 80GB HBM3) has 61 GiB free, so it fits."
    )
    short = fit(coder, [small])
    assert not short.fits
    assert short.sentence == (
        "Qwen2.5-Coder-32B (AWQ 4-bit) needs about 24 GiB of GPU memory, but the most that is "
        "free is 12 GiB on GPU 1 (NVIDIA L4), 12 GiB short. Free some GPU memory or pick a "
        "smaller quantisation."
    )
    bf16 = REGISTRY.model("bge-m3").model_copy(update={"vram_gib": 40.0})
    assert fit(bf16, [small]).sentence.endswith(
        "Use the AWQ 4-bit build; BF16 is only for eval regression."
    )
    none = fit(coder, [])
    assert not none.fits and none.gpu_index is None
    assert "no GPU was found" in none.sentence


def test_quantisation_rule_follows_section_7() -> None:
    assert recommended_quant("hopper") == "fp8" and recommended_quant("blackwell") == "fp8"
    assert recommended_quant("ada") == "awq4" and recommended_quant("ampere") == "awq4"
    assert recommended_quant("other") == "awq4"


def test_vllm_spec_carries_the_mandatory_flags_and_airgap_env() -> None:
    coder = REGISTRY.model("qwen2.5-coder-32b-awq")
    spec = vllm_spec(coder, name="vllm-coder", gpu_ids=[0, 1], image=IMAGE)
    for flag in MANDATORY_GENERATE_FLAGS:
        assert flag in spec.argv
    # vLLM's current flag; the removed `--guided-decoding-backend` would end the instance at
    # start with "unrecognized arguments".
    assert (
        spec.argv[spec.argv.index("--structured-outputs-config") + 1] == '{"backend": "xgrammar"}'
    )
    assert "--guided-decoding-backend" not in spec.argv
    # The model path is `vllm serve`'s positional argument (`--model` is deprecated there).
    assert spec.argv[:3] == ["/data/Models/qwen2.5-coder-32b-awq", "--served-model-name", coder.id]
    assert "--model" not in spec.argv
    assert (
        "--tensor-parallel-size" in spec.argv
        and spec.argv[spec.argv.index("--tensor-parallel-size") + 1] == "2"
    )
    assert spec.argv[spec.argv.index("--quantization") + 1] == "awq_marlin"
    assert spec.env["CUDA_VISIBLE_DEVICES"] == "0,1"
    for key, value in AIRGAP_ENV.items():
        assert spec.env[key] == value
    assert spec.network == "slas-inference" and spec.model_id == coder.id
    embed = vllm_spec(
        REGISTRY.model("bge-m3"), name="vllm-embed", gpu_ids=[0], image=IMAGE, generate=False
    )
    assert "--enable-prefix-caching" not in embed.argv and "--quantization" not in embed.argv
    fp8 = vllm_spec(
        REGISTRY.model("deepseek-v3-fp8"), name="vllm-planner", gpu_ids=[0], image=IMAGE
    )
    assert fp8.argv[fp8.argv.index("--quantization") + 1] == "fp8"


def test_container_spec_refuses_latest_missing_airgap_and_other_networks() -> None:
    base = vllm_spec(REGISTRY.model("bge-m3"), name="vllm-embed", gpu_ids=[0], image=IMAGE)
    with pytest.raises(ValueError, match="never latest"):
        ContainerSpec.model_validate({**base.model_dump(), "image": "vllm/vllm-openai:latest"})
    with pytest.raises(ValueError, match="airgap environment missing VLLM_NO_USAGE_STATS"):
        env = dict(base.env)
        del env["VLLM_NO_USAGE_STATS"]
        ContainerSpec.model_validate({**base.model_dump(), "env": env})
    with pytest.raises(ValueError, match="join only slas-inference"):
        ContainerSpec.model_validate({**base.model_dump(), "network": "slas-edge"})
    with pytest.raises(ValueError):
        ContainerSpec.model_validate({**base.model_dump(), "name": "notvllm"})
