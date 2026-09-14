# Registering a lab target and arming power actions

The Validation Agent drives one server at a time through `slas_hal`. Before a run can touch
a server, a person registers it and, separately, confirms it is free and **arms** it. Every
new target starts disarmed; a disarmed target refuses every power action, whatever the plan
says (CLAUDE.md §1.2 value 2, INV-7).

## 1 · Put the secrets where the executor can read them

Target records carry **references**, never secrets (INV-5). In quickstart the references
point at the validation executor's environment, filled from `.env`:

```
LAB_GX8_01_BMC_PASSWORD=…            # the BMC user's password
LAB_GX8_01_SSH_KEY=-----BEGIN OPENSSH PRIVATE KEY-----\n…\n-----END OPENSSH PRIVATE KEY-----
```

The prod profile uses `vault:kv/lab/gx8-01/bmc` references instead. A record that holds a
literal secret is refused when added, and the literal is not echoed back.

## 2 · Write the record

`lab-gx8-01.json`:

```json
{
  "alias": "lab-gx8-01",
  "bmc": {
    "host": "10.20.30.40",
    "user": "slas-validation",
    "password_ref": "env:LAB_GX8_01_BMC_PASSWORD",
    "https_port": 443,
    "ipmi_port": 623,
    "ca_bundle": null
  },
  "ssh": {
    "host": "10.20.30.41",
    "user": "slas",
    "port": 22,
    "private_key_ref": "env:LAB_GX8_01_SSH_KEY",
    "known_hosts_line": "10.20.30.41 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI…"
  },
  "pdu": null,
  "vendor_hint": null
}
```

- `bmc.user` needs Redfish read access, the `ComputerSystem.Reset` action and, for SOL,
  IPMI-over-LAN with the Operator privilege. Use a dedicated BMC account.
- `ssh.known_hosts_line` is the target's host key as `ssh-keyscan -t ed25519 <host>` prints
  it. `ssh` runs with `StrictHostKeyChecking=yes`; an unknown or changed key is refused.
- `ssh.user` needs to run `sync`, `dmesg`, `lspci -D -vv -nn` and `logger`; nothing needs
  root beyond what `dmesg_restrict` on the target allows.
- `pdu` names the outlet for AC cycles. The driver for the lab's PDU model is not written
  yet (`slas_hal.drivers.pdu`); until it is, AC cycles are refused with a sentence.
- `bmc.ca_bundle` points at the CA that signed the BMC certificate when it is not the
  platform bundle. TLS is always verified.

## 3 · Register, check, arm

```bash
slas target add lab-gx8-01.json
slas target list                       # "… power actions not armed."
# Read-only checks (power state, SEL, inventory) work now; power actions do not.
slas target arm lab-gx8-01 --by lee --note "Rack 4, no other job on it, cables checked."
slas target disarm lab-gx8-01          # when the run is over or the server is handed back
```

Arming is recorded with who, when and the note; the run's power journal
(`Validation/Power/<alias>.jsonl`) shows every intent before the action that followed it.

## What the drivers do with the record

| Need | Driver | How |
|---|---|---|
| Power state, SEL, inventory, reset | Redfish over HTTPS | Basic auth header built per request from the resolved secret; DMTF URIs discovered from `/redfish/v1/`; vendor quirks from `config/bmc-quirks.yaml` |
| Power when the quirk table says Redfish cannot | `ipmitool` | argv only; the password in `IPMI_PASSWORD` (`-E`), never on the command line |
| Serial console | `ipmitool sol activate` | streamed into `Validation/Console/<alias>.log`; fence markers are appended there |
| Counters, sync, lspci, fence marker in syslog | `ssh` | key written 0600 to tmpfs for one command and shredded; `BatchMode=yes`, pinned `known_hosts` |
| Remote syslog | UDP receiver on `SYSLOG_LISTEN` | targets send `*.* @<executor>:5514`; one JSON line per message |
| AC cycle | PDU driver | outlet off, wait, on — once the lab's PDU model has a driver |
