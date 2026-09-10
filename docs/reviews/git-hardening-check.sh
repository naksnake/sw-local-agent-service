#!/bin/sh
# Reproduces the checks behind the git-broker hardening finding in the 2026-09-10 review.
# Simulates a sandbox that can write Projects/<slug>/.git/config, then runs the broker's git
# with the flags from CLAUDE.md §5.7. Needs only git. Safe: everything happens under $WORK.
set -u
WORK=${WORK:-$(mktemp -d)}
mkdir -p "$WORK" && cd "$WORK" || exit 1
export GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_CONFIG_GLOBAL=/dev/null
FULL="-c core.hooksPath=/var/empty -c core.fsmonitor=false -c credential.helper= -c core.sshCommand= -c include.path="
REST="-c core.hooksPath=/var/empty -c core.fsmonitor=false -c credential.helper= -c core.sshCommand="
commit() { git -c user.name=a -c user.email=a@slas.local commit -q "$@"; }
ran() { [ -f "$WORK/$1" ] && echo "RAN" || echo "did not run"; }

echo "git: $(git --version)"
echo
echo "1. The exact flag list from CLAUDE.md 5.7 (includes '-c include.path=')"
git init -q r1 && (cd r1 && git $FULL status >/dev/null 2>&1 && echo "   git accepted the flags" || echo "   git REFUSED to run: $(git $FULL status 2>&1 | head -1)")

echo
echo "2. Is a repo-level [include] neutralised by the remaining flags?"
printf '[user]\n\tname = INCLUDED\n' > evil.inc
(cd r1 && git config include.path "$WORK/evil.inc" && echo "   user.name resolved through the include: $(git $REST config user.name)")

echo
echo "3. Does '-c credential.helper=' stop a helper configured in .git/config?"
git init -q r3 && (cd r3 && git config credential.helper "!f() { echo x >> $WORK/m3; }; f" && printf 'protocol=https\nhost=example.invalid\n' | git $REST credential fill >/dev/null 2>&1; echo "   repo credential helper: $(ran m3)")

echo
echo "4. Does 'core.hooksPath=/var/empty' stop a pre-push hook?"
git init -q --bare remote.git
git init -q r4 && (cd r4 && commit --allow-empty -m init && mkdir -p .git/hooks && printf '#!/bin/sh\necho x >> %s\n' "$WORK/m4" > .git/hooks/pre-push && chmod +x .git/hooks/pre-push && git remote add origin "$WORK/remote.git" && git $REST push -q origin HEAD:refs/heads/b1 2>/dev/null; echo "   pre-push hook: $(ran m4)")

echo
echo "5. Does a repo-configured 'url.<base>.insteadOf' redirect the broker's push?"
git init -q --bare attacker.git
git init -q r5 && (cd r5 && commit --allow-empty -m init && git remote add origin https://gitlab.internal/firmware/bmc-tool.git && git config "url.$WORK/attacker.git.insteadOf" https://gitlab.internal/firmware/bmc-tool.git && git $REST push -q origin HEAD:refs/heads/stolen 2>/dev/null; echo "   origin is https://gitlab.internal/...; branches now in attacker.git: [$(git --git-dir="$WORK/attacker.git" branch --list | tr -d ' \n')]")

echo
echo "6. Do repo-configured filter/textconv commands run when the broker checks out or diffs (pull path)?"
git init -q r6 && (cd r6 && echo hello > f.txt && git add -A && commit -m c \
  && git config filter.evil.clean cat && git config filter.evil.smudge "sh -c 'echo x >> $WORK/m6a; cat'" \
  && git config diff.evil.textconv "sh -c 'echo x >> $WORK/m6b; cat'" && printf '* filter=evil\n* diff=evil\n' > .gitattributes && git add -A && commit -m attrs \
  && rm -f f.txt && git $REST checkout -q -- f.txt && echo "   smudge filter on checkout: $(ran m6a)" \
  && echo changed > f.txt && git $REST diff >/dev/null 2>&1; echo "   textconv on diff: $(ran m6b)")

echo
echo "7. Does '-c core.sshCommand=' suppress a repo core.sshCommand, and does GIT_SSH_COMMAND still win?"
git init -q r7 && (cd r7 && git config core.sshCommand "sh -c 'echo repo >> $WORK/m7; exit 1'" \
  && git $REST ls-remote git@example.invalid:x/y.git >/dev/null 2>&1; echo "   repo sshCommand under flags: $(ran m7)" \
  && GIT_SSH_COMMAND="sh -c 'echo env >> $WORK/m7b; exit 1'" git $REST ls-remote git@example.invalid:x/y.git >/dev/null 2>&1; echo "   GIT_SSH_COMMAND under flags: $(ran m7b)")

echo
echo "8. What happens when the broker's uid differs from the owner of Projects/<slug>/.git?"
git init -q r8 && (cd r8 && commit --allow-empty -m init) && if chown -R 1000:1000 r8 2>/dev/null; then (cd r8 && echo "   without safe.directory: $(git $REST status 2>&1 | head -1)" && echo "   with -c safe.directory=*: $(git $REST -c safe.directory='*' status 2>&1 | head -1)"); else echo "   (chown not permitted here; skipped)"; fi

echo
echo "9. Does a fresh broker-side clone of the hostile repo run anything?"
rm -f m6a; git $REST clone -q "$WORK/r6" r9 2>/dev/null; echo "   smudge on clone: $(ran m6a)  (filter definitions live in .git/config, which a clone does not inherit)"
echo
echo "work dir: $WORK"
