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

Installing the runner on a real station, tuning window matching and timing, and the
screenshot retention policy are P10.
