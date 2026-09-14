# Admin → Git hosts — copy (CLAUDE.md §5.7, §9)

The allowlist git-broker egresses to (`config/git-hosts.yaml`). Component:
`apps/webui/src/git/GitHostsAdmin.tsx`.

| Element | Text |
|---|---|
| Heading | Git hosts |
| Lede | git-broker reaches only these hosts. A remote on any other host is refused with the list shown here. Changes apply as soon as you save; nothing is restarted. |
| Host row | *gitlab.internal* · gitlab · https and ssh · ssh waits for a pinned host key · *note* |
| Add form | Name · Hostname · Kind: GitLab (opens merge requests) / Gitea (opens pull requests) / GitHub (opens pull requests; needs an ADR) / Plain Git (push only) · SSH host key (one known_hosts line; leave empty to allow https only) |
| Primary button | Allow host |
| Added | *gitlab.lab3.internal* is now an allowed Git host over *https and ssh*. It applies now; nothing restarted. |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Public host | *github.com* is a public host and needs an ADR before it can be allowed. | Reaching a public Git host is an exception to INV-1 (no external network dependency). | Write the ADR under docs/adr, then add the host. |
| Not added | The host wasn't added. | The api service didn't answer. | Try again; if it repeats, run `slas logs api` on the host. |
