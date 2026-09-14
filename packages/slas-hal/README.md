# slas-hal

Hardware abstraction: the one interface every physical action goes through (CLAUDE.md §5.2,
INV-3, INV-5). P7 ships the models, the Redfish parsers, the plan primitives and the fakes;
the real Redfish/IPMI/SSH/PDU drivers arrive in P8 against one target server.

| Module | What it does |
|---|---|
| `model.py` | `Snapshot` (power, `Inventory` with `PcieDevice` width AND speed and firmware, `Counters` AER/EDAC/MCE/Xid, `SelEntry`); what a baseline and every VERIFY compare. |
| `redfish.py` | Redfish payloads → models. A malformed or truncated answer is a three-part `HalError`, never a half-filled model. |
| `hal.py` | The `Hal` protocol: power, SEL, inventory, counters, snapshot, console, fence marker, wait for the OS, SSH argv. Targets are opaque references; drivers resolve credentials at dispatch. |
| `primitives.py` | The Validation plan verbs, their arguments and risk; renders `plans/schema/plan.schema.json` and `plans/primitives/validation.yaml` (a test keeps them in step). |
| `fakes/bmc.py` | `FakeTarget` plays back the recorded fixtures under `fakes/fixtures/` (ugly ones included: truncated SEL, cut JSON, a device without link state), scripts a boot over SOL, applies `Plant`s (PCIe width or speed loss, Xid, SEL entry, missing device, firmware change, boot failure) at a chosen cycle and records every power action. `FakeHal` implements the protocol over a set of targets and can crash once in a chosen method, the way a killed executor looks to the journal. |
