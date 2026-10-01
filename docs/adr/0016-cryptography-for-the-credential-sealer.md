# ADR-0016: `cryptography` seals Git credentials

Status: accepted
Date: 2026-09-17

Accepted under the project owner's round-2 instruction of 2026-09-17 ("Build All"): the
git broker must be healthy after `./install.sh --build`, and CLAUDE.md §5.7 requires the
stored credential to be AES-GCM encrypted by reference.

## Context
`slas_git.credentials.AesGcmSealer` was written against the `cryptography` package and
refused to construct while the package was unapproved; `FakeSealer` (an HMAC-keyed stream
for tests) stood in. The round-2 git-broker service builds the AES-GCM sealer by default
and, without the package, answered every credential route with a three-part 503 and
reported itself unhealthy. The standard library has no AEAD cipher, and writing one is
not an option this project takes.

## Decision
`packages/slas-git` depends on `cryptography==50.0.1` (pinned, INV-8). The broker's
default sealer is AES-256-GCM with a 96-bit random nonce per operation and the credential
reference plus owner as associated data, keyed from `SLAS_SECRET_KEY` through
`derive_key`. `FakeSealer` remains for tests only (`SLAS_SEALER=fake-for-tests` is logged
as a warning when a service starts with it). The prod profile keeps Vault KV as the other
`CredentialStore`.

## Consequences
- `uv.lock` gains `cryptography` and its `cffi`/`pycparser` wheels; the offline bundle
  carries them like every other dependency (INV-1 unchanged: nothing is fetched at runtime).
- A test now proves the AES-GCM sealer round-trips, refuses a wrong owner and a short key.
- The git-broker's `/health` reports `sealer: ok` on a quickstart host.

## Invariants touched
INV-8 (one more pinned dependency), INV-14 (the encrypted-by-reference store becomes real
on quickstart).
