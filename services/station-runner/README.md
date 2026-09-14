# services/station-runner

The daemon on a physical test station (CLAUDE.md §5.2, §10.3, INV-4). The platform never
receives the station's display; it sends **signed step batches over mTLS** and receives
results and screenshots.

| Module | What it does |
|---|---|
| `protocol.py` | `StepBatch` (one compiled skill, one allowlisted command, or a state request) signed as canonical JSON with a per-station key (HMAC-SHA256 by reference; Ed25519 once `cryptography` is approved). `verify_batch` checks station, key, signature, expiry and replay. `BatchResult` carries the skill run, the command result or the state snapshot, plus screenshots as PNG bytes. |
| `runner.py` | `StationRunner`: performs a verified batch — GUI steps through the local `ScreenDriver` (screenshot before and after), `run` steps and commands only from the station's allowlist, never a shell; secret handles are substituted at type time and the journal masks them. `collect_state()` gathers config files, recent logs and application versions for the backup. |
| `server.py` | mTLS with the standard library: the server requires a client certificate signed by the platform CA; `MtlsRunnerClient` presents the executor's certificate and pins the CA. `InProcessRunnerClient` skips the network for tests. |
| `fakes.py` | `FakeStation`: a scripted Login → BurnIn screen and a scripted shell (`fixture-ctl`, `burnin-ctl`, `sensors-ctl`, `evlog`, `station-ctl`) with one planted failure at a time. |
| `enrol.py` | The station side of enrolment (P10): one call with the one-time code from Admin → Stations, over TLS with the bundle's CA pinned, writes `ca.pem`, `client.pem`, `client.key`, `batch.key`, `config.json`, `runner.json` under the state directory, mode 0600. |
| `control.py` | Watch and take over (P10): `Controller` (pause · resume · abort · status) and `PausableScreen`, which asks the controller before every GUI primitive so a skill stops at the next step boundary and, on abort, ends with `Stopped: <who> took over <station>.` |
| `cli.py` | `slas-station-runner enrol \| serve \| doctor \| windows \| prune \| show`. `serve` starts the mTLS server, the local VNC server when the record enables it, and relays VNC over `/vnc` to callers with the executor's certificate. Linux drives the display with xdotool; Windows waits on the PyAutoGUI decision and refuses GUI steps with a sentence. |

`runner.py` also carries `ScreenTuning` (window matching mode, settle, timeout scale, rate),
`VncSettings` and the screenshot `RetentionPolicy` per station; the runner prunes its own
screenshots after every skill batch. `server.py` adds `relay`, `POST /vnc` and `VncTunnel`
(the executor side: a loopback port for noVNC, each connection one mTLS stream to the
runner). Packaging for a station lives in `deploy/station-runner/`; the procedure is
`docs/runbooks/station-runner.md`.
