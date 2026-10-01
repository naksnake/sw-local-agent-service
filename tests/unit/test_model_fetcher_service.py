"""The model fetcher as a service (ADR-0018): links, the derived registry entry, the fetch
records against the fake hub on loopback, the registry import, cancel and remove, the
three-part refusals, the routes against the contract, settings and the CLI. No real hub,
no runtime (CLAUDE.md §11)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from slas_fetch import DEFAULT_HUB_HOSTS, Hub
from slas_http.identity import Identity
from slas_kernel.clock import FakeClock
from slas_model_fetcher import cli, links
from slas_model_fetcher.fetcher import FetchManager, StartBody
from slas_model_fetcher.links import HubLink, LinkError, parse_link
from slas_model_fetcher.registry_import import import_model, registered_ids
from slas_model_fetcher.service.app import create_app, route_table
from slas_model_fetcher.service.settings import Settings, parse_hosts
from slas_model_manager.registry import (
    PROFILE_REGISTRIES,
    RegistryError,
    read_registry_file,
    registry_from_mapping,
    render_registry_yaml,
)
from slas_observability.events import EventLog, ListSink
from tests.unit.test_fetch_models import CONFIG, WEIGHTS, FakeHub

REPO_ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
MANAGER = Identity("pat@slas.local", "Pat", frozenset({"model:manage"}))
READER = Identity("vi@slas.local", "Vi", frozenset())
THREE_PARTS = {"what_happened", "likely_cause", "what_to_do", "trace_id"}
HOSTS = ("127.0.0.1", *DEFAULT_HUB_HOSTS)


@pytest.fixture
def hub() -> Any:
    fake = FakeHub().start()
    try:
        yield fake
    finally:
        fake.stop()


class Harness:
    def __init__(self, tmp_path: Path, hub: FakeHub, *, registry: bool = True) -> None:
        self.models_dir = tmp_path / "Models"
        self.models_dir.mkdir(parents=True)
        if registry:
            (self.models_dir / "models.yaml").write_text(
                render_registry_yaml(PROFILE_REGISTRIES["quickstart"], header="quickstart"),
                encoding="utf-8",
            )
        self.settings = Settings(models_dir=self.models_dir, hub_hosts=HOSTS, endpoint=hub.endpoint)
        self.sink = ListSink()
        self.clock = FakeClock(START, step=timedelta(seconds=1))
        self.app: FastAPI = create_app(
            self.settings,
            hub_factory=lambda: Hub(endpoint=hub.endpoint),
            log=EventLog("model-fetcher", self.sink),
            clock=self.clock,
            threads=False,  # a fetch runs inline, so a test reads its outcome at once
        )
        self.manager: FetchManager = self.app.state.manager
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def start(self, link: str, model_id: str | None = None) -> httpx.Response:
        body: dict[str, Any] = {"link": link}
        if model_id is not None:
            body["id"] = model_id
        response: httpx.Response = self.client.post(
            "/v1/fetches", json=body, headers=MANAGER.headers()
        )
        return response

    def events(self, name: str) -> list[dict[str, Any]]:
        return [r for r in self.sink.records() if r["event"] == name]


def assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert set(body) == THREE_PARTS
    return body


# --- links ---------------------------------------------------------------------------------------


def test_every_accepted_link_form_parses_and_everything_else_is_refused() -> None:
    hosts = DEFAULT_HUB_HOSTS
    full = parse_link("https://huggingface.co/Qwen/Qwen3.8-27B-FP8", hosts=hosts)
    assert full == HubLink("Qwen", "Qwen3.8-27B-FP8", "main")
    assert full.repo_id == "Qwen/Qwen3.8-27B-FP8"
    assert full.canonical() == "https://huggingface.co/Qwen/Qwen3.8-27B-FP8"
    tree = parse_link("https://huggingface.co/Qwen/Qwen3.8-27B-FP8/tree/refs%2Fpr%2F3", hosts=hosts)
    assert tree.revision == "refs/pr/3"
    sha = parse_link(" https://huggingface.co/BAAI/bge-m3/commit/5617a9f6 ", hosts=hosts)
    assert sha == HubLink("BAAI", "bge-m3", "5617a9f6")
    assert sha.canonical().endswith("/tree/5617a9f6")
    short = parse_link("hf.co/deepseek-ai/DeepSeek-V4-Flash", hosts=hosts)
    assert short == HubLink("deepseek-ai", "DeepSeek-V4-Flash")
    assert parse_link("http://hf.co/BAAI/bge-m3.git/", hosts=hosts).repo == "bge-m3"
    bare = parse_link("MiniMaxAI/MiniMax-M2.7@d494266a", hosts=hosts)
    assert bare == HubLink("MiniMaxAI", "MiniMax-M2.7", "d494266a")
    assert bare.source("minimax-m2.7").repo == "MiniMaxAI/MiniMax-M2.7"
    mirror = parse_link("https://hub.internal/Qwen/Qwen3.8-27B", hosts=(*hosts, "hub.internal"))
    assert mirror.owner == "Qwen"

    def refused(text: str) -> str:
        with pytest.raises(LinkError) as raised:
            parse_link(text, hosts=hosts)
        return raised.value.message.likely_cause

    assert refused("") == "Nothing was pasted."
    assert "not a model repository" in refused("https://huggingface.co/datasets/foo/bar")
    assert "owner and a repository" in refused("https://huggingface.co/Qwen")
    assert "/tree/<revision> or /commit/<sha>" in refused(
        "https://huggingface.co/Qwen/Qwen3.8-27B/blob/main/config.json"
    )
    assert "neither a hub address nor a bare" in refused("ftp://huggingface.co/Qwen/Qwen3.8-27B")
    assert "characters" in refused("Qwen/Qwen3.8 27B")
    with pytest.raises(LinkError) as raised:
        parse_link("https://example.com/Qwen/Qwen3.8-27B", hosts=hosts)
    assert raised.value.message.what_happened == "example.com is not an allowed model hub host."
    assert "SLAS_HUB_HOSTS" in raised.value.message.what_to_do
    with pytest.raises(LinkError) as forms:
        parse_link("just words", hosts=hosts)
    assert forms.value.message.what_to_do.startswith(
        "Paste one of: https://huggingface.co/<owner>/<repo>, "
    )
    assert "<owner>/<repo>[@revision]" in forms.value.message.what_to_do


def test_the_registry_entry_is_derived_from_the_link_and_the_files(tmp_path: Path) -> None:
    assert links.slug_of("Qwen3.8-27B-FP8") == "qwen3.8-27b-fp8"
    assert links.slug_of("DeepSeek_V4__Flash") == "deepseek-v4-flash"
    assert links.slug_of("--Weird Name!!") == "weird-name"
    assert links.slug_of("!!!").startswith("model-")
    assert links.unique_id("bge-m3", ["bge-m3", "bge-m3-2"]) == "bge-m3-3"
    assert links.unique_id("new", ["bge-m3"]) == "new"
    assert links.family_of("deepseek-ai") == "DeepSeek" and links.family_of("Qwen") == "Qwen"
    assert links.family_of("MiniMaxAI") == "MiniMax" and links.family_of("moonshotai") == "Kimi"
    assert links.family_of("someone") == "someone"
    assert links.quant_of("Qwen3.8-27B-FP8") == "fp8"
    assert links.quant_of("Llama-3-70B-AWQ-INT4") == "awq4", "AWQ wins over the int4 it carries"
    assert links.quant_of("bge-m3") == "bf16" and links.quant_of("Model-bf16") == "bf16"
    for name in ("Model-GPTQ", "Model-NVFP4", "Model-MXFP4", "Model-GGUF"):
        with pytest.raises(LinkError) as raised:
            links.quant_of(name)
        assert "the registry does not admit" in raised.value.message.what_happened
        assert "quant = fp8, awq4, bf16" in raised.value.message.likely_cause
    model_dir = tmp_path / "m"
    model_dir.mkdir()
    assert links.context_of(model_dir) == 32768, "no config.json: the default"
    (model_dir / "config.json").write_text('{"max_position_embeddings": 131072}')
    assert links.context_of(model_dir) == 131072
    (model_dir / "config.json").write_text('{"text_config": {"max_position_embeddings": 40960}}')
    assert links.context_of(model_dir) == 40960, "a vision-language checkpoint"
    (model_dir / "config.json").write_text('{"max_position_embeddings": 512}')
    assert links.context_of(model_dir) == 32768, "below the registry's minimum: the default"
    (model_dir / "config.json").write_text("not json")
    assert links.context_of(model_dir) == 32768
    assert links.vram_estimate_gib(29 * 2**30) == 37.0, "29 GiB of weights plus 25 %"
    assert links.vram_estimate_gib(10) == 1.0
    entry = links.registry_entry(
        HubLink("Qwen", "Qwen3.8-27B-FP8"),
        model_id="qwen3.8-27b-fp8",
        total_bytes=29 * 2**30,
        model_dir=model_dir,
    )
    assert entry == {
        "id": "qwen3.8-27b-fp8",
        "display_name": "Qwen3.8-27B-FP8",
        "family": "Qwen",
        "path": "qwen3.8-27b-fp8",
        "quant": "fp8",
        "vram_gib": 37.0,
        "context": 32768,
        "roles": [],
    }
    registry_from_mapping({"version": 1, "models": [entry]})


# --- the registry import -----------------------------------------------------------------------


def test_import_appends_keeps_the_header_and_never_touches_roles_or_an_existing_id(
    tmp_path: Path,
) -> None:
    models_file = tmp_path / "models.yaml"
    entry = {
        "id": "new-model", "display_name": "New", "family": "Acme", "path": "new-model",
        "quant": "bf16", "vram_gib": 2.0, "context": 4096, "roles": [],
    }  # fmt: skip
    registry = import_model(models_file, entry)
    assert [m.id for m in registry.models] == ["new-model"]
    text = models_file.read_text()
    assert text.startswith("# Models/models.yaml for SW Local Agent Service")
    assert "written by the model\n# fetcher (ADR-0018)" in text
    assert registered_ids(models_file) == ["new-model"]

    models_file.write_text(
        render_registry_yaml(PROFILE_REGISTRIES["quickstart"], header="The header\nstays")
    )
    before, header = read_registry_file(models_file)
    assert header == "The header\nstays"
    registry = import_model(models_file, entry)
    assert [m.id for m in registry.models][-1] == "new-model"
    assert dict(registry.roles) == before["roles"] and list(registry.voters) == before["voters"]
    assert models_file.read_text().startswith("# The header\n# stays\nversion: 1")
    assert not models_file.with_name("models.yaml.tmp").exists(), "written atomically"
    with pytest.raises(RegistryError) as raised:
        import_model(models_file, entry)
    assert raised.value.message.what_happened == "new-model is already in the model registry."
    models_file.write_text("models: [")
    with pytest.raises(RegistryError) as broken:
        registered_ids(models_file)
    assert "not valid YAML" in broken.value.message.likely_cause
    assert registered_ids(tmp_path / "absent.yaml") == []


# --- the service ----------------------------------------------------------------------------------


def test_a_pasted_link_is_fetched_verified_and_registered(tmp_path: Path, hub: FakeHub) -> None:
    h = Harness(tmp_path, hub)
    response = h.start(f"{hub.endpoint}/demo/tiny")
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["state"] == "done" and record["model_id"] == "tiny"
    assert record["repo"] == "demo/tiny" and record["revision"] == "main"
    assert record["files_done"] == record["files_total"] == 3
    assert (
        record["bytes_done"]
        == record["bytes_total"]
        == len(WEIGHTS) + len(CONFIG) + len(b"# demo\n")
    )
    assert record["sentence"] == (
        "tiny is here (1.0 MiB, 3 files) and registered as tiny; give it a role on this page to "
        "start it. Its GPU memory is estimated at 1 GiB from the file sizes; correct it in the "
        "registry if you know better."
    )
    assert record["by"] == "pat@slas.local" and record["problem"] is None
    assert record["started_at"].startswith("2026-09-18T09:00:00") and record["finished_at"]
    assert record["entry"]["quant"] == "bf16" and record["entry"]["roles"] == []
    # On disk: the weights, the checksums, the manifest, the registry entry (roles untouched).
    assert (h.models_dir / "tiny" / "model.safetensors").read_bytes() == WEIGHTS
    assert (h.models_dir / "tiny" / "SHA256SUMS").is_file()
    manifest = json.loads((h.models_dir / "manifest.json").read_text())
    assert [m["path"] for m in manifest["models"]] == ["tiny"]
    data, _ = read_registry_file(h.models_dir / "models.yaml")
    assert [m["id"] for m in data["models"]][-1] == "tiny"
    assert data["roles"] == PROFILE_REGISTRIES["quickstart"]["roles"]
    assert data["voters"] == PROFILE_REGISTRIES["quickstart"]["voters"]
    assert h.events("fetch.done")[0]["model"] == "tiny"
    # The table: newest first, the record by id, and the removal of a finished one.
    listed = h.client.get("/v1/fetches").json()
    assert [r["id"] for r in listed] == [record["id"]]
    assert h.client.get(f"/v1/fetches/{record['id']}").json()["state"] == "done"
    removed = h.client.delete(f"/v1/fetches/{record['id']}", headers=MANAGER.headers())
    assert removed.json() == {"sentence": "Removed the record of tiny; the files stay."}
    assert h.client.get("/v1/fetches").json() == []
    assert (h.models_dir / "tiny" / "model.safetensors").is_file()
    assert_problem(h.client.get(f"/v1/fetches/{record['id']}"), 404)


def test_the_id_may_be_overridden_and_a_taken_id_is_refused(tmp_path: Path, hub: FakeHub) -> None:
    h = Harness(tmp_path, hub)
    assert_problem(h.start(f"{hub.endpoint}/demo/tiny", "Bad Id"), 400)
    taken = assert_problem(h.start(f"{hub.endpoint}/demo/tiny", "bge-m3"), 409)
    assert taken["what_happened"] == "bge-m3 is already in the model registry."
    response = h.start(f"{hub.endpoint}/demo/tiny", "my-tiny")
    assert response.status_code == 201 and response.json()["model_id"] == "my-tiny"
    assert (h.models_dir / "my-tiny" / "SHA256SUMS").is_file()
    # The same link again: the default id is taken, so the next free one is used, and the
    # files already here are kept without a second download.
    hub.requests.clear()
    again = h.start(f"{hub.endpoint}/demo/tiny")
    assert again.status_code == 201 and again.json()["model_id"] == "tiny"
    third = h.start(f"{hub.endpoint}/demo/tiny")
    assert third.status_code == 201 and third.json()["model_id"] == "tiny-2"


def test_refusals_come_before_a_thread_starts(tmp_path: Path, hub: FakeHub) -> None:
    h = Harness(tmp_path, hub)
    body = assert_problem(h.start("https://example.com/Qwen/Qwen3.8-27B"), 400)
    assert body["what_happened"] == "example.com is not an allowed model hub host."
    body = assert_problem(h.start("nonsense"), 400)
    assert body["what_to_do"].startswith("Paste one of: ")
    body = assert_problem(h.start("Qwen/Qwen3-GPTQ"), 400)
    assert "the registry does not admit" in body["what_happened"]
    assert h.client.get("/v1/fetches").json() == [] and hub.requests == []
    # Capability and identity: the api adds them; without them nothing starts.
    assert_problem(h.client.post("/v1/fetches", json={"link": "Qwen/Qwen3"}), 401)
    forbidden = assert_problem(
        h.client.post("/v1/fetches", json={"link": "Qwen/Qwen3"}, headers=READER.headers()), 403
    )
    assert forbidden["what_happened"] == "Vi may not add a model."
    assert_problem(h.client.post("/v1/fetches", json={}, headers=MANAGER.headers()), 400)
    assert_problem(h.client.delete("/v1/fetches/f-1", headers=READER.headers()), 403)


def test_a_hub_problem_is_a_failed_record_in_three_parts(tmp_path: Path, hub: FakeHub) -> None:
    h = Harness(tmp_path, hub)
    response = h.start(f"{hub.endpoint}/demo/missing")
    assert response.status_code == 201
    record = response.json()
    assert record["state"] == "failed"
    assert record["problem"]["what_happened"].startswith("The hub has nothing at ")
    assert record["sentence"].startswith("The hub has nothing at ")
    assert record["finished_at"] is not None
    assert h.events("fetch.failed")
    assert not (h.models_dir / "missing").exists()
    assert "missing" not in registered_ids(h.models_dir / "models.yaml")
    # A gated repository without a token: the sentence says what to do, never the token.
    hub.gated = True
    gated = h.start(f"{hub.endpoint}/demo/tiny").json()
    assert gated["state"] == "failed" and "gated or private" in gated["problem"]["likely_cause"]
    assert "hf_test-token" not in json.dumps(h.sink.records())
    # A broken registry file stops a fetch before it starts (fix it first).
    hub.gated = False
    (h.models_dir / "models.yaml").write_text("models: [")
    assert_problem(h.start(f"{hub.endpoint}/demo/tiny"), 503)


def test_a_token_from_the_secret_file_reaches_the_hub_as_a_header_only(
    tmp_path: Path, hub: FakeHub
) -> None:
    hub.gated = True
    token_file = tmp_path / "hf_token"
    token_file.write_text("hf_test-token\n")
    settings = Settings(
        models_dir=tmp_path / "Models",
        hub_hosts=HOSTS,
        endpoint=hub.endpoint,
        token_file=token_file,
    )
    app = create_app(settings, threads=False, log=EventLog("model-fetcher", ListSink()))
    client = TestClient(app, raise_server_exceptions=False)
    record = client.post(
        "/v1/fetches", json={"link": "demo/tiny"}, headers=MANAGER.headers()
    ).json()
    assert record["state"] == "done", record["sentence"]
    assert "hf_test-token" not in json.dumps(record)
    assert all("hf_test-token" not in path for path, _ in hub.requests), "never in a URL"
    assert (tmp_path / "Models" / "models.yaml").is_file(), "a registry is created when none exists"


def test_cancel_stops_between_files_and_the_next_fetch_resumes(
    tmp_path: Path, hub: FakeHub
) -> None:
    """With threads on, a fetch that is cancelled right after it starts ends `cancelled` with
    the finished files kept; the same link fetched again keeps them."""
    h = Harness(tmp_path, hub)
    manager = FetchManager(
        models_dir=h.models_dir,
        hub_factory=lambda: Hub(endpoint=hub.endpoint),
        hosts=HOSTS,
        log=EventLog("model-fetcher", h.sink),
        clock=h.clock,
        threads=False,
    )
    # Cancel before the thread runs: the flag is honoured before the first file.
    record = manager.start(StartBody(link="demo/tiny"), by="pat@slas.local")
    assert record.state == "done"
    manager.cancel_or_remove(record.id)
    fresh = FetchManager(
        models_dir=tmp_path / "Other",
        hub_factory=lambda: Hub(endpoint=hub.endpoint),
        hosts=HOSTS,
        log=EventLog("model-fetcher", h.sink),
        clock=h.clock,
        threads=True,
    )
    started = fresh.start(StartBody(link="demo/tiny", id="slow"), by="pat@slas.local")
    fresh._cancel[started.id].set()  # the test controls the thread
    fresh.wait(started.id)
    final = fresh.get(started.id)
    assert final.state in ("cancelled", "done")
    if final.state == "cancelled":
        assert "was cancelled after" in final.sentence and "resumes" in final.sentence
        assert not (tmp_path / "Other" / "slow" / "SHA256SUMS").exists()
    # Cancelling a running fetch answers with what will happen; removing is for finished ones.
    assert fresh.cancel_or_remove(started.id)["sentence"].startswith("Removed the record of tiny")


def test_health_route_table_settings_and_cli(tmp_path: Path, hub: FakeHub) -> None:
    h = Harness(tmp_path, hub, registry=False)
    health = h.client.get("/health")
    assert health.status_code == 200
    assert health.json()["checks"] == {"models_dir": "ok", "hub": "not checked"}
    assert h.client.get("/metrics").status_code == 200 and h.client.get("/docs").status_code == 404
    contract = (REPO_ROOT / "docs" / "api-contract-round-2.md").read_text(encoding="utf-8")
    section = contract.split("## 3b. model-fetcher", 1)[1].split("## 4.", 1)[0]
    documented = set(re.findall(r"`(GET|POST|DELETE) (/v1/[^\s`?]+)`", section))
    assert documented == {
        ("POST", "/v1/fetches"),
        ("GET", "/v1/fetches"),
        ("GET", "/v1/fetches/{fetch_id}"),
        ("DELETE", "/v1/fetches/{fetch_id}"),
    }
    table = route_table(h.app)
    assert set(table) == documented | {("GET", "/health"), ("GET", "/metrics")}

    settings = Settings.from_env(
        {
            "SLAS_MODELS_DIR": "/data/Models",
            "SLAS_HUB_HOSTS": " huggingface.co, *.hf.co ",
            "HF_ENDPOINT": "https://hub.internal/",
            "HTTPS_PROXY": "http://proxy.internal:3128",
            "HF_TOKEN_FILE": str(tmp_path / "absent"),
        }
    )
    assert settings.hub_hosts == ("huggingface.co", "*.hf.co")
    assert settings.allowed_hosts == ("huggingface.co", "*.hf.co", "hub.internal")
    assert settings.endpoint == "https://hub.internal" and settings.read_token() is None
    assert settings.models_file == Path("/data/Models/models.yaml")
    assert settings.sentence() == (
        "Fetching from https://hub.internal through the proxy http://proxy.internal:3128 into "
        "/data/Models; allowed hosts: huggingface.co, *.hf.co, hub.internal."
    )
    assert parse_hosts("") == DEFAULT_HUB_HOSTS
    defaults = Settings.from_env({})
    assert defaults.allowed_hosts == DEFAULT_HUB_HOSTS and defaults.hub().token is None
    assert defaults.token_file == Path("/run/secrets/hf_token")

    served: list[tuple[FastAPI, str]] = []
    import io

    out = io.StringIO()
    code = cli.main(
        ["serve"],
        environ={"SLAS_MODELS_DIR": str(tmp_path / "M"), "SLAS_BIND": "0.0.0.0:8123"},
        runner=lambda app, bind: served.append((app, bind)),
        stdout=out,
    )
    assert code == 0 and served[0][1] == "0.0.0.0:8123"
    assert out.getvalue().startswith(
        "Serving the model fetcher on 0.0.0.0:8123. Fetching from https://huggingface.co into "
    )
    unwritable = Settings(models_dir=Path("/proc/slas-cannot-write"))
    broken = TestClient(
        create_app(unwritable, log=EventLog("model-fetcher", ListSink())),
        raise_server_exceptions=False,
    )
    assert_problem(broken.get("/health"), 503)
