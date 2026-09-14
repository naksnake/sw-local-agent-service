"""Screen driver (CLAUDE.md §5.2, ADR-0002): GUI steps as a deterministic executor.

`driver.ScreenDriver` enforces the rules whatever the backend: screenshot before and after
every step, targets by title, accessible name or image, at most N actions per second, a
deny-list of windows, and a hard stop when the focus changes unexpectedly.
`backend.FakeScreen` replays scripted windows for tests; `xdotool.XdotoolBackend` drives a
real Xvfb display inside the screen worker with argv only.
"""

__version__ = "0.0.1"
