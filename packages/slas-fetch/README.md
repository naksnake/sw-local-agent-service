# slas-fetch

The one implementation of "get model weights from the hub and prove they are whole", shared
by the preparation-time script and the running platform's fetcher (CLAUDE.md §7; ADR-0014,
ADR-0018). Standard library only.

| Module | Owns |
|---|---|
| `fetch.py` | `Source` (`<path> <owner/repo> [revision]`), `Hub` (the hub's tree, revision and resolve routes; resumable downloads with a growing pause on a flaky link; retries never on an HTTP answer), `plan_model` (what is missing, what resumes, what is redundant), `fetch_planned` (download, verify sha256 or git blob id, write `SHA256SUMS`, report `Progress`, stop on `cancel()`), `write_manifest` / `merge_manifest`, the offline `verify`, and the hub host allowlist (`host_allowed`, `allowlisted_opener`: a redirect outside the allowlist is refused in three parts) |
| `cli.py` | `main()`: the `fetch`, `verify` and `merge-manifest` commands `scripts/fetch_models.py` exposes |

Who uses it:

- `scripts/fetch_models.py` — a thin wrapper; `./install.sh --fetch-models` on a connected
  host before the stack starts. It adds `packages/slas-fetch` to `sys.path` when the package
  is not installed, so the bare host needs no virtualenv.
- `services/model-fetcher` — the quickstart-only service behind "Add a model" on the Models
  page: one `fetch_planned` per pasted link, progress into a fetch record, then the registry
  import.

Every error is a `FetchError` with `what_happened`, `likely_cause`, `what_to_do`.
