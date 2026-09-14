# services/git-broker

The only component that holds a Git credential or reaches a Git host (CLAUDE.md §5.7, INV-14).

| Module | What it does |
|---|---|
| `broker.py` | `GitBroker`: add/rotate/delete remotes, test connection, clone, pull, push a branch (validation gate → Consensus Router for agent diffs → push → merge request on GitLab/Gitea/GitHub), bundle export/import. Authorises with `slas_authz` capabilities, decrypts the credential for one call inside an `ExitStack`, runs `git` as argv with the hardening flags, writes one audit row. |
| `askpass.py` | The `GIT_ASKPASS` helper and `token_pipe()`: the token reaches git through an inherited pipe fd, never argv, env, URL or disk. |
| `sshkey.py` | `key_file()`: private key on the tmpfs key dir, 0600, `GIT_SSH_COMMAND` with `IdentitiesOnly`, `StrictHostKeyChecking=yes` and the pinned known_hosts, shredded in `finally`. |
| `runner.py` | `ProcessRunner` protocol (`LocalProcessRunner`, `FakeProcessRunner`) with `pass_fds`. |

Engine code lives in `packages/slas-git`: hosts allowlist, credentials store and sealers,
remotes, validation gate, host APIs, bundles, audit, redaction. The HTTP surface of the
service and the compose entry wait on the dependency decisions; the AES-GCM sealer needs
the `cryptography` package to be approved.
