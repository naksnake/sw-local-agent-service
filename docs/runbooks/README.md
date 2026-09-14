# docs/runbooks

| Runbook | Phase | What it covers |
|---|---|---|
| [`targets.md`](targets.md) | P8 | Registering a lab server as a target (credential references only), what the BMC and SSH users need, and arming or disarming power actions. |
| [`station-runner.md`](station-runner.md) | P10 | Installing the station runner on a Linux or Windows test station from the offline bundle, enrolling it with a one-time code, tuning window matching and timing for the login + BurnIn skill, screenshot retention, and watching or taking over through VNC. |

Operational runbooks (restore drill, upgrade, model swap). P11/P12.
| [`observability.md`](observability.md) | P11 | `slas status`, the metrics every service exposes, the six Grafana dashboards, the alert rules and the local alert channel, and how one trace id is followed from the WebUI to the executor. |
