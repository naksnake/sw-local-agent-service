# Installing the station runner on a test station

The Factory Agent drives a physical test station through a small daemon on the station, the
**station runner** (CLAUDE.md §5.2, §10.3, INV-4). The platform never receives the station's
display or its input devices; it sends signed step batches over mTLS and gets back results
and screenshots. This runbook takes one station from a bare OS to a unit going through the
loop with the operator watching, in six steps. Every command prints sentences; nothing here
needs a config edit or a restart on the platform.

## 0 · What you need

| On the platform host | On the station |
|---|---|
| The factory executor running once, so the platform CA exists under `${SLAS_DATA_ROOT}/Factory/ca/` | Python 3.12 (64-bit) from offline media |
| The station-runner bundle: `deploy/station-runner/build-bundle.sh` → `dist/slas-station-runner-<version>.tgz` | Linux: `xdotool`, ImageMagick (`import`), `x11vnc`, and the vendor test application in the operator's graphical session |
| An administrator with `factory:stations_manage` (Admin → Stations) | Windows: the vendor test application; TightVNC as a service if the operator should watch. GUI steps on Windows wait on the PyAutoGUI decision (see §7) |
| A route from the factory network to the station's runner port (8443 by default) and from the station to the executor's enrolment port (8444) | A fixed hostname or IP the executor can reach |

The bundle carries no secret: wheels, the installers and the platform CA (`slas-ca.pem`).
Read `BUNDLE` on the station and compare the CA fingerprint with the one shown under
Admin → Stations before enrolling.

## 1 · Add the station and issue its code (platform)

Admin → Stations → **Add station**: name (`station-07`), description. Then **Issue code**.
The page shows one sentence:

> Enter this code on station-07 within 15 minutes: K7PM-4RQD-XN2H. It works once; issuing a new code cancels it.

Codes use letters and digits that read aloud safely (no 0/O/1/I/L). Five wrong codes lock
the station's enrolment until a new code is issued. Codes are stored hashed.

Set the station's allowed programs (the vendor CLI tools a `run` step or a command batch may
start, argv only), and under **Tune** the window matching, timing, screenshot retention and
whether the operator may watch over VNC. Defaults are sane; tune after the first run (§5).

## 2 · Install the bundle (station)

Linux:

```
tar xzf slas-station-runner-<version>.tgz && cd slas-station-runner-<version>
./install.sh
```

Windows (PowerShell, as the operator user):

```
tar -xzf slas-station-runner-<version>.tgz; cd slas-station-runner-<version>
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Both create a virtual environment from the bundled wheels (`pip install --no-index`), copy
`slas-ca.pem` next to it, and register the runner to start with the operator's graphical
session (systemd `--user` unit on Linux, a scheduled task at logon on Windows). Nothing is
installed system-wide and nothing runs as root.

## 3 · Enrol (station)

```
slas-station-runner enrol --platform https://<factory-executor>:8444 \
    --station station-07 --code K7PM-4RQD-XN2H \
    --runner-url https://station-07.factory.internal:8443 \
    --ca ~/.slas-station-runner/slas-ca.pem
