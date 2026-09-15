# slas-git

Git engine shared by the workspace tooling and the broker (CLAUDE.md §5.7).

| Module | What it does |
|---|---|
| `workspace.py` | Method 2: `GitWorkspace` runs `git` as argv through a `GitExec` (the sandbox exec API in production, `LocalGitExec` for tests) with the hardening flags on every call — `core.hooksPath=/var/empty`, no credential helper, no fsmonitor, no include, no SSH command, `protocol.allow=never`. Init with the user's identity, branch, commit with `Slas-Agent`/`Slas-Ticket` trailers, log with parsed trailers, diff, status. `PUSH_EXPLANATION` is the sentence shown when `git push` fails in the sandbox. |

Method 1 (`git-broker`: encrypted credentials by reference, `GIT_ASKPASS`, tmpfs SSH keys,
host allowlist, validation gate, push + PR/MR, bundles) is the next P6 session.
