# services/git-broker

The only component that holds a Git credential or reaches a Git host (CLAUDE.md §5.7, INV-14).

| Module | What it does |
|---|---|
| `broker.py` | `GitBroker`: add/rotate/delete remotes, test connection, clone, pull, push a branch (validation gate → Consensus Router for agent diffs → push → merge request on GitLab/Gitea/GitHub), bundle export/import. Authorises with `slas_authz` capabilities, decrypts the credential for one call inside an `ExitStack`, runs `git` as argv with the hardening flags, writes one audit row (with the request's trace id). |
| `askpass.py` | The `GIT_ASKPASS` helper and `token_pipe()`: the token reaches git through an inherited pipe fd, never argv, env, URL or disk. |
| `sshkey.py` | `key_file()`: private key on the tmpfs key dir, 0600, `GIT_SSH_COMMAND` with `IdentitiesOnly`, `StrictHostKeyChecking=yes` and the pinned known_hosts, shredded in `finally`. |
| `runner.py` | `ProcessRunner` protocol (`LocalProcessRunner`, `FakeProcessRunner`) with `pass_fds`. |
| `service/settings.py` | `Settings.from_environ()`: data root, secrets dir, allowlist path, key dir, sealer, bind. |
| `service/state.py` | `HostsFile` (the allowlist read at start and written back by Admin → Git hosts) and `BrokerState`. |
| `service/app.py` | `create_app(broker=…, hosts=…, log=…)`, `create_app_from_settings()`, `build_broker()`, `build_sealer()`, `route_table()`. |
| `service/routes.py` | The routes of `docs/api-contract-round-2.md` §7; identity headers → capability checks → the broker → three-part errors. |
| `cli.py` | `slas-git-broker serve`, the container command. |

Engine code lives in `packages/slas-git`: hosts allowlist, credentials store and sealers,
remotes, validation gate, host APIs, bundles, audit, redaction.

## Serving

`slas-git-broker serve` reads the environment (`SLAS_DATA_ROOT`, `SLAS_SECRETS_DIR`,
`GIT_HOSTS_ALLOWLIST`, `SLAS_KEY_DIR`, `SLAS_ASKPASS_DIR`, `SLAS_SEALER`, `SLAS_CA_BUNDLE`,
`SLAS_GIT_PATH`, `SLAS_BIND`; defaults in `service/settings.py`), reads `SLAS_SECRET_KEY` from
`${SLAS_SECRETS_DIR}/secret_key`, and serves on `SLAS_BIND`. Under `${SLAS_DATA_ROOT}` it uses
`Coding/<user>/Projects/<slug>` and `Coding/<user>/Bundles/` (shared with the sandbox manager)
and its own `.git-broker/{credentials.json, remotes.json, audit.jsonl, bin/slas-askpass}`.
Both `Coding/` and `.git-broker/` must be mounted, and the allowlist file rw when Admin → Git
hosts is to write it; a read-only allowlist refuses in three parts and the operator edits
`config/git-hosts.yaml` on the host.

`GET /health` names three checks: `git` (on `SLAS_GIT_PATH`), `store` (`.git-broker/`
writable) and `sealer`. The AES-GCM sealer needs the `cryptography` package, which is not an
approved dependency yet; until it is, `SLAS_SEALER=aes-gcm` (the default) leaves the sealer
`missing`, the credential routes answer 503 in three parts, and projects, hosts and bundles
still work. `SLAS_SEALER=fake-for-tests` is obfuscation for tests, never for a deployment.
