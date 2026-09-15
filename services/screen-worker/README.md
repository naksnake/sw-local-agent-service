# services/screen-worker

Zone S (CLAUDE.md §4.1, §5.2, ADR-0002): one Xvfb display per session, x11vnc bound to
localhost, noVNC for the operator to watch and take over. The screen driver runs here
against the platform's own display; no host X11 socket or `/dev/input` is ever mounted
(INV-4).

| File | Owns |
|---|---|
| `slas_screen_worker/session.py` | `DisplaySession` argv for Xvfb, x11vnc and websockify; `SessionManager` allocating up to `DISPLAYS_PER_WORKER` displays through a `ProcessRunner` (fake in tests) |
| `Dockerfile` | Debian slim pinned by digest with xvfb, x11vnc, novnc, websockify, xdotool, imagemagick; non-root user |
| `entrypoint.sh` | starts one session from the environment for image smoke tests |

The orchestrator-facing session API arrives with the api dependencies (ADR-0005). The
Phase 4 CI job that runs a GUI skill on a real Xvfb display needs this image built.
