# docs/runbooks

| Runbook | Phase | What it covers |
|---|---|---|
| [`targets.md`](targets.md) | P8 | Registering a lab server as a target (credential references only), what the BMC and SSH users need, and arming or disarming power actions. |
| [`station-runner.md`](station-runner.md) | P10 | Installing the station runner on a Linux or Windows test station from the offline bundle, enrolling it with a one-time code, tuning window matching and timing for the login + BurnIn skill, screenshot retention, and watching or taking over through VNC. |

Operational runbooks (restore drill, upgrade, model swap). P11/P12.
| [`observability.md`](observability.md) | P11 | `slas status`, the metrics every service exposes, the six Grafana dashboards, the alert rules and the local alert channel, and how one trace id is followed from the WebUI to the executor. |
| [`prod-profile.md`](prod-profile.md) | P12 | Installing and operating the prod profile: the signed bundle or Harbor, Vault (AppRole per service, where secrets live, rotation), Keycloak beside built-in sign-in, the Kata/Firecracker sandbox tier, backups under object lock, Loki and Tempo. |
| [`restore-drill.md`](restore-drill.md) | P12 | The restore drill: pick a point in time, run `slas backup drill`, check by hand, and the RTO table every drill adds a row to. |
| [`deploy-hgx-b300.md`](deploy-hgx-b300.md) | — | Deploying to an HGX B300 NVL8 host: what runs today and what waits on a dependency decision, host preparation, bringing weights, the bundle build and install, the eight-GPU model layout, and daily use of the WebUI and CLI. |
