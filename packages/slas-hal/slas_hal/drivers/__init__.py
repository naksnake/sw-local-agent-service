"""Real drivers behind the `Hal` protocol (CLAUDE.md §5.2): Redfish over HTTPS, ipmitool and
ssh as argv-only child processes, SOL capture, a syslog receiver and the PDU protocol.
`real.RealHal` composes them; the executor sees the same interface as `slas_hal.fakes`.

Secrets reach a child process through an environment variable (`IPMI_PASSWORD`) or a 0600
file on tmpfs that is shredded afterwards — never argv, never a URL, never a log (INV-5).
"""
