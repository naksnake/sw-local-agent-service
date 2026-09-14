# slas-hal

Hardware abstraction: the one interface every physical action goes through (CLAUDE.md §5.2,
INV-3, INV-5). P7 shipped the models, the Redfish parsers, the plan primitives and the fakes;
P8 adds the real drivers behind the same interface, tested against a fake Redfish service and
fake `ssh`/`ipmitool` processes. Registering a target and arming it: `docs/runbooks/targets.md`.

| Module | What it does |
|---|---|
| `model.py` | `Snapshot` (power, `Inventory` with `PcieDevice` width AND speed and firmware, `Counters` AER/EDAC/MCE/Xid, `SelEntry`); what a baseline and every VERIFY compare. |
| `redfish.py` | Redfish payloads → models. A malformed or truncated answer is a three-part `HalError`, never a half-filled model. |
| `hal.py` | The `Hal` protocol: power, SEL, inventory, counters, snapshot, console, fence marker, wait for the OS, SSH argv. Targets are opaque references; drivers resolve credentials at dispatch. |
| `primitives.py` | The Validation plan verbs, their arguments and risk; renders `plans/schema/plan.schema.json` and `plans/primitives/validation.yaml` (a test keeps them in step). |
| `targets.py`, `credentials.py` | `TargetRecord` (BMC, SSH, PDU outlet — credential *references* only, INV-5), `TargetRegistry` with the arming gate: every new target refuses power actions until `slas target arm` records who confirmed it free. `CredentialResolver` turns `env:NAME` (quickstart) or `vault:PATH` (prod) into the secret at dispatch. |
| `quirks.py` | BMC quirk shims keyed by Manufacturer regex and firmware range, merged into a `QuirkSet` the Redfish driver reads; rendered to `config/bmc-quirks.yaml`. |
| `drivers/redfish.py` | The Redfish driver: DMTF URIs discovered from the service root, Basic auth per request, paged SEL, PCIe device links followed, `ComputerSystem.Reset`; audit rows of method, path, status, ms. |
| `drivers/ipmi.py`, `drivers/ssh.py` | `ipmitool` and `ssh` as argv-only children: password in `IPMI_PASSWORD`, key on tmpfs for one command and shredded, pinned `known_hosts`. In-band parsers for `lspci -D -vv -nn` (LnkSta width AND speed) and `dmesg` (AER/EDAC/MCE/Xid). |
| `drivers/sol.py`, `drivers/syslog.py` | SOL capture (`ipmitool sol activate` streamed into `Validation/Console/<alias>.log`, fence markers appended) and the UDP syslog receiver (RFC 3164/5424 → JSONL). |
| `drivers/pdu.py` | The PDU protocol behind AC cycles and a fake; the lab's PDU model is not named yet, so its driver is an explicit TODO and AC cycles are refused with a sentence. |
| `drivers/real.py` | `RealHal`: the `Hal` protocol over the drivers, one record per alias; every power action passes the arming gate, is journalled ahead to `Validation/Power/<alias>.jsonl`, and goes through Redfish, IPMI (per quirk) or the PDU. |
| `sinkcheck.py` | The CI grep: `python -m slas_hal.sinkcheck <dir> --known …` fails when any file under a log-sink directory carries a known secret or a credential shape. |
| `fakes/redfish_server.py` | A fake Redfish service over a `FakeTarget`: auth, collections, reset action, paged SEL, device links, planted faults. The real driver is tested against it. |
| `fakes/bmc.py` | `FakeTarget` plays back the recorded fixtures under `fakes/fixtures/` (ugly ones included: truncated SEL, cut JSON, a device without link state), scripts a boot over SOL, applies `Plant`s (PCIe width or speed loss, Xid, SEL entry, missing device, firmware change, boot failure) at a chosen cycle and records every power action. `FakeHal` implements the protocol over a set of targets and can crash once in a chosen method, the way a killed executor looks to the journal. |
