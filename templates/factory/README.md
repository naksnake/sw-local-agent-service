# templates/factory

Factory test-loop templates (CLAUDE.md §10.3 PLAN). The shipped template is rendered from
`slas_factory_executor.templates` and a unit test keeps file and code in step; a line's own
templates land as `${SLAS_DATA_ROOT}/Factory/Templates/<id>.template.json` until a YAML reader
is an approved dependency.

| Template | Steps |
|---|---|
| `final-test-9-steps.yaml` | Lease the station and bind the unit · power the unit on through the fixture · log in and start BurnIn (the `station-login-burnin` skill, every GUI step screenshot before and after) · wait for "Test complete" · read the BurnIn result · read the sensors against the limits · check the event log is empty · back up the station state · decide (deterministic gate, then 3 of 3 voters for PASS). The executor appends the release, which only runs after a PASS. |

`$station`, `$unit_sn` and `$mes_ticket_no` are filled in when a job is planned. Secrets are
referenced (`secret_refs: {password: env:STATION_OPERATOR_PASSWORD}`) and resolved by the
executor at dispatch; the station runner types them and never journals them (INV-5).
