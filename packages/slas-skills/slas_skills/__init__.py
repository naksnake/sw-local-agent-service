"""Skills: import → compile → run → export (CLAUDE.md §5.6, §6, INV-12).

A skill is data. `importer.import_skill` validates it against the model and the primitive
whitelist, checks the importing user's capabilities and classifies its risk;
`compiler.compile_skill` binds inputs, hands out secret handles and unrolls bounded loops
into a flat Plan; `runner.SkillRunner` performs it step by step with the journal and the
screen driver; `exporter.export_skill` writes it back with secrets stripped and a hash.
`skills/schema/skill.schema.json` is rendered by `jsonschema.render_skill_schema()`.
"""

__version__ = "0.0.1"
