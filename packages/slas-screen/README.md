# slas-screen

The screen driver: the deterministic executor of GUI steps (CLAUDE.md §5.2, INV-3, ADR-0002).

| Module | Owns |
|---|---|
| `driver.py` | `ScreenDriver`: focus, click, type, key, scroll, wait_for, screenshot, assert_visible; screenshot before/after; rate limit; deny-list; hard stop on focus change |
| `policy.py` | `ScreenPolicy`: actions per second, denied window patterns, coordinate policy |
| `backend.py` | `ScreenBackend` protocol and `FakeScreen` (scripted windows, texts, focus theft) |
| `xdotool.py` | `XdotoolBackend`: argv only, text over stdin, screenshots via ImageMagick `import` |
| `model.py` | `Window`, `Point`, `ScreenResult`, `ScreenStopError` |

Text, image and accessible-name search on a real display need PyAutoGUI or an AT-SPI
bridge; the xdotool backend returns "not found" for those until that dependency is approved,
so a recipe fails loudly instead of clicking blind.
