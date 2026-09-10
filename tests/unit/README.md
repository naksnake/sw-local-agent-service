# tests/unit

Unit tests for every workspace member, run with `uv run pytest`. They never touch real
hardware, a display, a live model or the network (CLAUDE.md §11); hosts are faked through
`slas_cli.fakes.FakeHostProbe` and the like.