```

The station posts the code over TLS (it pins the bundle's CA; it has no certificate of its
own yet). The executor checks the code, mints the station's certificate with the platform
CA, creates the station's batch signing key, and returns them once with the station's
configuration. They land under `~/.slas-station-runner/` (`%LOCALAPPDATA%\slas-station-runner\`
on Windows), mode 0600:

| File | What it is |
|---|---|
| `client.pem`, `client.key` | The station's mTLS identity, valid for the runner URL's host and the station name |
| `ca.pem` | The platform CA; only the executor's certificate is admitted |
| `batch.key` | The per-station key every batch must be signed with (`Factory/keys/<station>.key` on the platform) |
| `config.json` | The station record: allowed programs, tuning, retention, VNC |
| `runner.json` | Where the platform is, this runner's URL and bind address, the certificate fingerprint |

The sentence printed ends with the certificate fingerprint; Admin → Stations shows the same
one. A wrong code answers in three parts (`The code for station-07 is not right. 4 attempts
left before enrolment locks. Check the code with the administrator who issued it and try
again.`) and writes nothing.

## 4 · Check and start (station)

```
slas-station-runner doctor
```

`doctor` checks Python, the six files and their modes, the GUI backend (xdotool + DISPLAY on
Linux), ImageMagick, the VNC server, and prints the tuning and retention sentences. It ends
with `Summary: the station is ready to serve.` or the count of problems, each with what to
do. Then:

```
systemctl --user start slas-station-runner      # Linux
Start-ScheduledTask 'SLAS Station Runner'       # Windows
```

or `slas-station-runner serve` by hand in the graphical session. The runner listens on the
bind address from enrolment (default `0.0.0.0:8443`), refuses any client without a
certificate signed by the platform CA at the handshake, and refuses any batch not signed
with its key. On the platform, Admin → Stations shows the station as enrolled; a first job
from the Factory page reaches it.

## 5 · Tune window matching and timing for `station-login-burnin`

The shipped skill waits for a window called `BurnIn v3.2` and clicks `Start test`. Real
vendor applications differ in title, and slow stations need time between actions. Tune the
**station record**, never the skill (skills are data shared across installations):

1. With the vendor application open on the station, list what the runner sees:
   ```
   slas-station-runner windows
   ```
   It prints every window title and class and the current matching mode, for example
   `'BurnIn v3.2.17 — station-07' class 'burnin' (focused)`.
2. Pick a matching mode under Admin → Stations → Tune:
   *contains* (default: the recipe's title appears anywhere), *prefix*, *exact*, or
   *regular expression* for titles that carry a version or a serial number.
3. If the application lags behind the input, raise **Settle after each action** (0.2–0.5 s
   is typical); if BurnIn takes longer to come up than the recipe's `timeout_s`, raise
   **Wait timeouts ×** rather than editing the recipe. **Actions per second** stays at 10 or
   below (§5.2 rate limit).
4. Save, then on the station re-enrol or restart the runner to pick the change up
   (`doctor` prints the active tuning sentence). Run one job from the Factory page and
   read the login step's screenshots in the strip: before and after each action.

A real station has not been named yet (the task's `<alias>` placeholder), so the tuning
values in the repository are the defaults and the fake station's. Record the values you end
up with in the station's description so the next line can start from them.

## 6 · Watch and take over

When the station record has VNC on, the runner starts (Linux) or expects (Windows) a VNC
server bound to the station's loopback and relays it over its mTLS channel: the VNC bytes
never cross the factory network in the clear and never reach a machine without the
executor's certificate. On the Factory page a running job shows **Watch station**, then:

| Button | What happens |
|---|---|
| Watch station | The page shows where to attach the VNC view (`vnc://127.0.0.1:<port>`, relayed to the station). Read-only until you take over. |
| Take over | The runner pauses at the next step boundary and sends no input; the sentence says who took over. You drive the station through the VNC view. |
| Resume | The runner continues from the step it stopped at. |
| Abort | The current step stops with `Stopped: <you> took over station-07.`; the job ends as Failed, the unit stays on, the station is held. |

Every control action is journalled on the job with the operator's name. A station whose
record has VNC off answers with a sentence instead of a dead link.

## 7 · Screenshot retention

`config/factory.yaml` sets the platform default (30 days; 180 days for failed or held jobs;
at most 400 screenshots per job) and Admin → Stations → Tune sets it per station. The
platform prunes `Factory/Jobs/<ticket>/screens/`; the runner prunes its own copies after
every skill batch and on `slas-station-runner prune`. The first and last screenshot of a
job and every failure screenshot are always kept.

## 8 · Waiting on decisions

| Blocked | Why | Until then |
|---|---|---|
| GUI steps on Windows | PyAutoGUI is not an approved dependency | The runner on Windows performs commands, state and control batches and refuses GUI steps with a sentence; `doctor` says so |
| A single self-contained binary | PyInstaller is not an approved dependency | The venv installer (`install.sh` / `install.ps1`) is the shipped form |
| Ed25519 batch signatures | `cryptography` is not an approved dependency | HMAC-SHA256 with a per-station key kept on the platform by reference |
| Tuning against the real station | No station named yet | Steps in §5 with the defaults |

## Revoking a station

Admin → Stations → **Revoke**: the station's batch key is deleted on the platform, so the
executor can sign nothing for it and schedules no job to it; the record forgets the runner
URL and the fingerprint. Issue a new code to enrol it again (a new certificate and a new
key). Certificates expire after 397 days. **Remove** deletes the record of a station that is
not enrolled.
