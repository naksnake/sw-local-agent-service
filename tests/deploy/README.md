# tests/deploy

Deployment tests (CLAUDE.md §11): in P0 they run `install.sh` against a faked host through
PATH shims. From P1 they install on a fresh VM under egress-DROP and assert the login page
loads (INV-10).
