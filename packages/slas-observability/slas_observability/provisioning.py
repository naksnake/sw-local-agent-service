"""Every file under `observability/`, rendered from code. `python -m slas_observability.render`
writes them; `tests/unit/test_observability_files_in_step.py` fails when either side drifts.

    observability/prometheus/prometheus.yml            scrape jobs, rule file, Alertmanager
    observability/prometheus/prometheus.prod.yml       the same with the prod registry's voters
    observability/prometheus/rules.yml                 recording and alert rules
    observability/alertmanager/alertmanager.yml        one receiver: the local channel webhook
    observability/grafana/provisioning/datasources/prometheus.yml
    observability/grafana/provisioning/dashboards/slas.yml
    observability/grafana/dashboards/<uid>.json        the six dashboards
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from slas_observability import yamlish
from slas_observability.alerts import alertmanager_config
from slas_observability.dashboards import DASHBOARDS, render_dashboard
from slas_observability.rules import prometheus_config, rules_config

HEADER: Final = (
    "# Rendered from slas_observability (CLAUDE.md §8.2, P11); a unit test keeps file and\n"
    "# code in step. Edit the Python, then run `uv run python -m slas_observability.render`.\n"
)


def grafana_datasource() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Prometheus",
                "type": "prometheus",
                "uid": "slas-prometheus",
                "access": "proxy",
                "url": "http://prometheus:9090",
                "isDefault": True,
                "editable": False,
                "jsonData": {"timeInterval": "15s", "httpMethod": "POST"},
            }
        ],
    }


def grafana_dashboard_provider() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "providers": [
            {
                "name": "slas",
                "orgId": 1,
                "folder": "SW Local Agent Service",
                "type": "file",
                "disableDeletion": True,
                "updateIntervalSeconds": 30,
                "allowUiUpdates": False,
                "options": {"path": "/etc/grafana/dashboards", "foldersFromFilesStructure": False},
            }
        ],
    }


def render_yaml(config: Mapping[str, Any]) -> str:
    return HEADER + yamlish.dump(config)


def render_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def rendered_files() -> dict[str, str]:
    """Relative path under `observability/` → content."""
    files = {
        "prometheus/prometheus.yml": render_yaml(prometheus_config("quickstart")),
        # Mounted by compose/prod.override.yml: the same file with the prod registry's voters.
        "prometheus/prometheus.prod.yml": render_yaml(prometheus_config("prod")),
        "prometheus/rules.yml": render_yaml(rules_config()),
        "alertmanager/alertmanager.yml": render_yaml(alertmanager_config()),
        "grafana/provisioning/datasources/prometheus.yml": render_yaml(grafana_datasource()),
        "grafana/provisioning/dashboards/slas.yml": render_yaml(grafana_dashboard_provider()),
    }
    for uid in DASHBOARDS:
        files[f"grafana/dashboards/{uid}.json"] = render_json(render_dashboard(uid))
    return files


def write_all(root: Path) -> list[Path]:
    written: list[Path] = []
    for relative, content in rendered_files().items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
