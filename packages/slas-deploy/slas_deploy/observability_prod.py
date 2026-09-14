"""Loki and Tempo for the prod profile (CLAUDE.md §3 prod observability column, §8.2 trace ids;
ADR-0012), plus the Grafana datasources that point at them. Rendered to `config/loki/` and
`config/tempo/`. Single-binary, filesystem storage under the containers' volumes, no
analytics, no egress. Tempo receives the same `traceparent` the platform already forwards
(slas_observability.tracing) over OTLP/HTTP on the backend network.
"""

from __future__ import annotations

from typing import Any


def loki_config() -> dict[str, Any]:
    return {
        "auth_enabled": False,
        "analytics": {"reporting_enabled": False},
        "server": {"http_listen_port": 3100, "log_level": "info"},
        "common": {
            "path_prefix": "/loki",
            "replication_factor": 1,
            "ring": {"kvstore": {"store": "inmemory"}},
            "storage": {
                "filesystem": {"chunks_directory": "/loki/chunks", "rules_directory": "/loki/rules"}
            },
        },
        "schema_config": {
            "configs": [
                {
                    "from": "2026-01-01",
                    "store": "tsdb",
                    "object_store": "filesystem",
                    "schema": "v13",
                    "index": {"prefix": "index_", "period": "24h"},
                }
            ]
        },
        "limits_config": {
            "retention_period": "720h",
            "reject_old_samples": True,
            "reject_old_samples_max_age": "168h",
        },
        "compactor": {
            "working_directory": "/loki/compactor",
            "retention_enabled": True,
            "delete_request_store": "filesystem",
        },
    }


def tempo_config() -> dict[str, Any]:
    return {
        "usage_report": {"reporting_enabled": False},
        "server": {"http_listen_port": 3200, "log_level": "info"},
        "distributor": {
            "receivers": {
                "otlp": {
                    "protocols": {
                        "http": {"endpoint": "0.0.0.0:4318"},
                        "grpc": {"endpoint": "0.0.0.0:4317"},
                    }
                }
            }
        },
        "ingester": {"max_block_duration": "5m"},
        "compactor": {"compaction": {"block_retention": "720h"}},
        "storage": {
            "trace": {
                "backend": "local",
                "wal": {"path": "/var/tempo/wal"},
                "local": {"path": "/var/tempo/blocks"},
            }
        },
        "metrics_generator": {
            "registry": {"external_labels": {"source": "tempo"}},
            "storage": {"path": "/var/tempo/generator/wal"},
        },
    }


def grafana_datasources() -> dict[str, Any]:
    return {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Loki",
                "type": "loki",
                "uid": "slas-loki",
                "access": "proxy",
                "url": "http://loki:3100",
                "editable": False,
                "jsonData": {
                    "derivedFields": [
                        {
                            "name": "trace_id",
                            "matcherRegex": '"trace_id": ?"([0-9a-f]{32})"',
                            "url": "${__value.raw}",
                            "datasourceUid": "slas-tempo",
                        }
                    ]
                },
            },
            {
                "name": "Tempo",
                "type": "tempo",
                "uid": "slas-tempo",
                "access": "proxy",
                "url": "http://tempo:3200",
                "editable": False,
                "jsonData": {
                    "tracesToLogsV2": {"datasourceUid": "slas-loki", "filterByTraceID": True},
                    "serviceMap": {"datasourceUid": "slas-prometheus"},
                },
            },
        ],
    }
