# Git panel and Terminal — copy (CLAUDE.md §5.7, §9)

Per project, inside Coding: Status · Commit · History · Push/Pull · Bundle · Terminal.
Component: `apps/webui/src/git/GitPanel.tsx`. Commit and History call `git` inside the
sandbox through the sandbox-manager exec API; Push/Pull and Bundle call git-broker.

| Tab | Element | Text |
|---|---|---|
| header | Title | Git · *bmc* on *slas/T-coding-0001* |
| Status | Clean | Nothing to commit; the working tree matches the last commit. |
| Status | Changed files | *M* bmc/fan.py |
| Commit | Lede | *2* changed files will be committed on *slas/T-coding-0001* with your name. |
| Commit | Done | Committed *1a2b3c4d5e*: *Tune fan curve* |
| History | Row | *1a2b3c4d5e* Add fan control · agent · T-coding-0001 (agent commits carry the Slas-Agent trailer) |
| Push/Pull | No remote | You have no saved remote. Add one under Settings → Git remotes; pushing happens from here, never from the sandbox. |
| Push/Pull | Lede | Push *branch* to *remote*: the validation gate runs first, then the branch is pushed and a review request is opened where the host supports it. A human merges. |
| Push/Pull | Buttons | Push to *gitlab-firmware* · Pull |
| Push/Pull | Pushed | Pushed *slas/T-coding-0001* (*1a2b3c4d5e*) to *gitlab-firmware*. Opened a review request: *url* |
| Push/Pull | Gate line | ✓ Every change stays inside the project. · ✓ No hook files were added. · ✓ No secret found by *gitleaks*. · ✓ *3 of 3* approve the change. |
| Push/Pull | Refused | The push was refused by the validation gate. *1 of 8* checks failed: *built-in patterns found 1 secret-like value: settings.py:1 (gitlab_pat). Remove them and commit again.* |
| Push/Pull | Protected | Direct pushes to a protected branch need git:push_protected, which is off by default; push to a branch and open a merge request instead. |
| Push/Pull | SSH remote | Pushed … SSH remotes carry no API token, so open the review request on the host yourself. |
| Push/Pull | Pulled | Pulled *main* from *gitlab-firmware*; *bmc* is at *1a2b3c4d5e*. |
| Bundle | Lede | A bundle carries this repository's history as one file for another site. Nothing is merged on import. |
| Bundle | Exported | Bundle *bmc-20260914-090000.bundle*: *2* refs, *4,096* bytes, sha256 *…* |
| Bundle | Imported | Imported *2* branches under bundle/: *main, feature*. |
| Terminal | Lede | Runs inside your sandbox as the workspace user, with the same isolation as the agent. The session is recorded to the ticket with secrets redacted. |
| Terminal | `git push` | … Push happens from the Git panel, which uses your saved remote. |

The Terminal tab is line-based until xterm.js and the WebSocket carrying
`TerminalMessage` frames (`slas_sandbox_manager.terminal`) are approved and wired.
