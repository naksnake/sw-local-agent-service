# ADR-0018: Fetching model weights from the WebUI on a connected quickstart host

Status: accepted
Date: 2026-09-18

Accepted on the project owner's request of 2026-09-18: "in webui add ai model: let user
paste link then it will download and import."

## Context
INV-1 says the running platform has no external network dependency: weights arrive by
sneakernet in a signed bundle, and ADR-0014 opened one preparation window on a connected
quickstart host (`./install.sh --build --fetch-models` fetches images and weights before
the stack starts; the running platform still downloads nothing). CLAUDE.md §15 keeps the
neighbouring question as open decision (13).

The first installation is that connected quickstart host: one owner, Docker, `--build`. The
owner asked to add a model from the Models page by pasting a Hugging Face link, with the
download and the registry import done by the platform, and to change which model serves a
role from the same page instead of editing `Models/models.yaml` by hand. The first request
conflicts with INV-1 as written, because the download happens while the platform runs.
CLAUDE.md §0 says such a conflict is surfaced and recorded, never resolved silently; this
ADR is that record. The second request is what INV-9 already promises (model changes never
need a config edit or a restart) and needs no exception.

## Decision
- **One new service, `model-fetcher`, is the single component with outbound network
  access, on the quickstart profile only.** It joins `slas-backend` (to answer the api) and
  a new non-internal network `slas-egress` of which it is the sole member. It may reach
  only the model hub: `huggingface.co`, `cdn-lfs.huggingface.co`, `*.hf.co`, and the
  `HF_ENDPOINT` mirror when one is set; `SLAS_HUB_HOSTS` names the allowlist and the
  service refuses a link, an endpoint or a redirect outside it in three parts. It goes
  through `HTTPS_PROXY` when that is set. A token for a gated repository comes from
  `HF_TOKEN_FILE`, is sent as a header only, and is never logged, never in a URL and never
  returned.
- **It writes under `${SLAS_DATA_ROOT}/Models` and nothing else.** It holds no runtime
  socket, no credential store and no model context, and it starts nothing: after a fetch it
  writes `SHA256SUMS` and the manifest exactly as `scripts/fetch_models.py` does, then
  appends the model's entry to `Models/models.yaml` (read, validate with the model manager's
  registry loader, add, write atomically; an existing id is never overwritten; `roles` and
  `voters` are never touched). The model manager re-reads the file on its next reconcile
  tick and the Models page shows the card; the person gives the model a role there.
- **The fetch logic is one package, `packages/slas-fetch`.** `scripts/fetch_models.py`
  becomes a thin wrapper over it, so `./install.sh --fetch-models` on the preparation path
  and the service on the running platform download, resume and verify weights the same way
  (sha256 for LFS files, git blob ids for the rest, `.part` files that resume).
- **Roles from the Models page.** The model manager gains `PUT /v1/roles` (partial: only
  the roles and voters given change), which validates the ids, requires the weights to be
  present, rewrites `Models/models.yaml` atomically and reconciles. The api proxies it
  behind `model:manage`; the WebUI's Roles panel becomes editable.
- **The prod profile does not start the fetcher** and keeps the signed bundle path. The
  service sits behind the compose profile `fetch`; the installer adds `fetch` to
  `COMPOSE_PROFILES` on quickstart and never on prod, removes a leftover `model-fetcher`
  container on a prod install as it does for the parts that are off (ADR-0017), and the
  image lock lists the image for quickstart only.
- **Nothing else gains egress.** The model manager, the vLLM instances, the gateway and
  every other service keep the networks they had; `slas-inference` stays internal.

## Alternatives considered
- Keep `./install.sh --fetch-models` as the only way: honest to INV-1 as written, but it
  asks the owner to leave the WebUI, edit `config/model-sources.txt`, run the installer and
  edit `models.yaml` for every model, which §1.2 value 4 ("one page to manage models") and
  INV-9 argue against on a connected host.
- Give the model manager the egress instead of a new service: fewer containers, but the
  component that holds the runtime socket would also hold the only route out (INV-4 asks
  the opposite), and the prod profile could not turn the egress off without a second image.
- Let the api download: the api is behind the edge and owns sessions; a multi-hour download
  in its process would tie the two together and give the browser-facing service a route out.

## Consequences
Easier: a model is added from the page it is managed on, with progress and a sentence, and
the role change that used to need a file edit is a form. Harder: a quickstart host that runs
the fetcher is, by construction, not air-gapped while a fetch runs; the runbooks say so, the
Models page says what will happen before it happens, and an installation that must stay
air-gapped installs prod or leaves the `fetch` profile off. A model added this way carries
estimates (`vram_gib` from the file sizes with 25 % headroom, `context` from `config.json`)
that the person may correct in the registry.

## Invariants touched
INV-1 — relaxed for one service on the quickstart profile only, to the hub allowlist,
recorded here; the invariant stays as written for every other component and for prod.
INV-4 — held: the fetcher mounts no runtime socket and runs nothing model-authored.
INV-5 — held: the hub token is a file the fetcher alone reads; it never reaches a log, a URL
or a model context. INV-8 — held: the fetcher's image is pinned like the others and the
weights it fetches are checksummed against what the hub publishes. INV-9 — met more fully:
roles change from the Models page without a config edit or a restart.
