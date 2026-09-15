# Settings → Git remotes — copy (CLAUDE.md §5.7, §9)

Paste-only credential fields, never echoed back; after save only the fingerprint is shown;
Test connection runs `ls-remote` inside git-broker. Component:
`apps/webui/src/git/GitRemotesSettings.tsx`.

| Element | Text |
|---|---|
| Heading | Git remotes |
| Lede | Repositories you can push to and pull from. The credential is stored encrypted and used only inside git-broker; the sandbox never holds it, and this page never shows it again. |
| Empty | You have no remote yet. Add one below; a deploy key or a project token is better than a personal credential. |
| Remote row | *gitlab-firmware* · *https://gitlab.internal/firmware/bmc.git* · token *…p6Bd* · default branch *main* · last used *…* / never used |
| Row buttons | Test connection · Rotate · Delete |
| Rotate field | New token (paste; it is never shown again) → Save new credential |
| Add form | Name · Address · Credential: Token (https) / SSH private key (ssh) · Token — paste only; it is stored encrypted and never shown again |
| Advice | Prefer a deploy key or a project token over a personal credential; it can be revoked without touching your account. |
| Primary button | Save remote · while waiting: Saving… |
| Saved | Saved *gitlab-firmware*. The token is stored encrypted; only its fingerprint *…p6Bd* is shown from now on. |
| Tested | Connected to *gitlab-firmware*: *3* branches, including *main*. |
| Test failed | *gitlab-firmware* did not accept the credential (*…p6Bd*). Rotate it under Settings → Git remotes; the value stored may be wrong or expired. |
| Rotated | Rotated *gitlab-firmware*; the new fingerprint is *…1234*. |
| Deleted | Removed *gitlab-firmware*; its credential was deleted with it. |

| Case | What happened | Likely cause | What to do |
|---|---|---|---|
| Host not allowed | *github.com* is not an allowed Git host. Allowed: *gitlab.internal, gitea.internal*. | git-broker reaches only the hosts an administrator listed under Admin → Git hosts (config/git-hosts.yaml); a public host needs an ADR first. | Use a listed host, or ask an administrator to add this one. |
| Token in address | The address carries a credential. | Tokens never go in a URL; the broker injects them at dispatch. | Remove the user:token@ part and paste the token in its own field. |
| Not a token | The pasted value does not look like a token. | A token is one line of at least 8 characters; a private key is not a token. | Paste the token again, or choose SSH key. |
| Key with https | An SSH key goes with an ssh address. | Keys are used with ssh://git@host/… or git@host:… addresses. | Use the ssh address of the repository, or choose Token. |
| No pinned host key | *gitlab.internal* has no pinned SSH host key. | SSH is only used with StrictHostKeyChecking against a key an administrator pinned. | Ask an administrator to add the host key under Admin → Git hosts, or use https. |
| Not saved | The remote wasn't saved. | The api service didn't answer. | Try again; if it repeats, run `slas logs api` on the host. |
