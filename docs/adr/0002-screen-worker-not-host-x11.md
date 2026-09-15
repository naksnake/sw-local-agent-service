# ADR-0002: A screen worker with its own virtual displays, never the host X11

Status: accepted
Date: 2026-09-11

## Context
All three agents need to operate a graphical interface at some point: the Coding Agent an
IDE or desktop, the Validation Agent a vendor tool or BIOS setup over KVM, the Factory
Agent a station's test application. The obvious shortcut — mounting the platform host's
X11 socket or `/dev/input` into the container that runs the GUI steps — would hand
model-authored and skill-authored steps control of the machine that runs the platform.
That breaks INV-4 ("Agents never touch the platform host") and makes every GUI step a
potential escape. It also ties GUI work to one physical display and makes tests need a
real screen.

## Decision
GUI control runs in **Zone S, the screen worker** (`services/screen-worker`), a gVisor
container that owns one Xvfb virtual display per session and exposes it to the operator
through x11vnc/noVNC so they can watch and take over. The screen driver
(`packages/slas-screen`, PyAutoGUI + xdotool behind our own interface) executes only
concrete steps sent by the orchestrator; it never sees a model or a credential. Every GUI
step takes a screenshot before and after, both attached to the ticket; targets are found by
window title, accessible name or template image, never bare coordinates unless a recipe
author supplies them; actions are rate-limited to 10 per second, a deny-list blocks
terminal emulators on the platform host and password managers, and the driver stops if the
focused window changes unexpectedly (CLAUDE.md §5.2).

On a physical test station the same primitives run in a small **station runner** daemon
that receives signed step batches over mTLS and returns screenshots and results; the
platform never receives the station's raw display or input devices.

No container that runs model-authored or skill-authored steps mounts a host X11 socket,
`/dev/input`, or a runtime socket. Docker-in-Docker and host display sharing are rejected
(CLAUDE.md §4.3). Tests run the driver against a **screen fake** that replays scripted
windows; a real Xvfb display appears only in the Phase 4 CI job that proves the worker.

## Consequences
Easier: GUI automation is testable without hardware; several sessions run at once on one
host; the operator can watch any session and take over; the same driver serves all three
agents and the station runner; the screenshot trail makes every GUI step reproducible.

Harder: one more container and network (`slas-screen`) to run; applications must be
installable inside the worker image or on the station; latency and rendering differ from a
physical display, so window matching needs tuning on real stations (Phase 10).

## Invariants touched
INV-4 is upheld by construction: the host display and input devices are never mounted
anywhere. INV-3 holds because the screen driver is a deterministic executor of concrete
steps; the model only produces the plan. INV-6 holds because every GUI step is journalled
with its before and after screenshots. No invariant is relaxed.
