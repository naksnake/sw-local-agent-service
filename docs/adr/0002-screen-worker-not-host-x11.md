# ADR-0002: A dedicated screen worker instead of the host's display

Status: accepted
Date: 2026-09-10

## Context
All three agents need GUI automation: an IDE or desktop for the Coding Agent, vendor tools
and BIOS setup for the Validation Agent, the station test application for the Factory
Agent. The chosen driver stack (PyAutoGUI + xdotool, CLAUDE.md §4.3) needs an X display. The
obvious shortcut, mounting the platform host's X11 socket or `/dev/input` into the container
that runs model-authored or skill-authored steps, would let a model or a skill drive the
platform host itself, which INV-4 forbids outright and §4.3 lists as rejected.

## Decision
GUI steps run in a dedicated **screen worker** service (Zone S, CLAUDE.md §4.1): one Xvfb
display per session, owned by the platform, exposed to the operator through x11vnc/noVNC so
they can watch and take over. The screen driver (`packages/slas-screen`) executes steps on
that display and takes a screenshot before and after every GUI step for the ticket. On a
physical factory station the same primitives are executed by the **station runner**, a
daemon on the station that receives signed step batches over mTLS; the platform receives
screenshots and results, never the station's display device. The orchestrator, the sandbox
and every other container are started without any host X socket, `/dev/input` or engine
socket (the two managers that hold the engine socket run no model or skill steps). Tests run
the driver against a screen fake; CI may use an Xvfb display, which is virtual.

## Consequences
Easier: INV-4 holds by construction; the operator can observe and intervene; screenshots
are journalled evidence (INV-6). Harder: one more service with its own memory (shm) budget;
a Coding desktop needs a defined transport between the network-less sandbox and the screen
worker's display, and window targeting needs a window manager and accessibility or OCR
support in the screen-worker image (recorded as open items in the 2026-09-10 review, to be
settled in P4 and P6).

## Invariants touched
INV-4 is upheld (no host display or input device reaches any container that runs model or
skill steps). INV-3 is upheld (the driver is deterministic; models never drive the display).
INV-6 is served (screenshot before and after every step). No invariant is relaxed.
