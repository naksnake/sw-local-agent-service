# skills

| Path | What it is |
|---|---|
| `schema/skill.schema.json` | The skill file schema, rendered from `slas_skills.jsonschema.build_skill_schema()` (CLAUDE.md §6). A test keeps it in step; do not edit by hand. |
| `library/*.skill.yaml` | The skills that ship with the platform, rendered from `slas_skills.library`. |

Skills are data (INV-12): whitelisted verbs only, run with the importing user's capabilities.
Import them from the Skills page or with `slas skill import <file>`.
