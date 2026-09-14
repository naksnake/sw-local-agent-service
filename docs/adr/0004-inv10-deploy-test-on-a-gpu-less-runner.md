# ADR-0004: Proving INV-10 in CI on a GPU-less, egress-blocked runner

Status: proposed
Date: 2026-09-14

## Context
INV-10 reads: "`./install.sh` on a fresh GPU host reaches a working login page with no
manual steps. CI enforces it." Phase 1 must add `tests/deploy`. GitHub-hosted runners
have no GPU. The Phase 0 `egress-drop` job runs each step inside `docker run
--network none`, where no Docker daemon exists, so it can never run `docker compose`;
Docker-in-Docker is rejected (§4.3). Blocking egress host-wide on a runner severs the
runner's own connection to GitHub.

## Decision
- **The runner is the fresh host.** A `bundle` job (network allowed) builds
  `slas-bundle-<version>.tgz`: third-party images pulled by digest and retagged,
  first-party images built with `--network none`, every image `docker save`d, a manifest of
  image IDs, the installer and its packages, and a vendored Python
  (python-build-standalone, pinned url and sha256) so that no host package is needed.
- **A `deploy` job installs it as one ordinary user.** Everything that needs the network
  (download the bundle, pnpm install, Playwright's Chromium, a canary image) happens
  before the block. Then `tests/deploy/egress-drop.sh` applies iptables rules: a
  `DOCKER-USER` DROP for container traffic not destined to Docker's own ranges, with a
  dedicated counter for the edge network, and an owner-match chain for the test user that
  rejects everything except loopback and the Docker ranges. The block is proven before
  the install (a canary container and the test user both fail to reach the internet;
  containers still reach each other).
- **Two recorded deviations from the product command:** `--no-gpu` (GPU checks become
  warnings with the sentence "No GPU was found: models cannot load until one is added;
  everything else works.") and `--data-root /mnt/slas` (the runner's larger disk). A
  FakeHost test proves that without `--no-gpu` a GPU-less host still fails the preflight.
- **What the job asserts.** Exit 0; every service healthy; only edge publishes a port;
  the first printed URL answers over TLS verified with the exported root, never
  `--insecure`; the set of image IDs after install is a subset of before ∪ manifest ∪ lock
  and dockerd's journal shows no pull; the edge log has no ACME/OCSP/dial errors and the
  edge DROP counter is zero; a second `./install.sh` prints "Nothing changed." with every
  container's `StartedAt` unchanged. Later sessions extend the same job with sign-in, add a
  person, change a setting, and the full-flow Playwright spec.
- **The GPU path** is exercised on the reference GPU host as a release-checklist item,
  not in CI. INV-10's sentence gains "CI enforces it on a GPU-less runner with the two
  deviations recorded in ADR-0004".

## Alternatives considered
- A self-hosted GPU runner: exact, but a mandatory piece of infrastructure the project does
  not have yet; can be added later without changing the test.
- A nested VM on the runner (`/dev/kvm` is available on hosted Linux runners): the cleanest
  "fresh VM", one extra session; recorded as the fallback if the per-container block cannot
  be made to hold.
- Running compose inside the existing `egress-drop` containers: impossible without a
  daemon; DinD is rejected.

## Consequences
Easier: INV-10 is green on every pull request from the second Phase 1 session onward, and
every later session extends one job. Harder: iptables behaviour differs between backends
and must be printed and pre-checked in the job; Docker's DNS forwarder resolves from the
host namespace (recorded gap, mitigated by `--pull never` and the image-ID assertion).

## Invariants touched
INV-10 (enforced in CI with two named deviations; the GPU path checked before release),
INV-1 (nothing is pulled or fetched during install; proven by counters and image IDs),
INV-8 (images verified by ID against the lock in git).
