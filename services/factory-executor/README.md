# services/factory-executor

The deterministic loop that drives test stations through the station runner (CLAUDE.md §4.1
factory zone, §10.3, INV-3, INV-11). Sole member of the factory network. Zero LLM in the loop:
the kernel hands it plan steps, it performs them on the station and hands back observations
with screenshots, findings, votes and exports.

| Module | What it does |
|---|---|
| `primitives.py` | The Factory plan verbs (`lease_station`, `station_command`, `skill`, `wait_for_screen`, `read_result`, `read_sensors`, `check_event_log`, `verdict`, `backup_station`, `release_station`, and the destructive `station_config_change`); renders `plans/primitives/factory.yaml` and `plans/schema/factory-plan.schema.json`. |
| `templates.py` | Test-loop templates: the shipped `final-test-9-steps` (rendered to `templates/factory/`), a line's own `Factory/Templates/<id>.template.json`, and `compile_template` → a plan with the lease first and the release last. |
| `mes.py` | The MES adapter interface (`poll`, `report`), the file-drop implementation under `Factory/MES/{inbox,processing,done,rejected,outbox}`, and a fake. |
| `backup.py` | Station state → `Backups/stations/<station>/<ticket>/` with a manifest of hashes, attached as a `backup` export. |
| `executor.py` | `FactoryExecutor`: signs each batch for the station's runner, stores the screenshots that come back under `Factory/Jobs/<ticket>/screens/`, keeps the test-step map in `state.json`, and decides: the deterministic gate (result PASS, sensors within limits, event log empty), then `factory_pass` to the Consensus Router. PASS needs 3 of 3; a gate failure is FAIL and a split vote is the line lead's call — in both cases the step fails, so the kernel stops before the release: the unit stays on, the station stays leased, and the finding becomes the draft ticket for the line lead. `decide()` records the line lead's decision and releases the station. |

Everything runs and is tested against `slas_station_runner.fakes.FakeStation`; installing
the runner on a real station is P10.
