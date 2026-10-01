# services/model-fetcher

The only component with outbound network access, on the quickstart profile only (ADR-0018;
CLAUDE.md §4.1 zone F). Behind "Add a model" on the Models page: the person pastes a Hugging
Face link, the fetcher downloads the weights into `${SLAS_DATA_ROOT}/Models/<id>/`, verifies
every file against what the hub publishes, writes `SHA256SUMS` and `manifest.json` exactly as
`scripts/fetch_models.py` does, and appends the model's entry to `Models/models.yaml`. The
model manager re-reads that file on its next tick; the person gives the model a role on the
same page. The fetcher starts nothing and holds no runtime socket, credential store or model
context.

| Module | Owns |
|---|---|
| `links.py` | the accepted link forms (`https://huggingface.co/<owner>/<repo>[/tree/<rev>|/commit/<sha>]`, `hf.co/…`, `<owner>/<repo>[@rev]`), the derived registry id, display name, family and quantisation, the `context` read from `config.json`, the `vram_gib` estimate |
| `registry_import.py` | append one entry to `Models/models.yaml`: read, validate with the model manager's loader, add, write atomically; never an existing id, never `roles` or `voters` |
| `fetcher.py` | `FetchRecord` (the wire shape) and `FetchManager`: one background thread per fetch, progress from `slas_fetch.fetch_planned`, cancel between files, the sentences |
| `service/` | `settings.py` (environment), `routes.py` (contract §3b), `app.py` (`create_app`, every collaborator injectable) |
| `cli.py` | `slas-model-fetcher serve` |

Environment (defaults): `SLAS_MODELS_DIR` (`/data/Models`; `models.yaml` lives there),
`SLAS_HUB_HOSTS` (`huggingface.co,cdn-lfs.huggingface.co,*.hf.co`; the `HF_ENDPOINT` host is
always allowed too), `HF_ENDPOINT` (`https://huggingface.co`), `HTTPS_PROXY` (honoured by
urllib), `HF_TOKEN_FILE` (`/run/secrets/hf_token`; empty or absent means no token; sent as a
header only, never logged), `SLAS_BIND` (`0.0.0.0:8000`).

Health: `{"models_dir": "ok" | "unwritable", "hub": "not checked"}` — the hub is never called
from a health probe.
