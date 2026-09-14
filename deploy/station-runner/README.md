# The station-runner bundle

Everything a physical test station needs to join the platform, in one tarball, with no
network (CLAUDE.md §5.2, §10.3, INV-1, INV-4). Built on the platform host by
`deploy/station-runner/build-bundle.sh`; installed on the station by `install.sh` (Linux) or
`install.ps1` (Windows). The full procedure is `docs/runbooks/station-runner.md`.

| File | What it is |
|---|---|
| `wheels/` | The runner (`slas-station-runner`) with its workspace packages and every pinned third-party wheel, for Linux x86_64 and Windows amd64. `pip install --no-index` only. |
| `slas-ca.pem` | The platform CA. `enrol` pins it to verify the factory executor; the runner pins it to admit only the executor's client certificate. Public, not a secret. |
| `install.sh`, `slas-station-runner.service` | Linux: venv under `~/.slas-station-runner`, a systemd `--user` unit in the operator's graphical session. |
| `install.ps1` | Windows: venv under `%LOCALAPPDATA%\slas-station-runner`, a scheduled task at logon. GUI steps need PyAutoGUI, which is not an approved dependency yet; until then the runner on Windows refuses GUI steps with a sentence. |
| `BUNDLE` | Version, build time, CA fingerprint — read it aloud against Admin → Stations before enrolling. |

The bundle never carries a private key, a batch key or a credential. A station gets those by
redeeming a **one-time code** issued under Admin → Stations, over TLS to the executor, and
keeps them under its state directory with mode 0600. A single self-contained binary
(PyInstaller) waits on the dependency decision; the venv is the shipped form.
