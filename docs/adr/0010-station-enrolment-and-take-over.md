# ADR-0010: Station enrolment with one-time codes, per-station keys, and VNC relayed over mTLS

Status: proposed
Date: 2026-09-14

## Context
CLAUDE.md §5.2 says the station runner performs GUI primitives locally over mTLS with signed
step batches and that the operator watches through a VNC view and can take over; §10.3 says
a real station is P10; INV-4 forbids the platform from ever receiving a station's display
or input devices; INV-5 forbids credentials in a model context, argv, URL or log; INV-10
forbids manual setup for anything mandatory. Phase 9 shipped the runner with a fake and a
CA minted by the tests. P10 has to say how a physical station gets its identity, how the
operator watches, and who may do what.

## Decision
- **Enrolment by one-time code.** An administrator adds a station under Admin → Stations
  and issues a code (`XXXX-XXXX-XXXX`, alphabet without 0/O/1/I/L). The platform stores a
  salted SHA-256 of it; it lives 15 minutes, works once, and five wrong codes lock the
  station's enrolment until a new code is issued (`config/factory.yaml`). The station posts
  `{station, code, runner_url}` over TLS to the executor's enrolment endpoint, pinning the
  platform CA shipped in its bundle; the executor answers once with the station's
  certificate and key, the CA, the batch signing key and the station's configuration.
- **The platform CA** lives under `Factory/ca/` and is created by the factory executor at
  first start with `openssl` (argv only). It issues dual-purpose certificates (serverAuth
  and clientAuth, the runner host in the SAN) so one certificate serves both directions.
  The CA key never leaves the platform; issued keys are shredded after they are returned.
- **Per-station batch keys**, HMAC-SHA256 until `cryptography` is approved, kept at
  `Factory/keys/<station>.key` (0600) and referenced as `file:Factory/keys/<station>.key`
  through `slas_hal.credentials.LocalCredentialResolver`. Revoking a station deletes the
  key; the executor can then sign nothing for it.
- **Watch and take over.** The station runs its own VNC server bound to loopback; the
  runner relays it on `POST /vnc` to callers holding the executor's certificate, and
  `VncTunnel` on the executor side gives noVNC a loopback port. Control batches (`pause`,
  `resume`, `abort`, `status`) are signed like every other batch; the runner pauses only at
  a step boundary (`PausableScreen`), and an abort ends the step with
  `Stopped: <who> took over <station>.` Every control action is journalled with the
  operator's name.
- **Tuning is per station record, never per skill**: window matching mode, settle after
  each action, wait timeout scale, actions per second (`ScreenTuning` → `ScreenPolicy`).
- **Screenshot retention** is a setting (`slas_screen.retention.RetentionPolicy`): platform
  default in `config/factory.yaml`, override per station; applied to
  `Factory/Jobs/<ticket>/screens/` and by the runner to its own copies. The first, last and
  failure screenshots of a job are always kept.
- **Two capabilities** join ADR-0006's set: `factory:control` (watch a station and take it
  over; engineers hold it) and `factory:stations_manage` (add stations, issue codes, tune;
  administrators hold it). The role set is unchanged.
- **Packaging** is a venv from offline wheels with a systemd `--user` unit (Linux) or a
  logon task (Windows); a single binary waits on PyInstaller. GUI steps on Windows wait on
  PyAutoGUI; until then the runner there refuses GUI steps with a sentence.

## Consequences
- A station can be enrolled by reading a code aloud to a person standing at it; no file is
  copied by hand and no credential crosses the network in the clear.
- Revocation is immediate and local (delete a key file); rotation is "issue a new code".
- The VNC view is only as safe as the executor's certificate: it never leaves the executor
  container, and the relay refuses anyone else at the handshake.
- New config file `config/factory.yaml`; new page section Admin → Stations (a section of an
  existing page, not a new page).

## Invariants touched
INV-1 (bundle installs offline), INV-4 (display never leaves the station; relay carries
pixels, not devices), INV-5 (keys by reference, codes hashed), INV-7 (an abort is a human
act, journalled), INV-10 (CA created at first start), INV-12 (tuning outside the skill).
