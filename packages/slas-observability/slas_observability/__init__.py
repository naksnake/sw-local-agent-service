"""slas_observability: the metrics every service exposes, one trace id from the WebUI to the
executor, structured JSON events, the local alert channel, and the Prometheus, Alertmanager
and Grafana files rendered from code (CLAUDE.md §8.2, P11).

Standard library only. Every metric name lives in `metrics.CATALOGUE`; the dashboards and
alert rules are tested against it so a renamed metric breaks the build, not a dashboard.
"""

from slas_observability import metrics, tracing

__all__ = ["metrics", "tracing"]
