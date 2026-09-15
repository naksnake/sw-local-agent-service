# slas-sop

Dual-language SOP renderer: SopModel → `sop.en.md` + `sop.zh-Hant.md` + `sop.json`, always
together (CLAUDE.md §5.5, INV-13).

| Module | What it does |
|---|---|
| `glossary.py` | `DEFAULT_GLOSSARY` → `docs/glossary.yaml` (rendered; a test keeps them in step), `Glossary.pinned_sentence()` for the translator, `terms_in()` for verification. |
| `protect.py` | Replaces ticket ids, BDFs, paths, filenames, versions, hex, `snake_case`, acronyms and numbers with `⟦n⟧` before translation and restores them after; `identifiers()` lists them. |
| `translate.py` | `Translator` protocol (production: the gateway's `planner` role), `translate_prose()` with placeholder and glossary verification and English fallback, `translate_sop()`, `FakeTranslator`. |
| `render.py` | `render_markdown()`, `render_sop()`; the Chinese rendering states whether prose was translated and which items stayed in English. |

zh-Hans (Simplified) is a Settings toggle per §5.5 and is not rendered yet; PDF via
WeasyPrint waits on the dependency decision.
