"""Everything under observability/ is rendered from code and references only metrics that
exist; the six dashboards and the two required alerts are present."""

from __future__ import annotations

import json
from pathlib import Path

from slas_observability.dashboards import (
    DASHBOARDS,
    expressions,
    metric_names_in,
    render_dashboard,
)
from slas_observability.metrics import METRIC_NAMES
from slas_observability.provisioning import rendered_files, write_all
from slas_observability.render import main as render_main
from slas_observability.rules import (
    EXTERNAL_METRICS,
    SERVICES,
    VLLM_ROLES,
    VLLM_VOTERS,
    known_metric_names,
    prometheus_config,
    rule_groups,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
OBS = REPO_ROOT / "observability"


def known_metrics() -> set[str]:
    names = known_metric_names()
    assert names >= METRIC_NAMES and names >= EXTERNAL_METRICS
    return names


def test_every_provisioned_file_is_in_step_with_the_code() -> None:
    files = rendered_files()
    assert len(files) == 12
    for relative, content in files.items():
        path = OBS / relative
        assert path.is_file(), (
            f"{relative} is missing; run `uv run python -m slas_observability.render`"
        )
        assert path.read_text(encoding="utf-8") == content, (
            f"{relative} drifted from slas_observability; regenerate it"
        )


def test_render_writes_the_same_files_anywhere(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    written = write_all(tmp_path)
    assert len(written) == 12
    assert render_main([str(tmp_path / "again")]) == 0
    assert "12 files written" in capsys.readouterr().out
    assert (tmp_path / "again" / "prometheus" / "rules.yml").read_text() == (
        tmp_path / "prometheus" / "rules.yml"
    ).read_text()


def test_the_six_dashboards_reference_only_metrics_that_exist() -> None:
    assert set(DASHBOARDS) == {
        "slas-inference",
        "slas-gpu",
        "slas-agents",
        "slas-sandboxes-screens",
        "slas-validation",
        "slas-factory",
    }
    known = known_metrics()
    seen_slas: set[str] = set()
    for uid in DASHBOARDS:
        dashboard = render_dashboard(uid)
        assert dashboard["uid"] == uid and dashboard["title"].startswith("SLAS · ")
        assert dashboard["editable"] is False and dashboard["schemaVersion"] == 39
        assert len(dashboard["panels"]) >= 5
        ids = [panel["id"] for panel in dashboard["panels"]]
        assert ids == list(range(1, len(ids) + 1))
        for panel in dashboard["panels"]:
            pos = panel["gridPos"]
            assert pos["x"] >= 0 and pos["x"] + pos["w"] <= 24, (uid, panel["title"])
            assert panel["targets"], (uid, panel["title"])
            assert panel["title"][0].isupper()
        for expr in expressions(dashboard):
            names = metric_names_in(expr)
            assert names, expr
            unknown = {n for n in names if n not in known}
            assert not unknown, f"{uid}: {expr} references {sorted(unknown)}"
            for name in names:
                if name.startswith("slas_"):
                    base = name
                    for suffix in ("_bucket", "_sum", "_count"):
                        if name.endswith(suffix) and name[: -len(suffix)] in METRIC_NAMES:
                            base = name[: -len(suffix)]
                    seen_slas.add(base)
    missing = {
        n for n in METRIC_NAMES if n not in seen_slas and n not in {"slas_alerts_raised_total"}
    }
    assert not missing, f"metrics no dashboard shows: {sorted(missing)}"
    # The written JSON is the same document.
    on_disk = json.loads((OBS / "grafana" / "dashboards" / "slas-factory.json").read_text())
    assert on_disk == render_dashboard("slas-factory")


def test_metric_name_extraction_skips_labels_functions_and_groupings() -> None:
    assert metric_names_in(
        'sum by (outcome) (rate(slas_gateway_requests_total{outcome!="ok"}[5m]))'
    ) == {"slas_gateway_requests_total"}
    assert metric_names_in(
        "histogram_quantile(0.95, sum by (le, instance) "
        "(rate(vllm:time_to_first_token_seconds_bucket[5m])))"
    ) == {"vllm:time_to_first_token_seconds_bucket"}
    assert metric_names_in(
        "DCGM_FI_DEV_FB_USED / clamp_min(DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE, 1)"
    ) == {
        "DCGM_FI_DEV_FB_USED",
        "DCGM_FI_DEV_FB_FREE",
    }
    assert metric_names_in("slas:gateway_error_ratio:15m > 0.05") == {
        "slas:gateway_error_ratio:15m"
    }


def test_the_rules_carry_the_two_required_alerts_in_three_parts() -> None:
    known = known_metrics()
    alerts = {
        rule["alert"]: rule for group in rule_groups() for rule in group["rules"] if "alert" in rule
    }
    assert "SlasCircuitBreakerOpen" in alerts and "SlasConsensusDisagreement" in alerts
    assert alerts["SlasCircuitBreakerOpen"]["expr"] == "slas_breaker_open == 1"
    assert alerts["SlasConsensusDisagreement"]["expr"] == (
        "increase(slas_consensus_disagreements_total[10m]) > 0"
    )
    assert len(alerts) >= 18
    for name, rule in alerts.items():
        assert set(rule["annotations"]) == {"summary", "likely_cause", "what_to_do"}, name
        assert rule["labels"]["severity"] in ("info", "warning", "critical"), name
        assert all(text.endswith(".") for text in rule["annotations"].values()), name
        unknown = {n for n in metric_names_in(rule["expr"]) if n not in known}
        assert not unknown, f"{name}: {sorted(unknown)}"
    groups = [group["name"] for group in rule_groups()]
    assert groups == [
        "slas-recording",
        "slas-inference",
        "slas-consensus",
        "slas-gpu",
        "slas-agents",
        "slas-sandboxes-screens",
        "slas-validation",
        "slas-factory",
    ]


def test_prometheus_scrapes_every_vllm_instance_the_model_manager_starts() -> None:
    """Roles and voters (`vllm-<role>`, `vllm-voter-<model id>`), per profile, in step with
    the shipped registries (contract round 2 §3; CLAUDE.md §15 decision 14)."""
    for profile in ("quickstart", "prod"):
        # config/models.<profile>.yaml is itself kept in step with
        # slas_model_manager.registry.PROFILE_REGISTRIES by its own test; its `roles:` and
        # `voters:` blocks are flat, so plain line parsing reads them.
        text = (REPO_ROOT / "config" / f"models.{profile}.yaml").read_text(encoding="utf-8")
        roles_block = text.split("\nroles:\n", 1)[1].split("\nvoters:\n", 1)[0]
        voters_block = text.split("\nvoters:\n", 1)[1]
        roles = [line.strip().split(":")[0] for line in roles_block.splitlines() if line.strip()]
        voters = [line.strip()[2:] for line in voters_block.splitlines() if line.startswith("  - ")]
        assert VLLM_VOTERS[profile] == tuple(voters), (
            f"slas_observability.rules.VLLM_VOTERS[{profile!r}] drifted from "
            f"config/models.{profile}.yaml"
        )
        assert set(roles) == set(VLLM_ROLES)
        jobs = {job["job_name"]: job for job in prometheus_config(profile)["scrape_configs"]}
        targets = [t for sc in jobs["vllm"]["static_configs"] for t in sc["targets"]]
        # The instance names the model manager gives them (`vllm-<role>`, `vllm-voter-<id>`).
        assert targets == [f"vllm-{role}:8000" for role in VLLM_ROLES] + [
            f"vllm-voter-{voter}:8000" for voter in voters
        ]
        voter_labels = [
            sc["labels"] for sc in jobs["vllm"]["static_configs"] if sc["labels"]["role"] == "voter"
        ]
        assert [labels["voter"] for labels in voter_labels] == voters
    prod_text = (OBS / "prometheus" / "prometheus.prod.yml").read_text()
    quickstart_text = (OBS / "prometheus" / "prometheus.yml").read_text()
    assert "vllm-voter-minimax-m2.7:8000" in prod_text
    assert "vllm-voter-minimax-m2.7:8000" not in quickstart_text, (
        "a quickstart host must not scrape (and alert on) the prod-only third voter"
    )
    assert "vllm-voter-qwen3.8-27b-fp8:8000" in quickstart_text


def test_prometheus_scrapes_every_service_and_the_files_are_plain_yaml() -> None:
    config = prometheus_config()
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    targets = [t for job in jobs["slas-services"]["static_configs"] for t in job["targets"]]
    assert targets == [f"{service}:8000" for service in (*SERVICES, "model-fetcher")]
    # The model fetcher is quickstart only (ADR-0018): a prod Prometheus does not scrape it.
    prod_jobs = {job["job_name"]: job for job in prometheus_config("prod")["scrape_configs"]}
    prod_targets = [t for sc in prod_jobs["slas-services"]["static_configs"] for t in sc["targets"]]
    assert prod_targets == [f"{service}:8000" for service in SERVICES]
    assert "model-fetcher:8000" in (OBS / "prometheus" / "prometheus.yml").read_text()
    assert "model-fetcher:8000" not in (OBS / "prometheus" / "prometheus.prod.yml").read_text()
    assert "vllm-coder:8000" in [t for sc in jobs["vllm"]["static_configs"] for t in sc["targets"]]
    assert config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"] == [
        "alertmanager:9093"
    ]
    rules_text = (OBS / "prometheus" / "rules.yml").read_text()
    assert rules_text.startswith("# Rendered from slas_observability")
    assert (
        '      - alert: SlasCircuitBreakerOpen\n        expr: "slas_breaker_open == 1"\n'
        in rules_text
    )
    datasource = (OBS / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text()
    assert "url: http://prometheus:9090" in datasource and "uid: slas-prometheus" in datasource
    provider = (OBS / "grafana" / "provisioning" / "dashboards" / "slas.yml").read_text()
    assert "path: /etc/grafana/dashboards" in provider
    alertmanager = (OBS / "alertmanager" / "alertmanager.yml").read_text()
    assert "url: http://api:8000/internal/alerts" in alertmanager
    for text in (rules_text, datasource, provider, alertmanager):
        assert "http://" not in text.replace("http://prometheus:9090", "").replace(
            "http://api:8000/internal/alerts", ""
        ), "no other host is referenced"
