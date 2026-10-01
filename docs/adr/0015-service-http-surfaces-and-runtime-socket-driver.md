# ADR-0015: Every service gets an HTTP surface; containers are driven over the runtime socket

Status: accepted
Date: 2026-09-17

Accepted by the project owner's instruction of 2026-09-17 ("Build All — let me finish
install.sh, then run anything"): after `./install.sh --build --fetch-models` the agents must be
startable from the WebUI. Round 1 (ADR-0005, ADR-0014) put the api and the WebUI on the wire;
every other service still ran the placeholder health runner and the agents existed only as
libraries with in-process fakes.

## Context
The kernel, the three agents, the executors, the gateway, the model manager, the sandbox
manager and the git broker are complete as libraries with tests against fakes, but nothing
connects them across container boundaries. The zone model (CLAUDE.md §4.1) requires those
boundaries: the sandbox manager and the model manager alone hold the runtime socket, the
executors alone reach the lab and factory networks, the git broker alone holds credentials,
the gateway alone talks to vLLM. The WebUI's agent pages run against fakes (`apps/webui/src/
*/api.ts`), with interfaces that already name every call the pages need.

Two things were missing: a way for the services to call each other, and a way for the two
socket holders to create containers. The `podman` CLI is not in the service images, and
mounting a runtime socket into anything else is forbidden (INV-4). CLAUDE.md §0.3 says to ask
before adding a dependency; the owner's instruction covers this round.

## Decision
1. **One HTTP stack for every service.** `packages/slas-http` gives every service a FastAPI
   app (`create_service_app`: `/health` with named checks, `/metrics`, the trace middleware,
   three-part errors), a uvicorn `run()`, and `ServiceClient`, an httpx client that forwards
   the trace and identity headers and turns a three-part body into a `ServiceError`. The
   dependencies are exactly the approved set of ADR-0005 (`fastapi==0.141.1`,
   `uvicorn==0.53.0`, `httpx==0.28.1`, `pydantic==2.13.5`), now shared by the services that
   need them; `pyyaml==6.0.3` (already used by the api) loads each service's configuration
   file. No dependency outside that set is added. Every service gains a console script and
   `serve` becomes its container command (`docs/api-contract-round-2.md` §1).
2. **Identity travels as headers, not tokens.** The api resolves the session and forwards
   `X-Slas-User`, `X-Slas-Display-Name` and `X-Slas-Capabilities` on the internal network;
   the receiving service runs the capability check where the action executes (CLAUDE.md
   §11). Nothing else — no session id, no password, no key — crosses a service boundary.
   In the prod profile the backend network is expected to carry mTLS between services;
   that is a later ADR and does not change the headers.
3. **Containers are created through the Engine API over the runtime socket.**
   `packages/slas-container` speaks the Docker Engine API, which Podman serves as its compat
   API, so one client starts vLLM instances (model manager) and sandboxes (sandbox manager)
   on either runtime. GPUs attach through `DeviceRequests` for the nvidia driver on Docker
   and CDI device names on Podman; gVisor is `HostConfig.Runtime: runsc`. The hardening the
   sandbox spec already enforces (no network, read-only rootfs, caps dropped, no socket or
   display mounts) is what the body carries. `SLAS_RUNTIME_SOCKET` names the socket; a Docker
   host sets it to `/var/run/docker.sock`.

   *Amendment, 2026-09-18.* The installer first preferred Podman's socket whenever it
   existed. On the first host (Docker and Podman both installed) that pointed the two
   managers at Podman while `docker compose` and `install.sh --build` had put every image in
   Docker's store, and every sandbox and vLLM container failed with Podman's "image not
   known". The rule is now: the socket of the engine that holds the images. Docker's socket
   wins whenever it exists, because the stack itself runs on `docker compose`; Podman's
   stays the compose default and serves a host without Docker. `slas doctor` looks in the
   same order. A value a person sets is still kept.
4. **The executors serve steps.** The kernel in the orchestrator hands each Validation or
   Factory step to `POST /v1/execute` on the executor's container; the executor performs it
   with the HAL or the station runner and returns the observation. INV-3 holds: the model
   never sees the executor, and the executor never sees the model.
5. **Kernel runs are threads inside the orchestrator**; the pages poll. A queue or a
   scheduler is not needed at this scale (open decision 1) and would be a separate ADR.

## Consequences
- `docs/api-contract-round-2.md` is the contract every service and the WebUI build against;
  a route table test per service keeps them in step, as round 1 does for the api.
- Seven services gain a real entrypoint; `python -m slas_observability.serve` stays as the
  fallback for images without one (screen-worker, local-search-api).
- The sandbox images must build on a connected host without the toolchain bundle
  (ADR-0014's path), each toolchain pinned to an upstream image digest or a distribution
  package version; the offline bundle path is unchanged.
- The vLLM image joins the image lock as a third-party image that compose does not start.
- Coverage, ruff, mypy and the egress-DROP build stay as they are; `uv.lock` is updated once
  for the whole round.

## Invariants touched
INV-1 (no new runtime egress; the build path of ADR-0014 pulls the vLLM image), INV-3
(the executor boundary becomes an HTTP boundary; the model stays outside it), INV-4 (the
socket stays with the two managers; the Engine API replaces a CLI, not a mount), INV-5 and
INV-14 (identity headers carry no secret; the broker's store is unchanged), INV-8 (every
pin is `==`; the vLLM image is pinned by digest), INV-9 (models and instances still change
through the Models page, no restart).
