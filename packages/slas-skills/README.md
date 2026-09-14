# slas-skills

Skills are YAML recipes of whitelisted steps (CLAUDE.md §5.6, §6). They are data (INV-12):
validated against the schema, restricted to the primitive whitelist, run with the importing
user's capabilities, never able to escalate them. There is no `shell` primitive.

| Module | Stage |
|---|---|
| `primitives.py` | the whitelist: name, needed capability, risk class, arguments |
| `schema.py` | the recipe model and the parser from a mapping, with three-part errors naming the step |
| `jsonschema.py` | renders `skills/schema/skill.schema.json` from the same table |
| `templates.py` | `{{ name }}` binding and the one-comparison condition language |
| `importer.py` | IMPORT: schema → whitelist → capability check → risk; approval sentence for destructive steps |
| `compiler.py` | COMPILE: inputs bound, secrets → handles, loops unrolled (≤ 200 steps) → `Plan` |
| `runner.py` | RUN: every step journalled; screen steps through the screen driver; approvals enforced |
| `exporter.py` | EXPORT: YAML with secrets stripped and a content hash |
| `library.py` | the two shipped skills from §6.3, rendered to `skills/library/` |
| `state.py` | this installation's per-agent on/off record, `Skills/library/<id>.state.json` (ADR-0013): off by default, switches only for the author's agents, kept across a re-import that does not grow, never exported |
| `yamlout.py` | the YAML renderer; parsing arrives with the approved YAML dependency |
